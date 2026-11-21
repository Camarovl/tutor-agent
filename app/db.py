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
def init_db() -> None:
    """按 schema.sql 建表。幂等：反复调用不会报错、不会丢数据。"""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with _tx() as conn:
        conn.executescript(sql)


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
