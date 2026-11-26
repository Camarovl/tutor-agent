"""数据访问层 —— 🔴 全项目唯一允许碰数据库的文件。

分层约束（见 app/README.md 与 AGENTS.md 第四条第 8 项）：
- 本文件之外任何地方都不许写 SQL；要数据一律调这里的函数。
- 所有 SQL 一律用 `?` 占位参数化，禁止 f-string / 字符串拼接。
- 表结构见根目录 schema.sql。

SQLite 是 Python 自带的：不用安装、没有服务进程，
数据库就是 data/app.db 这个文件，第一次连接时自动创建。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

# ------------------------------------------------------------
# 路径约定
#   DATA_DIR = <仓库根>/data      （本文件在 <仓库根>/app/ 下）
#   DB_PATH  = <仓库根>/data/app.db
# 🔴 数据库文件由 .gitignore 排除，绝不入库。
# ------------------------------------------------------------
APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "app.db"
SCHEMA_PATH = ROOT_DIR / "schema.sql"


# ============================================================
# 连接管理
# ============================================================
def get_conn() -> sqlite3.Connection:
    """返回一个可用的数据库连接。

    两个开关在每个连接上都必须重新设置，因为它们是「按连接生效」的：
    - PRAGMA foreign_keys = ON   → 不设就等于外键约束不生效，
      可以插入指向不存在章节的脏数据，程序不报错，统计时才发现。
    - row_factory = sqlite3.Row  → 让查询结果能按列名取值（row["name"]）。

    调用方负责自己 commit / close，推荐写法见 _tx()。
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


class _tx:
    """极简事务上下文：正常退出自动 commit，出异常自动 rollback，最后关连接。

    用法：
        with _tx() as conn:
            conn.execute(...)
    """

    def __enter__(self) -> sqlite3.Connection:
        self.conn = get_conn()
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()
        # 返回 False：异常照常向上抛，不吞掉
        return False


# ============================================================
# 建库
# ============================================================
def _migrate(conn: sqlite3.Connection) -> None:
    """补齐老库缺的列。幂等：列已存在就跳过。

    schema.sql 用的是 CREATE TABLE IF NOT EXISTS —— 表已存在就整段跳过，
    不会给老库补新列。所以升级过 schema 之后，跑过 seed_data.py 的机器
    必须靠这里补齐，否则一查 wrong_question 就报 no such column。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(wrong_question)")}
    added = []
    for name, ddl in (
        ("source_question_id", "TEXT"),
        ("error_count", "INTEGER NOT NULL DEFAULT 1"),
        ("last_seen_at", "TEXT"),
    ):
        if name not in cols:
            # 列名与类型是写死的常量，不是外部输入；
            # SQLite 的 ALTER TABLE ADD COLUMN 不接受 ? 占位参数
            conn.execute(f"ALTER TABLE wrong_question ADD COLUMN {name} {ddl}")
            added.append(name)
    if added:
        print(f"[db] 迁移：wrong_question 补列 {added}")


#: 同一道题只留一条记录。用部分索引：手工录入的 source_question_id 为 NULL，
#: NULL 不参与唯一性判断，因此允许自由重复插入。
#: 🔴 必须等到 _migrate() 补完列再建，否则老库上会报 no such column。
_WQ_SOURCE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_wq_source
    ON wrong_question(source_question_id)
    WHERE source_question_id IS NOT NULL;
"""


def _ensure_indexes(conn: sqlite3.Connection) -> None:
    """建依赖新列的索引。放在 _migrate() 之后执行。"""
    conn.executescript(_WQ_SOURCE_INDEX)


def init_db() -> None:
    """建表 + 补列 + 建索引。幂等：反复调用不会报错、不会丢数据。

    三步的顺序是关键：
      ① executescript(schema)  新库一次建到位；老库因 IF NOT EXISTS 跳过
      ② _migrate               老库补上 schema 新增的列
      ③ _ensure_indexes        索引引用了新列，必须在补列之后建

    _tx 退出时已自动 commit，这里不再重复提交。
    """
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with _tx() as conn:
        conn.executescript(sql)      # ① 建表（新库一次到位）
        _migrate(conn)               # ② 老库补列
        _ensure_indexes(conn)        # ③ 补完列才能建索引


def insert_chapters(chapters: list[dict]) -> int:
    """灌入 15 个归类抽屉（🔴 不是知识点库）。

    可重复运行：先清空 chapter 再灌，第二次跑不会撞 UNIQUE 约束。
    注意 knowledge_point 通过外键指向 chapter，所以清空前必须先把
    依赖它们的行一并处理 —— 这里用 DELETE 清全表，外键 ON DELETE CASCADE
    会连带清掉挂在章节下的知识点。章节重灌本来就意味着知识框架重置。

    参数 chapters: [{"id":..., "name":..., "brief":...}, ...]
    返回: 实际写入的行数
    """
    if not chapters:
        return 0

    rows = [
        (c["id"], c.get("name"), c.get("brief"))
        for c in chapters
    ]

    with _tx() as conn:
        conn.execute("DELETE FROM chapter")
        conn.executemany(
            "INSERT INTO chapter (id, name, brief) VALUES (?, ?, ?)",
            rows,
        )
    return len(rows)


def load_chapters_from_json() -> list[dict]:
    """读取 data/chapters.json，供 insert_chapters() 使用。"""
    path = DATA_DIR / "chapters.json"
    with path.open(encoding="utf-8") as f:
        return json.load(f)


# ============================================================
# 错题
# ============================================================
def insert_wrong_question(data: dict, image_path: str | None = None) -> int:
    """写入一道错题及其知识点关联，返回新记录 id。

    data 字段（缺的可以没有）：question_text / student_answer /
    correct_answer / solution_steps / error_type / difficulty /
    source / kp_ids（知识点 id 列表）。

    🔴 solution_steps 可以是 list，会存成 JSON 文本。
    🔴 image_path 只存相对路径，不把二进制塞进数据库。
    """
    steps = data.get("solution_steps")
    if isinstance(steps, (list, tuple)):
        steps = json.dumps(steps, ensure_ascii=False)
    if steps is not None and not isinstance(steps, str):
        steps = str(steps)

    kp_ids = data.get("kp_ids") or []

    with _tx() as conn:
        cur = conn.execute(
            """
            INSERT INTO wrong_question
                (question_text, student_answer, correct_answer, solution_steps,
                 error_type, difficulty, source, image_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data.get("question_text"),
                data.get("student_answer"),
                data.get("correct_answer"),
                steps,
                data.get("error_type"),
                data.get("difficulty"),
                data.get("source"),
                image_path,
            ),
        )
        wq_id = cur.lastrowid
        if kp_ids:
            conn.executemany(
                "INSERT OR IGNORE INTO wq_kp (wq_id, kp_id) VALUES (?, ?)",
                [(wq_id, kp) for kp in kp_ids],
            )
    return int(wq_id)


def upsert_wrong_question_by_source(
    source_question_id: str,
    data: dict,
    image_path: str | None = None,
) -> int:
    """按题目 id 幂等写入错题：同一道题再次做错只累加，不新建记录。

    - 首次出现：插入一行，error_count = 1，last_seen_at 记当前时间
    - 再次出现：error_count + 1，刷新 last_seen_at，
                并同步题干/答案/错因/难度这些可能被改过的字段
    - 知识点关联始终 INSERT OR IGNORE，不重复、不删除已有挂载

    返回该错题记录的主键 id。

    🔴 整个「查 → 插/更新 → 挂知识点」在同一个事务里完成，
       避免并发时同一题被插成两行（唯一索引 idx_wq_source 是最后一道防线）。
    """
    if not source_question_id:
        raise ValueError("source_question_id 不能为空")

    steps = data.get("solution_steps")
    if isinstance(steps, (list, tuple)):
        steps = json.dumps(steps, ensure_ascii=False)
    if steps is not None and not isinstance(steps, str):
        steps = str(steps)

    kp_ids = data.get("kp_ids") or []

    with _tx() as conn:
        row = conn.execute(
            "SELECT id FROM wrong_question WHERE source_question_id = ?",
            (source_question_id,),
        ).fetchone()

        if row is None:
            cur = conn.execute(
                """
                INSERT INTO wrong_question
                    (question_text, student_answer, correct_answer, solution_steps,
                     error_type, difficulty, source, image_path,
                     source_question_id, error_count, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, datetime('now','localtime'))
                """,
                (
                    data.get("question_text"),
                    data.get("student_answer"),
                    data.get("correct_answer"),
                    steps,
                    data.get("error_type"),
                    data.get("difficulty"),
                    data.get("source"),
                    image_path,
                    source_question_id,
                ),
            )
            wq_id = int(cur.lastrowid)
        else:
            wq_id = int(row["id"])
            conn.execute(
                """
                UPDATE wrong_question SET
                    question_text  = COALESCE(?, question_text),
                    student_answer = COALESCE(?, student_answer),
                    correct_answer = COALESCE(?, correct_answer),
                    solution_steps = COALESCE(?, solution_steps),
                    error_type     = COALESCE(?, error_type),
                    difficulty     = COALESCE(?, difficulty),
                    source         = COALESCE(?, source),
                    image_path     = COALESCE(?, image_path),
                    error_count    = error_count + 1,
                    last_seen_at   = datetime('now','localtime')
                WHERE id = ?
                """,
                (
                    data.get("question_text"),
                    data.get("student_answer"),
                    data.get("correct_answer"),
                    steps,
                    data.get("error_type"),
                    data.get("difficulty"),
                    data.get("source"),
                    image_path,
                    wq_id,
                ),
            )

        if kp_ids:
            conn.executemany(
                "INSERT OR IGNORE INTO wq_kp (wq_id, kp_id) VALUES (?, ?)",
                [(wq_id, kp) for kp in kp_ids],
            )

    return wq_id


def recent_wrong_questions(limit: int = 20) -> list[dict]:
    """最近录入的错题，新的在前。附带该题挂载的知识点名字。"""
    with _tx() as conn:
        rows = conn.execute(
            """
            SELECT * FROM wrong_question
            ORDER BY datetime(created_at) DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        result = []
        for r in rows:
            item = dict(r)
            kps = conn.execute(
                """
                SELECT kp.id, kp.name
                FROM wq_kp JOIN knowledge_point kp ON kp.id = wq_kp.kp_id
                WHERE wq_kp.wq_id = ?
                ORDER BY kp.name
                """,
                (item["id"],),
            ).fetchall()
            item["knowledge_points"] = [dict(k) for k in kps]
            result.append(item)
    return result


def wrong_question_count() -> int:
    """错题总数。"""
    with _tx() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM wrong_question").fetchone()[0])


# ============================================================
# 练习记录
# ============================================================
def log_practice(
    kp_id: str,
    is_correct: bool,
    question_ref: str | None = None,
    difficulty: str | None = None,
    duration_sec: int | None = None,
) -> None:
    """写一条练习记录。掌握度统计全靠这张表。"""
    with _tx() as conn:
        conn.execute(
            """
            INSERT INTO practice_log
                (kp_id, question_ref, is_correct, difficulty, duration_sec)
            VALUES (?, ?, ?, ?, ?)
            """,
            (kp_id, question_ref, 1 if is_correct else 0, difficulty, duration_sec),
        )


# ============================================================
# 章节 / 知识点的细粒度访问
#
# 🔴 这一组是给 app/knowledge.py 用的：那里只做业务判断，
#    SQL 一律收口到这里。事务边界也留在这里，_tx 不出本文件。
# ============================================================
def chapter_rows() -> list[dict]:
    """全部章节（id / name / brief），按灌入顺序。

    供 knowledge.chapter_candidates() 使用 —— AI 唯一允许挂靠的层级。
    """
    with _tx() as conn:
        rows = conn.execute(
            "SELECT id, name, brief FROM chapter ORDER BY rowid"
        ).fetchall()
    return [dict(r) for r in rows]


def chapter_exists(chapter_id: str) -> bool:
    """章节 id 是否存在于 chapter 表。"""
    with _tx() as conn:
        row = conn.execute(
            "SELECT 1 FROM chapter WHERE id = ?", (chapter_id,)
        ).fetchone()
    return row is not None


def find_kp_by_name(name: str) -> dict | None:
    """按名字精确查知识点，查不到返回 None。"""
    with _tx() as conn:
        row = conn.execute(
            "SELECT id, name FROM knowledge_point WHERE name = ?", (name,)
        ).fetchone()
    return dict(row) if row is not None else None


def list_kps() -> list[dict]:
    """全部知识点（id / name）。查重时逐条比对用。"""
    with _tx() as conn:
        rows = conn.execute(
            "SELECT id, name FROM knowledge_point"
        ).fetchall()
    return [dict(r) for r in rows]


def list_kps_with_chapter() -> list[dict]:
    """全部知识点，附带所属章节中文名（id / name / chapter_id / chapter_name）。

    供 knowledge.all_knowledge_points() 使用，注入提示词当候选列表。
    顺序按章节再按名字，保证提示词内容稳定可复现。
    """
    with _tx() as conn:
        rows = conn.execute(
            """
            SELECT kp.id, kp.name, kp.chapter_id, c.name AS chapter_name
            FROM knowledge_point kp
            JOIN chapter c ON c.id = kp.chapter_id
            ORDER BY kp.chapter_id, kp.name
            """
        ).fetchall()
    return [dict(r) for r in rows]


def insert_kp(
    kp_id: str,
    name: str,
    chapter_id: str,
    explain: str | None,
    created_from: str,
) -> str:
    """新建一个知识点，返回写入的 kp_id。

    返回值与其他 insert 函数（insert_chapters / insert_wrong_question 等）
    保持一致，调用方可以直接拿去用，不必自己回查。

    🔴 外键保证 chapter_id 必须是已存在的章节；撞 id 或 name 会抛
    sqlite3.IntegrityError，由调用方决定怎么处理（并发复用属于正常情况）。
    """
    with _tx() as conn:
        conn.execute(
            """
            INSERT INTO knowledge_point (id, name, chapter_id, explain, created_from)
            VALUES (?, ?, ?, ?, ?)
            """,
            (kp_id, name, chapter_id, explain, created_from),
        )
    return kp_id


def find_kp_by_id_or_name(kp_id: str, name: str) -> dict | None:
    """按 id 或名字查知识点，查不到返回 None。

    insert 撞唯一约束后用来把「刚好被并发建出来的那条」捞回来。
    """
    with _tx() as conn:
        row = conn.execute(
            "SELECT id FROM knowledge_point WHERE id = ? OR name = ?",
            (kp_id, name),
        ).fetchone()
    return dict(row) if row is not None else None


# ============================================================
# 掌握度与调用统计（只读）
#
# 🔴 这两段 SQL 原样搬自 app/profile.py（C₂），一个字都没改语义。
#    profile.py 只负责把这些行转成 DataFrame 与业务判断，不再自己写 SQL。
# ============================================================

#: 掌握度聚合查询。
#: LEFT JOIN 保证从没练过的知识点也在结果里；NULLIF 防除零；
#: 排序不用 NULLS LAST（旧 SQLite 不认）。
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
_LLM_USAGE_SQL = """
SELECT
    COUNT(*) AS calls,
    COALESCE(SUM(COALESCE(prompt_tokens, 0) + COALESCE(completion_tokens, 0)), 0) AS tokens
FROM llm_log
"""


def mastery_rows() -> list[dict]:
    """每个知识点的掌握度聚合行，11 列，顺序见 interfaces.md 第一节。

    包含从没练过的知识点（attempts=0，各系数与 mastery 为 0.0，
    status 为「样本不足」）——契约要求这些行必须出现，否则界面缺行。

    🔴 本函数只取数据，不做 DataFrame 转换与四舍五入；那些归 profile.py。
    """
    with _tx() as conn:
        rows = conn.execute(_MASTERY_SQL).fetchall()
    return [dict(r) for r in rows]


def llm_usage() -> dict:
    """累计模型调用次数与 token 用量，返回 {"calls": int, "tokens": int}。

    没有记录时返回零值，不报错。
    """
    with _tx() as conn:
        row = conn.execute(_LLM_USAGE_SQL).fetchone()
    if row is None:
        return {"calls": 0, "tokens": 0}
    return {"calls": int(row["calls"] or 0), "tokens": int(row["tokens"] or 0)}


def log_llm_call(
    purpose: str,
    prompt_tokens: int,
    completion_tokens: int,
    retried: int = 0,
    success: bool = True,
) -> None:
    """记录一次模型调用。由 llm_client 在每次调用结束后调用。

    🔴 这里只负责写库，不做任何业务判断：调用成败、是否重试都由
       llm_client 判定后传进来。

    字段与 schema.sql 的 llm_log 定义一一对应。
    """
    with _tx() as conn:
        conn.execute(
            """
            INSERT INTO llm_log
                (purpose, prompt_tokens, completion_tokens, retried, success)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                purpose,
                int(prompt_tokens or 0),
                int(completion_tokens or 0),
                int(retried or 0),
                1 if success else 0,
            ),
        )
