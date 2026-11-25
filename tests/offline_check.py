"""离线自检 —— 不联网、不需要模型服务，先确认「地基」是好的。

维护人：C₂。契约：docs/interfaces.md（本脚本不调用任何业务函数，只做体检）。

用法：

    python tests/offline_check.py

覆盖范围（第 5 步初版 + 第 7 步校验器 + 第 8 步降级/落库原型）：

    □ 知识框架数据 data/chapters.json 能加载、条数与 id 规范
    □ 题库数据 data/question_bank.json 能加载、结构/章节引用合法
    □ schema.sql 能建出六张表，且每个连接的外键开关真的生效
    □ 本地 data/app.db 能连上、表齐全、章节与数据文件一致
    □ 数据库里没有指向不存在记录的孤儿引用（外键完整性）
    □ 题库里的 kp_id 都能在数据库中找到
    □ 错题抽取校验器：非法 JSON/缺字段/越界 id/错误枚举/越界 confidence 都被拦住
    □ 未配置密钥：明确列出缺项，is_configured() 为 False，且不抛异常
    □ 降级链路：假模型连续失败 → 带原因重试 2 次 → 降级（并验证第 2 次成功即采用）
    □ 落库原型：已有 id 过滤、new_concepts 查重、非法章节/ID 丢弃后写入临时库
    □ 掌握度：LEFT JOIN 全量、三因子、最终封顶、四档、排序、weak_points

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
# 降级链路（用假模型模拟 B 的 llm_client.chat 重试行为）
# ============================================================
def _run_retry_chain(generate, validator, max_retry: int = 2, label: str = "") -> dict:
    """模拟 chat(validator=..., max_retry=2) 的重试语义。

    每次失败都把 validator 返回的原因交给下一次 generate（对应生产里
    把错误原因追加进 messages）；连续 max_retry 次仍失败就返回降级结果。
    这只是测试替身，生产重试以 B 的 llm_client.chat 为准。
    """
    reasons: list[str] = []
    for attempt in range(max_retry + 1):
        last_reason = reasons[-1] if reasons else None
        raw = generate(attempt, last_reason)
        problem = validator(raw)
        if problem is None:
            print(f"   [{label}] 第 {attempt + 1} 次通过 → 采用")
            return {
                "ok": True,
                "data": raw,
                "attempts": attempt + 1,
                "reasons": reasons,
                "degraded": False,
            }

        reasons.append(problem)
        if attempt < max_retry:
            print(f"   [{label}] 第 {attempt + 1} 次校验失败：{problem}")
            print(f"   [{label}] → 回喂原因，开始第 {attempt + 2} 次")
        else:
            print(f"   [{label}] 第 {attempt + 1} 次校验失败：{problem} → 降级")

    return {
        "ok": False,
        "data": None,
        "attempts": max_retry + 1,
        "reasons": reasons,
        "degraded": True,
    }


def check_retry_chain(report: Report, chapters: list[dict] | None) -> None:
    """验证「失败 → 带原因重试 2 次 → 降级」和「第 2 次成功即采用」。"""
    try:
        from app import schemas
    except Exception as exc:  # noqa: BLE001
        report.fail("降级链路：无法导入 app/schemas.py", f"{type(exc).__name__}: {exc}")
        return

    allowed_kps = ["kp_a"]
    allowed_chapters = (
        [chapter["id"] for chapter in chapters]
        if chapters
        else ["ch_function_limit"]
    )
    try:
        validate = schemas.make_intake_validator(allowed_kps, allowed_chapters)
    except Exception as exc:  # noqa: BLE001
        report.fail("降级链路：构造校验器失败", f"{type(exc).__name__}: {exc}")
        return

    valid = {
        "question_text": "求极限",
        "student_answer": "1",
        "correct_answer": "3",
        "solution_steps": ["步骤一"],
        "knowledge_ids": ["kp_a"],
        "new_concepts": [],
        "difficulty": "easy",
        "error_type": "method_wrong",
        "confidence": 0.82,
    }
    missing = dict(valid)
    missing.pop("error_type")
    missing.pop("confidence")

    degrade_calls: list[tuple[int, str | None]] = []

    def degrade_generate(attempt: int, last_reason: str | None):
        degrade_calls.append((attempt, last_reason))
        if attempt == 0:
            return missing
        return {**valid, "knowledge_ids": ["kp_not_exist"]}

    try:
        degraded = _run_retry_chain(degrade_generate, validate, label="降级")
    except Exception as exc:  # noqa: BLE001
        report.fail("降级链路：降级用例抛异常", f"{type(exc).__name__}: {exc}")
        return

    problems: list[str] = []
    if degraded["ok"] or not degraded["degraded"]:
        problems.append(f"连续失败后应降级，实际 ok={degraded['ok']}")
    if degraded["attempts"] != 3:
        problems.append(f"应调用 3 次（首发 + 2 次重试），实际 {degraded['attempts']}")
    if len(degraded["reasons"]) != 3:
        problems.append(f"应记录 3 条失败原因，实际 {len(degraded['reasons'])}")
    if len(degrade_calls) != 3:
        problems.append(f"应调用假模型 3 次，实际 {len(degrade_calls)}")
    else:
        if not degrade_calls[1][1] or "缺少必填字段" not in degrade_calls[1][1]:
            problems.append(f"第 2 次没有收到第 1 次的原因：{degrade_calls[1][1]!r}")
        if not degrade_calls[2][1] or "knowledge_ids" not in degrade_calls[2][1]:
            problems.append(f"第 3 次没有收到第 2 次的原因：{degrade_calls[2][1]!r}")

    success_calls: list[tuple[int, str | None]] = []

    def recover_generate(attempt: int, last_reason: str | None):
        success_calls.append((attempt, last_reason))
        return missing if attempt == 0 else dict(valid)

    try:
        recovered = _run_retry_chain(recover_generate, validate, label="恢复")
    except Exception as exc:  # noqa: BLE001
        report.fail("降级链路：成功用例抛异常", f"{type(exc).__name__}: {exc}")
        return

    if not recovered["ok"] or recovered["degraded"]:
        problems.append(f"第 2 次合格时应采用，实际 ok={recovered['ok']}")
    if recovered["attempts"] != 2:
        problems.append(f"成功用例应调用 2 次，实际 {recovered['attempts']}")
    if len(recovered["reasons"]) != 1:
        problems.append(f"成功用例应记录 1 条原因，实际 {len(recovered['reasons'])}")
    if len(success_calls) != 2 or not success_calls[1][1]:
        problems.append("成功用例的第 2 次没有收到第 1 次的原因")
    elif "缺少必填字段" not in success_calls[1][1]:
        problems.append(f"成功用例回喂的原因不对：{success_calls[1][1]!r}")

    if problems:
        report.fail(f"降级链路：{len(problems)} 处不符合预期", "\n".join(problems))
    else:
        report.ok("降级链路：3 次调用后降级，2 条原因均已回喂")
        report.ok("降级链路：第 1 次失败后带原因重试，第 2 次通过即采用")


# ============================================================
# 落库原型（不改 db.py / intake.py，用临时库验证三步解析逻辑）
# ============================================================
def check_insert_prototype(report: Report, chapters: list[dict] | None) -> None:
    """验证给 B/C₁ 的补丁方案：已有 id 过滤、new_concepts 查重、非法项丢弃。

    生产落点应由 B 的 intake.commit() 采用这段 resolve 逻辑；本测试只在临时库里
    调用现成的 db.insert_wrong_question()，不改任何队友文件。
    """
    if not chapters:
        report.fail("落库原型：缺少章节数据，无法测试")
        return
    try:
        from app import knowledge
    except Exception as exc:  # noqa: BLE001
        report.fail("落库原型：无法导入 app/knowledge.py", f"{type(exc).__name__}: {exc}")
        return

    original_path = db.DB_PATH
    temp_path = DATA_DIR / f"_offline_insert_{os.getpid()}.db"
    try:
        temp_path.unlink(missing_ok=True)
        db.DB_PATH = temp_path
        db.init_db()
        db.insert_chapters(chapters)
        existing_id = knowledge.find_or_create(
            "原型已有知识点", chapters[0]["id"], context=None
        )

        def resolve(data: dict) -> list[str]:
            """三步：已有 id 过滤 → new_concepts 查重 → 非法项丢弃。"""
            existing = {row["id"] for row in db.list_kps()}
            resolved: list[str] = []

            for kp_id in data.get("knowledge_ids") or []:
                if (
                    isinstance(kp_id, str)
                    and kp_id in existing
                    and kp_id not in resolved
                ):
                    resolved.append(kp_id)

            for concept in data.get("new_concepts") or []:
                if not isinstance(concept, dict):
                    continue
                name = concept.get("name")
                chapter_id = concept.get("chapter_id")
                if not isinstance(name, str) or not name.strip():
                    continue
                if not isinstance(chapter_id, str) or not chapter_id.strip():
                    continue
                try:
                    kp_id = knowledge.find_or_create(name, chapter_id, context=None)
                except ValueError:
                    # 越界 chapter_id / 空名字：丢弃，不阻断整条入库
                    continue
                if kp_id not in resolved:
                    resolved.append(kp_id)

            return resolved

        data = {
            "question_text": "原型题目",
            "student_answer": "1",
            "correct_answer": "3",
            "solution_steps": ["步骤一"],
            "error_type": "method_wrong",
            "difficulty": "easy",
            "source": "text",
            "knowledge_ids": [existing_id, "kp_missing", existing_id],
            "new_concepts": [
                {"name": "原型新知识点", "chapter_id": chapters[0]["id"]},
                {"name": "原型越界知识点", "chapter_id": "ch_not_exist"},
                {"name": "", "chapter_id": chapters[0]["id"]},
                "不是对象",
            ],
        }
        resolved = resolve(data)
        wq_id = db.insert_wrong_question({**data, "kp_ids": resolved})
        recent = db.recent_wrong_questions(1)

        problems: list[str] = []
        if wq_id <= 0:
            problems.append(f"insert_wrong_question 返回了非法 id：{wq_id}")
        if len(resolved) != 2 or existing_id not in resolved:
            problems.append(f"解析结果应为 2 个 kp_id（含已有 id），实际 {resolved}")
        if "kp_missing" in resolved:
            problems.append("非法已有 id 没被丢弃")
        if not recent or recent[0]["question_text"] != "原型题目":
            problems.append("临时库里没有查到刚写入的错题")
        elif len(recent[0]["knowledge_points"]) != 2:
            problems.append(
                "wq_kp 关联数应为 2，实际 "
                f"{len(recent[0]['knowledge_points'])}"
            )
        kp_names = {row["name"] for row in db.list_kps()}
        if "原型新知识点" not in kp_names:
            problems.append("合法的 new_concept 没有长成知识点")
        if "原型越界知识点" in kp_names:
            problems.append("越界 chapter_id 的新概念被错误创建")

        if problems:
            report.fail(f"落库原型：{len(problems)} 处不符合预期", "\n".join(problems))
        else:
            report.ok(
                "落库原型：非法 id 丢弃、new_concepts 查重、越界章节丢弃，"
                f"错题 #{wq_id} 入库并挂 2 个知识点"
            )
    except Exception as exc:  # noqa: BLE001
        report.fail("落库原型：执行失败", f"{type(exc).__name__}: {exc}")
    finally:
        db.DB_PATH = original_path
        for suffix in ("", "-journal", "-wal", "-shm"):
            Path(str(temp_path) + suffix).unlink(missing_ok=True)


# ============================================================
# 掌握度与 weak_points（临时库，不碰 data/app.db）
# ============================================================
def check_mastery(report: Report, chapters: list[dict] | None) -> None:
    """按契约第七节造数据，验证三因子、封顶、四档、排序与 weak_points。"""
    if not chapters:
        report.fail("掌握度：缺少章节数据，无法测试")
        return
    try:
        from app import knowledge, profile
    except Exception as exc:  # noqa: BLE001
        report.fail("掌握度：无法导入 app/profile.py", f"{type(exc).__name__}: {exc}")
        return

    original_path = db.DB_PATH
    temp_path = DATA_DIR / f"_offline_mastery_{os.getpid()}.db"
    try:
        temp_path.unlink(missing_ok=True)
        db.DB_PATH = temp_path
        db.init_db()
        db.insert_chapters(chapters)
        chapter_id = chapters[0]["id"]

        def make_kp(name: str) -> str:
            return knowledge.find_or_create(name, chapter_id, context=None)

        weak = make_kp("测试薄弱知识点")
        few = make_kp("测试样本不足知识点")
        basic = make_kp("测试基本掌握")
        hard = make_kp("测试难题知识点")
        decayed = make_kp("测试衰减知识点")
        capped = make_kp("测试封顶知识点")
        never = make_kp("测试未练知识点")

        # 10 条、对 3 条、全易题 → 0.30，未掌握
        for index in range(10):
            db.log_practice(weak, index < 3, difficulty="easy")

        # 2 条、对 1 条 → 样本不足，不参与最薄弱排名
        for index in range(2):
            db.log_practice(few, index < 1, difficulty="easy")

        # 3 条、对 2 条 → 0.6667，基本掌握
        for index in range(3):
            db.log_practice(basic, index < 2, difficulty="easy")

        # 4 条、对 3 条、含 1 道难题 → 0.75 × 1.15 = 0.8625
        for _ in range(2):
            db.log_practice(hard, True, difficulty="easy")
        db.log_practice(hard, True, difficulty="hard")
        db.log_practice(hard, False, difficulty="easy")

        # 3 条全对，但上次练习在 40 天前 → 时间衰减 0.75
        for _ in range(3):
            db.log_practice(decayed, True, difficulty="easy")
        conn = sqlite3.connect(str(temp_path))
        try:
            conn.execute(
                "UPDATE practice_log SET created_at = datetime('now','localtime','-40 days')"
                " WHERE kp_id = ?",
                (decayed,),
            )
            conn.commit()
        finally:
            conn.close()

        # 4 条全对、全难题 → 1.0 × 1.15 = 1.15，最终截断到 1.0
        for _ in range(4):
            db.log_practice(capped, True, difficulty="hard")

        # 未练知识点：一条练习都没有，但必须出现在结果里（LEFT JOIN）
        df = profile.mastery_table()

        problems: list[str] = []
        if list(df.columns) != profile.MASTERY_COLUMNS:
            problems.append(f"列名/顺序不符：{list(df.columns)}")
        if len(df) != 7:
            problems.append(f"应有 7 个知识点（含未练过的），实际 {len(df)}")

        rows = {row["kp_name"]: row for _, row in df.iterrows()}

        def near(value, expected) -> bool:
            return abs(float(value) - float(expected)) < 1e-6

        weak_row = rows.get("测试薄弱知识点")
        if weak_row is None:
            problems.append("缺少「测试薄弱知识点」行")
        else:
            if weak_row["attempts"] != 10 or weak_row["correct"] != 3:
                problems.append(f"薄弱知识点次数不对：{weak_row['attempts']}/{weak_row['correct']}")
            if not near(weak_row["correct_rate"], 0.30):
                problems.append(f"基础正确率应为 0.30，实际 {weak_row['correct_rate']}")
            if not near(weak_row["mastery"], 0.30) or weak_row["status"] != "未掌握":
                problems.append(f"薄弱知识点掌握度/状态不对：{weak_row['mastery']}/{weak_row['status']}")

        few_row = rows.get("测试样本不足知识点")
        if few_row is None:
            problems.append("缺少「测试样本不足知识点」行")
        else:
            if few_row["status"] != "样本不足":
                problems.append(f"2 条记录应为样本不足，实际 {few_row['status']}")
            if df["kp_name"].tolist().index("测试样本不足知识点") == 0:
                problems.append("样本不足的知识点排到了第一，会霸占最薄弱榜")

        basic_row = rows.get("测试基本掌握")
        if basic_row is None or basic_row["status"] != "基本掌握":
            problems.append("0.6667 应为基本掌握")

        hard_row = rows.get("测试难题知识点")
        if hard_row is None:
            problems.append("缺少「测试难题知识点」行")
        else:
            if not near(hard_row["difficulty_factor"], 1.15):
                problems.append(f"含难题的难度系数应为 1.15，实际 {hard_row['difficulty_factor']}")
            if not near(hard_row["mastery"], 0.8625):
                problems.append(f"难题掌握度应为 0.8625，实际 {hard_row['mastery']}")
            if not float(hard_row["mastery"]) > 0.75:
                problems.append("含难题的掌握度没有高于同批全易题（0.75）")

        decayed_row = rows.get("测试衰减知识点")
        if decayed_row is None:
            problems.append("缺少「测试衰减知识点」行")
        else:
            if not near(decayed_row["decay_factor"], 0.75):
                problems.append(f"40 天前应衰减到 0.75，实际 {decayed_row['decay_factor']}")
            if not near(decayed_row["mastery"], 0.75):
                problems.append(f"衰减后掌握度应为 0.75，实际 {decayed_row['mastery']}")

        capped_row = rows.get("测试封顶知识点")
        if capped_row is None:
            problems.append("缺少「测试封顶知识点」行")
        else:
            if not near(capped_row["mastery"], 1.0):
                problems.append(f"最终掌握度应封顶 1.0，实际 {capped_row['mastery']}")
            if not near(capped_row["difficulty_factor"], 1.15):
                problems.append("难度系数不应被提前封顶（应保持 1.15）")

        never_row = rows.get("测试未练知识点")
        if never_row is None:
            problems.append("未练过的知识点没有出现在结果里（LEFT JOIN 失效）")
        else:
            if never_row["attempts"] != 0:
                problems.append("未练过的 attempts 应为 0")
            if not near(never_row["mastery"], 0.0) or never_row["status"] != "样本不足":
                problems.append("未练过的行应为 0.0 / 样本不足")

        rank = {"未掌握": 0, "基本掌握": 1, "已掌握": 2, "样本不足": 3}
        ranks = [rank.get(status, 99) for status in df["status"]]
        if ranks != sorted(ranks):
            problems.append(f"档位排序不对：{df['status'].tolist()}")
        if df.iloc[0]["kp_name"] != "测试薄弱知识点":
            problems.append(f"第一行应是薄弱知识点，实际 {df.iloc[0]['kp_name']}")

        top3 = profile.weak_points(3)
        if len(top3) != 3:
            problems.append(f"weak_points(3) 应返回 3 条，实际 {len(top3)}")
        if any(item["status"] == "样本不足" for item in top3):
            problems.append("weak_points 里混进了样本不足")
        if top3 and top3[0]["kp_name"] != "测试薄弱知识点":
            problems.append(f"weak_points 第一名不对：{top3[0]['kp_name']}")
        masteries = [float(item["mastery"]) for item in top3]
        if masteries != sorted(masteries):
            problems.append("weak_points 没有按掌握度升序")
        if len(profile.weak_points(10)) != 5:
            problems.append("weak_points(10) 应返回全部 5 个非样本不足知识点")
        if profile.weak_points(0) != []:
            problems.append("weak_points(0) 应为空")

        if problems:
            report.fail(f"掌握度：{len(problems)} 处不符合预期", "\n".join(problems[:8]))
        else:
            report.ok(
                "掌握度：LEFT JOIN 全量、0.30/未掌握、样本不足不霸榜、"
                "难题 0.8625、衰减 0.75、封顶 1.0、weak_points 排序全部正确"
            )
    except Exception as exc:  # noqa: BLE001
        report.fail("掌握度：执行失败", f"{type(exc).__name__}: {exc}")
    finally:
        db.DB_PATH = original_path
        for suffix in ("", "-journal", "-wal", "-shm"):
            Path(str(temp_path) + suffix).unlink(missing_ok=True)


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
    check_retry_chain(report, chapters)
    check_insert_prototype(report, chapters)
    check_mastery(report, chapters)
    check_schema(report)
    check_database(report, chapters, bank)

    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
