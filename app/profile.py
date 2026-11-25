"""功能③④的统计层：掌握度估计、薄弱点排序、练习选池与判分。

维护人：C₂。契约：docs/interfaces.md 第七节。

掌握度不是简单除法，而是三个确定性因子的乘积：

    掌握度 = 基础正确率 × 难度加权系数 × 时间衰减系数

    难度加权：全易题 1.0 / 含中等 1.05 / 含难题 1.15
    时间衰减：距上次练习 ≤7 天 1.0 / 8~30 天 0.9 / >30 天 0.75

封顶的是**最终掌握度**（截断到 1.0），不是难度系数本身 —— 否则 1.15 永远
会被先截掉，“难题答对更能证明掌握”就形同虚设。

分级阈值（练过 ≥3 次才参与分级，否则一律「样本不足」）：

    <40% 未掌握 / <70% 基本掌握 / ≥70% 已掌握

⚠️ 分层说明：
    契约第七节把 mastery_table() 的实现放在本文件（统计逻辑），但全项目
    红线又是「SQL 只在 db.py」。这里的折中是：**只读统计 SQL 集中在
    _MASTERY_SQL 一处**，通过公开的 db.get_conn() 执行，不写库、不拼参数、
    不散落。如果 A/C₁ 要求严格收口，把这段 SQL 原样搬到 db.mastery_rows()，
    本文件只保留 DataFrame 与 weak_points 即可。
"""

from __future__ import annotations

import pandas as pd

from . import db

#: 契约第七节定死的 11 列，顺序不能改（界面按位置渲染）
MASTERY_COLUMNS = [
    "kp_id",
    "kp_name",
    "chapter_id",
    "chapter_name",
    "attempts",
    "correct",
    "correct_rate",
    "difficulty_factor",
    "decay_factor",
    "mastery",
    "status",
]

#: 只读统计 SQL。LEFT JOIN 保证从没练过的知识点也在结果里；
#: NULLIF 防除零；排序不用 NULLS LAST（旧 SQLite 不认）。
_MASTERY_SQL = """
WITH stats AS (
    SELECT
        kp.id AS kp_id,
        kp.name AS kp_name,
        kp.chapter_id AS chapter_id,
        c.name AS chapter_name,
        COUNT(pl.id) AS attempts,
        COALESCE(SUM(CASE WHEN pl.is_correct = 1 THEN 1 ELSE 0 END), 0) AS correct,
        MAX(pl.created_at) AS last_practiced_at,
        MAX(CASE WHEN pl.difficulty = 'medium' THEN 1 ELSE 0 END) AS has_medium,
        MAX(CASE WHEN pl.difficulty = 'hard' THEN 1 ELSE 0 END) AS has_hard
    FROM knowledge_point kp
    JOIN chapter c ON c.id = kp.chapter_id
    LEFT JOIN practice_log pl ON pl.kp_id = kp.id
    GROUP BY kp.id, kp.name, kp.chapter_id, c.name
),
rated AS (
    SELECT
        *,
        CASE
            WHEN attempts = 0 THEN 0.0
            ELSE correct * 1.0 / NULLIF(attempts, 0)
        END AS correct_rate,
        CASE
            WHEN attempts = 0 THEN 0.0
            WHEN has_hard = 1 THEN 1.15
            WHEN has_medium = 1 THEN 1.05
            ELSE 1.0
        END AS difficulty_factor,
        CASE
            WHEN attempts = 0 THEN 0.0
            WHEN julianday('now', 'localtime') - julianday(last_practiced_at) <= 7 THEN 1.0
            WHEN julianday('now', 'localtime') - julianday(last_practiced_at) <= 30 THEN 0.9
            ELSE 0.75
        END AS decay_factor
    FROM stats
),
scored AS (
    SELECT
        *,
        CASE
            WHEN attempts = 0 THEN 0.0
            WHEN correct_rate * difficulty_factor * decay_factor > 1.0 THEN 1.0
            ELSE correct_rate * difficulty_factor * decay_factor
        END AS mastery
    FROM rated
)
SELECT
    kp_id,
    kp_name,
    chapter_id,
    chapter_name,
    attempts,
    correct,
    correct_rate,
    difficulty_factor,
    decay_factor,
    mastery,
    CASE
        WHEN attempts < 3 THEN '样本不足'
        WHEN mastery < 0.4 THEN '未掌握'
        WHEN mastery < 0.7 THEN '基本掌握'
        ELSE '已掌握'
    END AS status
FROM scored
ORDER BY
    CASE WHEN mastery IS NULL THEN 1 ELSE 0 END,
    CASE
        WHEN attempts < 3 THEN 3
        WHEN mastery < 0.4 THEN 0
        WHEN mastery < 0.7 THEN 1
        ELSE 2
    END,
    mastery ASC,
    kp_name ASC
"""


def mastery_table() -> pd.DataFrame:
    """返回全部知识点的掌握度表（含从没练过的，11 列顺序固定）。

    没练过的行：attempts/correct/correct_rate/difficulty_factor/decay_factor/
    mastery 全部为 0.0，status 固定「样本不足」——契约要求这些行必须出现，
    否则界面上的表会缺行。
    """
    conn = db.get_conn()
    try:
        rows = conn.execute(_MASTERY_SQL).fetchall()
    finally:
        conn.close()

    if not rows:
        return pd.DataFrame(columns=MASTERY_COLUMNS)

    df = pd.DataFrame([dict(row) for row in rows], columns=MASTERY_COLUMNS)

    # 保留 4 位小数，和桩数据/界面展示口径一致；阈值判断在 SQL 里已经做完。
    for column in ("correct_rate", "difficulty_factor", "decay_factor", "mastery"):
        df[column] = df[column].astype(float).round(4)

    df["attempts"] = df["attempts"].astype(int)
    df["correct"] = df["correct"].astype(int)
    return df


def weak_points(n: int) -> list[dict]:
    """排除「样本不足」后，掌握度最低的 n 个知识点（供功能④出题）。

    不只看「未掌握」：如果库里只有两个薄弱点而 n=3，第三个会从「基本掌握」
    或「已掌握」里补 —— 但绝不会拿「样本不足」来凑数，否则系统会一直推
    没人练过的题（这正是契约要求排除样本不足的原因）。
    """
    if n <= 0:
        return []
    df = mastery_table()
    if df.empty:
        return []
    ranked = df[df["status"] != "样本不足"].sort_values("mastery")
    return ranked.head(int(n)).to_dict("records")
