"""离线自检 —— 不联网、不需要模型服务，先确认「地基」是好的。

维护人：C₂。契约：docs/interfaces.md（本脚本不调用任何业务函数，只做体检）。

用法：

    python tests/offline_check.py

覆盖范围（第 5 步初版 + 第 7 步校验器）：

    □ 知识框架数据 data/chapters.json 能加载、条数与 id 规范
    □ 题库数据 data/question_bank.json 能加载、结构/章节引用合法
    □ schema.sql 能建出六张表，且每个连接的外键开关真的生效
    □ 本地 data/app.db 能连上、表齐全、章节与数据文件一致
    □ 数据库里没有指向不存在记录的孤儿引用（外键完整性）
    □ 题库里的 kp_id 都能在数据库中找到
    □ 错题抽取校验器：非法 JSON/缺字段/越界 id/错误枚举/越界 confidence 都被拦住
    □ 未配置密钥：明确列出缺项，is_configured() 为 False，且不抛异常

输出每项一行 ✅/❌，最后一行总计；有任何失败退出码非 0。

🔴 本项目没有「知识点数据文件」—— 知识点是运行时从真实错题里长出来的
（AGENTS.md 红线 6）。这里检查的是章节抽屉（data/chapters.json）、题库，
以及数据库里已经长出来的知识点，不要为了凑检查项去预置知识点库。

Windows 中文控制台默认 GBK，打印 ✅/❌ 会抛 UnicodeEncodeError 导致退出码 1，
所以开头先 reconfigure。脚本不 import streamlit / pandas，保持离线可跑。
"""

from __future__ import annotations

import sys

# 🔴 必须在任何 print 之前：中文 Windows 控制台默认 GBK，emoji 会直接把脚本打成退出码 1。
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import json
import os
import sqlite3
from pathlib import Path

# 允许从任意目录执行：python tests/offline_check.py 或 python C:\...\tests\offline_check.py
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402 —— 必须在 sys.path 调整之后

DATA_DIR = ROOT / "data"
CHAPTERS_PATH = DATA_DIR / "chapters.json"
QUESTION_BANK_PATH = DATA_DIR / "question_bank.json"

EXPECTED_TABLES = {
    "chapter",
    "knowledge_point",
    "wrong_question",
    "wq_kp",
    "practice_log",
    "llm_log",
}

DIFFICULTIES = {"easy", "medium", "hard"}
QUESTION_TYPES = {"choice", "fill"}


class Report:
    """一行一项的体检记录，最后统一汇总。"""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self._first_failure = ""

    def ok(self, message: str) -> None:
        print(f"✅ {message}")
        self.passed += 1

    def fail(self, message: str, detail: str = "") -> None:
        print(f"❌ {message}")
        if detail:
            for line in str(detail).splitlines():
                print(f"   {line}")
        if not self._first_failure:
            self._first_failure = message
        self.failed += 1

    def summary(self) -> int:
        total = self.passed + self.failed
        print("-" * 60)
        if self.failed:
            if self._first_failure:
                print(f"第一处失败：{self._first_failure}")
            print(f"总计：{total} 项，通过 {self.passed} 项，失败 {self.failed} 项")
            return 1
        print(f"总计：{total} 项，全部通过")
        return 0


# ============================================================
# 数据文件
# ============================================================
def load_chapters(report: Report) -> list[dict] | None:
    """加载 15 个归类抽屉，并检查 id / name / brief。"""
    try:
        chapters = json.loads(CHAPTERS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        report.fail(f"章节数据：找不到 {CHAPTERS_PATH.relative_to(ROOT)}")
        return None
    except (OSError, json.JSONDecodeError) as exc:
        report.fail("章节数据：读取或解析失败", f"{type(exc).__name__}: {exc}")
        return None

    if not isinstance(chapters, list):
        report.fail("章节数据：顶层必须是数组")
        return None

    problems: list[str] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for index, item in enumerate(chapters, start=1):
        if not isinstance(item, dict):
            problems.append(f"第 {index} 项不是对象")
            continue
        chapter_id = item.get("id")
        name = item.get("name")
        brief = item.get("brief")
        if not isinstance(chapter_id, str) or not chapter_id.startswith("ch_"):
            problems.append(f"第 {index} 项 id 不是 ch_ 开头：{chapter_id!r}")
        elif chapter_id in seen_ids:
            problems.append(f"第 {index} 项 id 重复：{chapter_id}")
        else:
            seen_ids.add(chapter_id)
        if not isinstance(name, str) or not name.strip():
            problems.append(f"第 {index} 项 name 为空")
        elif name in seen_names:
            problems.append(f"第 {index} 项 name 重复：{name}")
        else:
            seen_names.add(name)
        if not isinstance(brief, str) or not brief.strip():
            problems.append(f"第 {index} 项 brief 为空")

    if problems:
        report.fail(f"章节数据：{len(chapters)} 条，但有 {len(problems)} 处不合法", "\n".join(problems[:6]))
        return None

    if len(chapters) != 15:
        report.fail(f"章节数据：应为 15 条，实际 {len(chapters)} 条")
        return None

    report.ok(f"章节数据：{len(chapters)} 条，id/name/brief 合法")
    return chapters


def load_question_bank(report: Report, chapters: list[dict] | None) -> list[dict] | None:
    """加载题库，检查字段、章节引用与 choice/fill 结构。"""
    try:
        bank = json.loads(QUESTION_BANK_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        report.fail(f"题库数据：找不到 {QUESTION_BANK_PATH.relative_to(ROOT)}")
        return None
    except (OSError, json.JSONDecodeError) as exc:
        report.fail("题库数据：读取或解析失败", f"{type(exc).__name__}: {exc}")
        return None

    if not isinstance(bank, list) or not bank:
        report.fail("题库数据：必须是非空数组")
        return None

    chapter_ids = {c["id"] for c in chapters or []}
    problems: list[str] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(bank, start=1):
        if not isinstance(item, dict):
            problems.append(f"第 {index} 题不是对象")
            continue
        qid = item.get("id")
        if not isinstance(qid, str) or not qid.startswith("qp_"):
            problems.append(f"第 {index} 题 id 不是 qp_ 开头：{qid!r}")
        elif qid in seen_ids:
            problems.append(f"第 {index} 题 id 重复：{qid}")
        else:
            seen_ids.add(qid)

        if not str(item.get("stem") or "").strip():
            problems.append(f"第 {index} 题 stem 为空")
        if not str(item.get("answer") or "").strip():
            problems.append(f"第 {index} 题 answer 为空")
        if not str(item.get("explanation") or "").strip():
            problems.append(f"第 {index} 题 explanation 为空")
        if item.get("difficulty") not in DIFFICULTIES:
            problems.append(f"第 {index} 题 difficulty 非法：{item.get('difficulty')!r}")

        chapter_id = item.get("chapter_id")
        if chapters is not None and chapter_id not in chapter_ids:
            problems.append(f"第 {index} 题 chapter_id 不在章节表：{chapter_id!r}")

        kp_ids = item.get("kp_ids")
        if not isinstance(kp_ids, list) or not kp_ids:
            problems.append(f"第 {index} 题 kp_ids 必须是非空数组")
        else:
            for kp_id in kp_ids:
                if not isinstance(kp_id, str) or not kp_id.startswith("kp_"):
                    problems.append(f"第 {index} 题 kp_id 不是 kp_ 开头：{kp_id!r}")

        qtype = item.get("type")
        if qtype not in QUESTION_TYPES:
            problems.append(f"第 {index} 题 type 非法：{qtype!r}")
        elif qtype == "choice":
            options = item.get("options")
            if not isinstance(options, dict) or len(options) < 2:
                problems.append(f"第 {index} 题 choice 选项不足 2 个")
            elif str(item.get("answer") or "").strip().upper() not in {
                str(key).strip().upper() for key in options
            }:
                problems.append(f"第 {index} 题 answer 不在选项里：{item.get('answer')!r}")
        elif qtype == "fill" and item.get("options") not in ({}, None):
            problems.append(f"第 {index} 题 fill 的 options 应为空对象")

    if problems:
        report.fail(f"题库数据：{len(bank)} 题，但有 {len(problems)} 处不合法", "\n".join(problems[:6]))
        return None

    report.ok(f"题库数据：{len(bank)} 题，结构/难度/章节引用合法")
    return bank


# ============================================================
# 错题抽取校验器（app/schemas.py）
# ============================================================
def check_validators(report: Report, chapters: list[dict] | None) -> None:
    """用一组畸形输入验证 make_intake_validator()，确认拦得住且说得出原因。

    这里显式传入章节候选，不依赖 data/app.db；生产路径（不传章节、从
    knowledge.chapter_candidates() 懒加载）由 check_database 之后单独保证。
    """
    try:
        from app import schemas
    except Exception as exc:  # noqa: BLE001
        report.fail("校验器：无法导入 app/schemas.py", f"{type(exc).__name__}: {exc}")
        return

    allowed_kps = ["kp_a", "kp_b", "kp_c", "kp_d"]
    allowed_chapters = (
        [chapter["id"] for chapter in chapters]
        if chapters
        else ["ch_function_limit"]
    )
    try:
        validate = schemas.make_intake_validator(allowed_kps, allowed_chapters)
    except Exception as exc:  # noqa: BLE001
        report.fail("校验器：构造失败", f"{type(exc).__name__}: {exc}")
        return

    base = {
        "question_text": "求极限 $\\lim_{x\\to 0}\\frac{\\sin 3x}{x}$",
        "student_answer": "1",
        "correct_answer": "3",
        "solution_steps": ["sin 3x ~ 3x", "原式 = 3"],
        "knowledge_ids": ["kp_a"],
        "new_concepts": [
            {"name": "等价无穷小替换", "chapter_id": allowed_chapters[0]}
        ],
        "difficulty": "easy",
        "error_type": "method_wrong",
        "confidence": 0.82,
    }

    missing_payload = dict(base)
    missing_payload.pop("confidence")
    missing_payload.pop("error_type")

    # 与 docs/interfaces.md 第六节逐条对应：9 条拦截项 + 6 个类型/正向用例。
    cases = [
        ("合法草稿", dict(base), None),
        ("不是 JSON 对象", ["not", "a", "dict"], ("JSON 对象",)),
        (
            "缺必填字段",
            missing_payload,
            ("缺少必填字段", "confidence", "error_type"),
        ),
        ("question_text 为空", {**base, "question_text": "   "}, ("question_text",)),
        ("solution_steps 为空", {**base, "solution_steps": []}, ("solution_steps",)),
        (
            "solution_steps 有空白项",
            {**base, "solution_steps": ["ok", " "]},
            ("solution_steps",),
        ),
        (
            "knowledge_ids 越界",
            {**base, "knowledge_ids": ["kp_not_exist"]},
            ("knowledge_ids", "候选列表", "kp_not_exist"),
        ),
        (
            "knowledge_ids 超过 3 个",
            {**base, "knowledge_ids": ["kp_a", "kp_b", "kp_c", "kp_d"]},
            ("最多 3",),
        ),
        (
            "knowledge_ids 不是数组",
            {**base, "knowledge_ids": "kp_a"},
            ("knowledge_ids", "数组"),
        ),
        (
            "new_concepts 章节越界",
            {
                **base,
                "new_concepts": [
                    {"name": "新概念", "chapter_id": "ch_not_exist"}
                ],
            },
            ("chapter_id", "15 个章节"),
        ),
        (
            "new_concepts 名字为空",
            {
                **base,
                "new_concepts": [
                    {"name": " ", "chapter_id": allowed_chapters[0]}
                ],
            },
            ("name",),
        ),
        ("difficulty 非法", {**base, "difficulty": "简单"}, ("difficulty",)),
        ("error_type 非法", {**base, "error_type": "guessed"}, ("error_type",)),
        ("confidence 越界", {**base, "confidence": 1.5}, ("confidence",)),
        (
            "confidence 不是数字",
            {**base, "confidence": "0.8"},
            ("confidence",),
        ),
    ]

    problems: list[str] = []
    for label, payload, expected in cases:
        try:
            message = validate(payload)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{label}：校验器抛异常 {type(exc).__name__}: {exc}")
            continue

        if expected is None:
            if message is not None:
                problems.append(f"{label}：应通过，实际返回 {message!r}")
            continue

        if not isinstance(message, str):
            problems.append(f"{label}：应返回错误字符串，实际 {message!r}")
            continue

        lacking = [part for part in expected if part not in message]
        if lacking:
            problems.append(f"{label}：错误信息缺少 {lacking}，实际 {message!r}")

    if problems:
        report.fail(
            f"校验器：{len(cases)} 个用例中有 {len(problems)} 个不符合预期",
            "\n".join(problems[:8]),
        )
    else:
        report.ok(
            f"校验器：{len(cases)} 个用例全部符合预期（拦截 + 错误原因）"
        )


# ============================================================
# 未配置密钥（不联网，只检查配置读取）
# ============================================================
def check_config(report: Report) -> None:
    """三项必填清空时，配置检查要明确列出缺项且不抛异常。

    当前 main 只有 app/stubs.py 的配置读取；B 的 llm_client 合并后自动切到它，
    两边都有 missing_config_keys() / is_configured()。
    """
    import os
    from unittest.mock import patch

    required = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")
    try:
        from app import stubs
    except Exception as exc:  # noqa: BLE001
        report.fail("配置缺失：无法导入 app/stubs.py", f"{type(exc).__name__}: {exc}")
        return

    source = stubs
    source_name = "stubs"
    try:
        from app import llm_client  # 可选：feature/B 合并后才存在
        if callable(getattr(llm_client, "missing_config_keys", None)):
            source = llm_client
            source_name = "llm_client"
    except Exception:  # noqa: BLE001 —— 模块还没合并时正常回落
        pass

    with patch.dict(os.environ, {key: "" for key in required}):
        try:
            missing = list(source.missing_config_keys())
            configured = bool(source.is_configured())
        except Exception as exc:  # noqa: BLE001
            report.fail(
                f"配置缺失：{source_name} 检查时抛异常（应当返回缺项）",
                f"{type(exc).__name__}: {exc}",
            )
            return

    if set(missing) != set(required):
        report.fail(
            f"配置缺失：{source_name} 返回的缺项与预期不符",
            f"预期 {list(required)}，实际 {missing}",
        )
    elif configured:
        report.fail(f"配置缺失：{source_name} 在缺三项时仍报告 is_configured()=True")
    else:
        report.ok(
            f"配置缺失：{source_name} 返回三项缺项且 is_configured()=False（未抛异常）"
        )


# ============================================================
# 建表脚本（用临时库，不碰 data/app.db）
# ============================================================
def check_schema(report: Report) -> None:
    """按 schema.sql 建一个临时库，确认六张表和外键开关。"""
    original_path = db.DB_PATH
    temp_path = DATA_DIR / f"_offline_check_{os.getpid()}.db"
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        temp_path.unlink(missing_ok=True)
        db.DB_PATH = temp_path
        db.init_db()

        conn = db.get_conn()
        try:
            fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        finally:
            conn.close()

        names = {row[0] for row in rows}
        missing = EXPECTED_TABLES - names
        if missing:
            report.fail("建表脚本：缺少表", "、".join(sorted(missing)))
        elif fk != 1:
            report.fail("建表脚本：外键开关未生效（PRAGMA foreign_keys != 1）")
        else:
            report.ok("建表脚本：六张表齐全，外键开关生效")
    except Exception as exc:  # noqa: BLE001 —— 体检脚本要把异常翻成一行结果
        report.fail("建表脚本：执行失败", f"{type(exc).__name__}: {exc}")
    finally:
        db.DB_PATH = original_path
        for suffix in ("", "-journal", "-wal", "-shm"):
            Path(str(temp_path) + suffix).unlink(missing_ok=True)


# ============================================================
# 本地数据库
# ============================================================
def check_database(report: Report, chapters: list[dict] | None, bank: list[dict] | None) -> None:
    """检查 data/app.db：能连上、表齐全、章节一致、没有孤儿引用、题库引用都能找到。"""
    db_path = DATA_DIR / "app.db"
    if not db_path.exists():
        report.fail(
            "数据库：找不到 data/app.db",
            "先运行 python seed_data.py 建库灌数据，再跑本脚本",
        )
        return

    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error as exc:
        report.fail("数据库：连接失败", f"{type(exc).__name__}: {exc}")
        return

    try:
        table_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        table_names = {row[0] for row in table_rows}
        missing = EXPECTED_TABLES - table_names
        if missing:
            report.fail("数据库：表不齐", "、".join(sorted(missing)))
        else:
            report.ok("数据库：连接成功，六张表齐全")

        counts = {}
        for table in ("chapter", "knowledge_point", "wrong_question", "wq_kp", "practice_log", "llm_log"):
            try:
                counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                counts[table] = 0
        report.ok(
            "数据库内容："
            f"章节 {counts['chapter']} · 知识点 {counts['knowledge_point']} · "
            f"错题 {counts['wrong_question']} · 练习 {counts['practice_log']} · "
            f"调用日志 {counts['llm_log']}"
        )
        report.ok(
            f"知识点（运行时生成）：{counts['knowledge_point']} 条"
            "（本项目不预置知识点文件，符合设计）"
        )

        if chapters is not None:
            db_chapters = {
                row[0] for row in conn.execute("SELECT id FROM chapter").fetchall()
            }
            file_chapters = {c["id"] for c in chapters}
            if db_chapters != file_chapters:
                report.fail(
                    "章节一致性：数据库与 data/chapters.json 不一致",
                    f"库中独有：{sorted(db_chapters - file_chapters) or '无'}；"
                    f"文件独有：{sorted(file_chapters - db_chapters) or '无'}",
                )
            else:
                report.ok("章节一致性：数据库与 data/chapters.json 一致")

        orphans = conn.execute("PRAGMA foreign_key_check").fetchall()
        if orphans:
            detail = "\n".join(
                f"{row[0]} rowid={row[1]} 指向不存在的 {row[2]}" for row in orphans[:5]
            )
            report.fail(f"外键完整性：{len(orphans)} 条孤儿引用", detail)
        else:
            report.ok("外键完整性：没有孤儿引用")

        if bank is not None:
            kp_ids = {
                row[0] for row in conn.execute("SELECT id FROM knowledge_point").fetchall()
            }
            referenced = {
                kp_id
                for item in bank
                for kp_id in (item.get("kp_ids") or [])
                if isinstance(kp_id, str)
            }
            missing_kps = sorted(referenced - kp_ids)
            if missing_kps:
                report.fail(
                    f"题库知识点引用：{len(missing_kps)} 个 kp_id 在数据库里不存在",
                    "、".join(missing_kps[:8]),
                )
            else:
                report.ok(f"题库知识点引用：{len(referenced)} 个 kp_id 均存在")
    except sqlite3.Error as exc:
        report.fail("数据库：查询失败", f"{type(exc).__name__}: {exc}")
    finally:
        conn.close()


# ============================================================
# 入口
# ============================================================
def main() -> int:
    report = Report()
    print("🔎 学习辅导 AI 助教 · 离线自检")
    print("=" * 60)

    chapters = load_chapters(report)
    bank = load_question_bank(report, chapters)
    check_validators(report, chapters)
    check_config(report)
    check_schema(report)
    check_database(report, chapters, bank)

    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
