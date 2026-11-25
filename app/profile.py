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
    _MASTERY_SQL 与 _LLM_USAGE_SQL 两处**，通过公开的 db.get_conn() 执行，
    不写库、不拼参数、不散落。如果 A/C₁ 要求严格收口，把两段 SQL 分别搬到
    db.mastery_rows() / db.llm_usage()，本文件只保留 DataFrame 与业务函数。
"""

from __future__ import annotations

import json
from pathlib import Path

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

#: 模型调用累计次数 / token 用量（llm_log 是所有模型调用的流水表）。
#: 目前 main 上还没有 llm_client 写入它；B 合并后这里自然有数。
_LLM_USAGE_SQL = """
SELECT
    COUNT(*) AS calls,
    COALESCE(SUM(COALESCE(prompt_tokens, 0) + COALESCE(completion_tokens, 0)), 0) AS tokens
FROM llm_log
"""


def _llm_usage() -> tuple[int, int]:
    """返回 (累计调用次数, 累计 token 用量)；没有记录时为 (0, 0)。"""
    conn = db.get_conn()
    try:
        row = conn.execute(_LLM_USAGE_SQL).fetchone()
    finally:
        conn.close()
    if row is None:
        return 0, 0
    return int(row["calls"] or 0), int(row["tokens"] or 0)


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


def summary_text() -> dict:
    """界面顶部指标卡的 6 个键（键名由契约第七节定死）。

    llm_calls / llm_tokens 来自 llm_log 流水；main 上还没有 llm_client 写入它，
    所以现在通常是 0。B 合并后不需要改本函数，日志一有记录就会自动统计。
    """
    df = mastery_table()
    calls, tokens = _llm_usage()
    return {
        "kp_total": int(len(df)),
        "wrong_total": int(db.wrong_question_count()),
        "practice_total": int(df["attempts"].sum()) if not df.empty else 0,
        "weak_count": int((df["status"] == "未掌握").sum()) if not df.empty else 0,
        "llm_calls": calls,
        "llm_tokens": tokens,
    }


def build_practice_session(count: int = 3) -> tuple[list[dict], str]:
    """按薄弱知识点从本地题库选一组题（不调用模型）。

    选池规则：
      1. 只保留 kp_ids 都能在数据库里找到的题，避免练完写 practice_log 时外键失败；
      2. 按「命中薄弱知识点的个数」降序，同分按题目 id 稳定排序；
      3. 题库不够 count 题就返回现有的，并在来源说明里写清楚 —— 不假装有生成能力。
    """
    count = int(count)
    if count <= 0:
        return [], "题目数量必须大于 0"

    bank_path = Path(__file__).resolve().parent.parent / "data" / "question_bank.json"
    try:
        bank = json.loads(bank_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], f"题库读取失败：{type(exc).__name__}: {exc}"
    if not isinstance(bank, list) or not bank:
        return [], "题库为空，暂无题可练"

    existing = {row["id"] for row in db.list_kps()}
    valid: list[dict] = []
    for item in bank:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        kp_ids = item.get("kp_ids")
        if not isinstance(kp_ids, list) or not kp_ids:
            continue
        if not all(isinstance(kp_id, str) and kp_id in existing for kp_id in kp_ids):
            continue
        valid.append(item)

    if not valid:
        return [], "题库里的题目没有可用的知识点（可能还没跑 seed_data.py）"

    weak_ids = {row["kp_id"] for row in weak_points(count)}

    def weak_hits(item: dict) -> int:
        return sum(1 for kp_id in item["kp_ids"] if kp_id in weak_ids)

    ranked = sorted(valid, key=lambda item: (-weak_hits(item), str(item["id"])))
    chosen = [dict(item) for item in ranked[:count]]
    note = (
        "本次来自薄弱知识点（题库）"
        if any(weak_hits(item) > 0 for item in chosen)
        else "本次来自题库（薄弱点覆盖不足）"
    )
    if len(chosen) < count:
        note += f"；题库只有 {len(chosen)} 题，未接入生成"
    return chosen, note


def grade(item: dict, user_answer) -> bool:
    """客观判分（纯函数，不读库、不联网）。

    choice：忽略大小写与首尾空格；
    fill  ：去掉所有空白后比较（不承诺 1/2 与 0.5 这类等价写法）；
    未知题型：False。
    """
    if not isinstance(item, dict):
        return False
    expected = str(item.get("answer") or "").strip()
    got = str(user_answer or "").strip()
    if not expected:
        return False

    kind = item.get("type")
    if kind == "choice":
        return got.upper() == expected.upper()
    if kind == "fill":
        def normalize(value: str) -> str:
            return "".join(value.split()).lower()

        return normalize(got) == normalize(expected)
    return False


def record_result(items: list[dict], answers) -> list[bool]:
    """判分 + 写 practice_log + 做错的题回流错题本。

    answers 允许是 {题目 id: 用户答案}（界面用这个），也兼容按位置给的列表。
    每道题的每个 kp_id 各写一条练习记录，掌握度按知识点分别累计。
    """
    results: list[bool] = []
    for index, item in enumerate(items or []):
        if not isinstance(item, dict):
            results.append(False)
            continue

        qid = item.get("id")
        if isinstance(answers, dict):
            user_answer = answers.get(qid, "")
        elif isinstance(answers, (list, tuple)):
            user_answer = answers[index] if index < len(answers) else ""
        else:
            user_answer = ""

        ok = grade(item, user_answer)
        kp_ids = item.get("kp_ids")
        kp_ids = kp_ids if isinstance(kp_ids, list) else []

        for kp_id in kp_ids:
            db.log_practice(
                kp_id,
                ok,
                question_ref=qid,
                difficulty=item.get("difficulty"),
            )

        if not ok:
            explanation = item.get("explanation")
            steps = (
                [explanation]
                if isinstance(explanation, str) and explanation.strip()
                else []
            )
            db.insert_wrong_question(
                {
                    "question_text": item.get("stem") or "",
                    "student_answer": str(user_answer or ""),
                    "correct_answer": str(item.get("answer") or ""),
                    "solution_steps": steps,
                    "error_type": "unknown",
                    "difficulty": item.get("difficulty"),
                    "source": "practice",
                    "kp_ids": kp_ids,
                },
                image_path=None,
            )

        results.append(ok)
    return results
