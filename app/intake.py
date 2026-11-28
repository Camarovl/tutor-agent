"""功能② 错题录入与归类 —— 模型抽取 → 人工确认 → 落库。

维护人：B
契约：docs/interfaces.md 第五节

三条硬性约定：
1. 🔴 识别结果**永远**返回一个 IntakeDraft，失败也不抛异常 ——
   界面在 st.button 回调里直接调用它，抛出去就是整页 traceback。
   失败时 degraded=True + note 说明原因，界面显示空表单让人手填。
2. 🔴 AI 不直接写库：模型只给"建议的知识点"，是否真的新建由
   knowledge.find_or_create() 查重后决定。
3. 🔴 只有用户点「确认入库」才会调 commit()。
"""

from __future__ import annotations

import hashlib
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

from app import db, knowledge, llm_client, prompts

#: C₂ 的校验器（已随 main 落地）。导入失败不影响本模块可用性 ——
#: 退回到本文件的 _build_validator() 兜底，两条路径规则一致（见契约第六节）。
try:
    from app.schemas import make_intake_validator as _schemas_make_intake_validator
except Exception:  # noqa: BLE001 —— 未落地 / 导入期报错都退兜底
    _schemas_make_intake_validator = None

# ============================================================
# 注入阈值 —— 改这两个数只影响提示词长度，不影响数据
# ============================================================

#: 知识点候选的注入总量阈值。
#: 库里 ≤ 这个数时全量注入（最简单也最准，几十个知识点约几百 token）；
#: 超过之后改按题干相关性取前 _KP_INJECT_LIMIT 条，
#: 避免提示词随知识点增长无限膨胀、模型在长列表里挑错。
_KP_INJECT_ALL_BELOW = 80

#: 超过阈值时注入的条数上限（约 300 token）
_KP_INJECT_LIMIT = 30

#: 是否按"题干内容"做错题去重。
#: True  → 同一道题重复录入会合并成一行，error_count +1（走 db.upsert_wrong_question_by_source）
#: False → 每次都插一条新记录（决策 3 的字面行为）
_DEDUPE_BY_CONTENT = True

#: 契约第五节的两个枚举，顺序与 docs/interfaces.md 一致
ERROR_TYPES = (
    "concept_confusion",
    "calculation_error",
    "method_wrong",
    "incomplete",
    "unknown",
)
DIFFICULTIES = ("easy", "medium", "hard")

#: knowledge_ids 最多几个（契约限制）
_MAX_KNOWLEDGE_IDS = 3

#: solution_steps 最少几步（少于这个数视为识别失败，会被校验器打回）
_MIN_SOLUTION_STEPS = 2


# ============================================================
# 草稿数据结构 —— 字段与契约第五节一一对应
# ============================================================
@dataclass
class IntakeDraft:
    """一次识别的结果。

    界面按属性逐项渲染（见 streamlit_app.py 的②页），所以字段名不能改。
    """

    question_text: str = ""
    student_answer: str = ""
    correct_answer: str = ""
    solution_steps: list[str] = field(default_factory=list)
    knowledge_ids: list[str] = field(default_factory=list)
    new_concepts: list[dict] = field(default_factory=list)
    difficulty: str = "medium"
    error_type: str = "unknown"
    confidence: float = 0.0
    source: str = "text"
    degraded: bool = False
    note: str = ""

    #: 扩展字段（契约之外，界面目前不读）：学生答案与参考答案看起来一致时置 True。
    #: 作用有两个：commit() 用它拒绝入库；界面将来可以用它隐藏「确认入库」按钮。
    looks_correct: bool = False


def _warn(detail: str) -> None:
    """技术细节出口：只写 stderr，不进异常 message、也不进界面备注。"""
    try:
        print(f"[intake] {detail}", file=sys.stderr)
    except Exception:  # pragma: no cover —— stderr 不可用时不影响主流程
        pass


def _degraded(reason: str, source: str = "text", *, looks_correct: bool = False) -> IntakeDraft:
    """识别失败时的空草稿：字段全空、degraded=True，界面据此显示可手填的空表单。"""
    return IntakeDraft(
        question_text="",
        student_answer="",
        correct_answer="",
        solution_steps=[],
        knowledge_ids=[],
        new_concepts=[],
        difficulty="medium",
        error_type="unknown",
        confidence=0.0,
        source=source,
        degraded=True,
        note=reason,
        looks_correct=looks_correct,
    )


# ============================================================
# 校验器
# ============================================================
#: C₂ 的 app/schemas.py 已随 main 落地，默认走它的 make_intake_validator。
#: 本文件的 _build_validator() 只在「schemas 导入失败」或「构造时读不到章节/知识点」时兜底。
#: ⚠️ 两条路径的规则必须一致（契约第六节拦截项）；C₂ 的版本要求 solution_steps 非空即可，
#:    本文件兜底版要求 ≥2 步 —— 更严，但只在兜底时生效，不影响正常路径。


def _make_validator(
    allowed_kp_ids: set[str],
    allowed_chapter_ids: set[str],
) -> Callable[[dict], str | None]:
    """优先用 C₂ 的校验器；构造失败（库未建等）就退回本地兜底。"""
    if _schemas_make_intake_validator is not None:
        try:
            return _schemas_make_intake_validator(sorted(allowed_kp_ids))
        except Exception:  # noqa: BLE001 —— 读不到章节/知识点时不让录入整体挂掉
            pass
    return _build_validator(allowed_kp_ids, allowed_chapter_ids)


def _build_validator(
    allowed_kp_ids: set[str],
    allowed_chapter_ids: set[str],
) -> Callable[[dict], str | None]:
    """临时校验器：照契约第六节的拦截项实现。

    返回 None 表示合规；返回字符串表示不合规，这个字符串会被 chat() 回喂给模型重试。
    消息要写成"模型看得懂的指令"，不是给人看的报错。
    """

    def validate(data: dict) -> str | None:
        if not isinstance(data, dict):
            return "顶层必须是一个 JSON 对象"

        question_text = (data.get("question_text") or "").strip()
        if not question_text:
            return "question_text 不能为空，请填题干原文"

        if not (data.get("correct_answer") or "").strip():
            return "correct_answer 不能为空，请给出正确答案"

        steps = data.get("solution_steps")
        if not isinstance(steps, list) or len([s for s in steps if str(s).strip()]) < _MIN_SOLUTION_STEPS:
            return f"solution_steps 必须是数组且至少 {_MIN_SOLUTION_STEPS} 步"

        kp_ids = data.get("knowledge_ids")
        if kp_ids is None:
            kp_ids = []
        if not isinstance(kp_ids, list):
            return "knowledge_ids 必须是数组（没有合适的就填 []）"
        if len(kp_ids) > _MAX_KNOWLEDGE_IDS:
            return f"knowledge_ids 最多 {_MAX_KNOWLEDGE_IDS} 个，请只保留最相关的"
        unknown = [k for k in kp_ids if k not in allowed_kp_ids]
        if unknown:
            return (
                "knowledge_ids 里有不存在的 id：" + ", ".join(map(str, unknown))
                + "。只能填【已有知识点】里出现过的 id，没有合适的就填 []"
            )

        concepts = data.get("new_concepts") or []
        if not isinstance(concepts, list):
            return "new_concepts 必须是数组（没有建议就填 []）"
        for concept in concepts:
            if not isinstance(concept, dict):
                return 'new_concepts 的元素必须是 {"name": ..., "chapter_id": ...} 形式'
            if not (concept.get("name") or "").strip():
                return "new_concepts 里每一项都要有 name"
            chapter_id = (concept.get("chapter_id") or "").strip()
            if chapter_id not in allowed_chapter_ids:
                return (
                    f"new_concepts 里的 chapter_id={chapter_id!r} 不在【章节候选】里。"
                    "只能从候选里选，禁止发明新章节"
                )

        if data.get("error_type") not in ERROR_TYPES:
            return "error_type 只能是：" + " / ".join(ERROR_TYPES)

        if data.get("difficulty") not in DIFFICULTIES:
            return "difficulty 只能是：" + " / ".join(DIFFICULTIES)

        confidence = data.get("confidence")
        if confidence is not None:
            try:
                value = float(confidence)
            except (TypeError, ValueError):
                return "confidence 必须是 0 到 1 之间的小数"
            if not 0.0 <= value <= 1.0:
                return "confidence 必须是 0 到 1 之间的小数"

        return None

    return validate


# ============================================================
# 候选注入
# ============================================================
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_]*")


def _grams(text: str) -> set[str]:
    """汉字 2-gram + 英文单词，用于相关性粗排（与 qa.retrieve_knowledge 同一套思路）。"""
    raw = (text or "").lower()
    grams: set[str] = set()
    for segment in _CJK_RE.findall(raw):
        if len(segment) == 1:
            grams.add(segment)
        for i in range(len(segment) - 1):
            grams.add(segment[i : i + 2])
    for word in _ASCII_RE.findall(raw):
        if len(word) >= 2:
            grams.add(word)
    return grams


def _candidate_kps(question_text: str, points: list[dict] | None = None) -> list[dict]:
    """给提示词用的知识点候选：库小就全量，库大就按题干相关性取前 N 条。

    points 参数允许调用方把 already 查好的全量列表传进来，避免重复查库。
    """
    points = knowledge.all_knowledge_points() if points is None else points
    if len(points) <= _KP_INJECT_ALL_BELOW:
        return points

    target = _grams(question_text)
    if not target:
        return points[:_KP_INJECT_LIMIT]

    def score(point: dict) -> int:
        name = point.get("name") or ""
        return len(target & _grams(name))

    ranked = sorted(points, key=lambda p: (-score(p), p.get("name") or ""))
    return ranked[:_KP_INJECT_LIMIT]


# ============================================================
# 答案比对（判断"疑似做对"）
# ============================================================
_SPACE_RE = re.compile(r"\s+")
_MULTIPLY_RE = re.compile(r"[·×*]")
_MINUS_RE = re.compile(r"[−–]")


def _normalize_answer(text: str) -> str:
    """把答案规范化到可直接字符串比较的程度。

    只做"显然等价"的变换：去空格、统一乘号/减号、小写、去掉句末句号。
    🔴 不做数学等价判断（x(x-1)e^x 与 (x-1)e^x 等价，但这里比不出来）——
       宁可漏判"做对"，也不要误判，否则真正做错的题会被拦在错题本外。
    """
    value = (text or "").strip().lower()
    value = _SPACE_RE.sub("", value)
    value = _MULTIPLY_RE.sub("*", value)
    value = _MINUS_RE.sub("-", value)
    return value.rstrip("。.;；")


def _looks_correct(student_answer: str, correct_answer: str) -> bool:
    """两边都非空且规范化后完全一致 → 视为"疑似做对"。"""
    left = _normalize_answer(student_answer)
    right = _normalize_answer(correct_answer)
    return bool(left) and left == right


# ============================================================
# 模型输出 → 草稿
# ============================================================
def _as_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ("" if value is None else str(value).strip())


def _as_steps(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [line.strip() for line in value.splitlines() if line.strip()]
    return []


def _draft_from_data(
    data: dict,
    *,
    question_text: str,
    student_answer: str,
    correct_answer: str,
    source: str,
    extra_note: str = "",
) -> IntakeDraft:
    """把模型返回的 dict 整理成草稿：不合法的字段就地降级，并在 note 里说明。"""
    notes: list[str] = []

    allowed_kp_ids = {point["id"] for point in knowledge.all_knowledge_points() if point.get("id")}
    kp_ids = [kid for kid in (data.get("knowledge_ids") or []) if kid in allowed_kp_ids]
    dropped_kp = len(data.get("knowledge_ids") or []) - len(kp_ids)
    if dropped_kp > 0:
        notes.append(f"丢弃了 {dropped_kp} 个不存在的知识点 id")

    chapter_ids = {row["id"] for row in knowledge.chapter_candidates() if row.get("id")}
    concepts: list[dict] = []
    for concept in data.get("new_concepts") or []:
        if not isinstance(concept, dict):
            continue
        name = _as_str(concept.get("name"))
        chapter_id = _as_str(concept.get("chapter_id"))
        if name and chapter_id in chapter_ids:
            concepts.append({"name": name, "chapter_id": chapter_id})
        elif name:
            notes.append(f"丢弃了建议知识点「{name}」：章节 {chapter_id or '(空)'} 不合法")

    error_type = data.get("error_type")
    if error_type not in ERROR_TYPES:
        notes.append(f"错因 {error_type!r} 不在枚举内，已改成 unknown")
        error_type = "unknown"

    difficulty = data.get("difficulty")
    if difficulty not in DIFFICULTIES:
        notes.append(f"难度 {difficulty!r} 不合法，已改成 medium")
        difficulty = "medium"

    try:
        confidence = float(data.get("confidence") if data.get("confidence") is not None else 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(max(confidence, 0.0), 1.0)

    final_question = _as_str(data.get("question_text")) or question_text
    final_student = _as_str(data.get("student_answer")) or student_answer
    final_correct = _as_str(data.get("correct_answer")) or correct_answer

    looks_correct = _looks_correct(final_student, final_correct)
    if looks_correct:
        notes.append("学生答案与参考答案一致：疑似已做对，建议核对后不要归档")

    if extra_note.strip():
        notes.append(extra_note.strip())

    return IntakeDraft(
        question_text=final_question,
        student_answer=final_student,
        correct_answer=final_correct,
        solution_steps=_as_steps(data.get("solution_steps")),
        knowledge_ids=kp_ids[:_MAX_KNOWLEDGE_IDS],
        new_concepts=concepts,
        difficulty=difficulty,
        error_type=error_type,
        confidence=confidence,
        source=source,
        degraded=False,
        note="；".join(notes),
        looks_correct=looks_correct,
    )


# ============================================================
# 对外接口：文本路径
# ============================================================
def analyze_text(
    question_text: str,
    student_answer: str = "",
    correct_answer: str = "",
) -> IntakeDraft:
    """手输路径。任何失败都返回 degraded 草稿，绝不抛异常。"""
    text = (question_text or "").strip()
    if not text:
        return _degraded("题干为空：至少写一句题干才能识别。", source="text")

    try:
        chapters = knowledge.chapter_candidates()
        all_points = knowledge.all_knowledge_points()
        points = _candidate_kps(text, all_points)
        # 校验用全部合法 id（候选只注入了前 N 条，不代表其他 id 不合法）
        allowed_kp_ids = {point["id"] for point in all_points if point.get("id")}
        allowed_chapter_ids = {row["id"] for row in chapters if row.get("id")}

        messages = prompts.build_intake_messages(
            question_text=text,
            student_answer=student_answer,
            correct_answer=correct_answer,
            chapters=chapters,
            known_points=points,
        )
        result = llm_client.chat(
            messages,
            purpose="intake",
            json_mode=True,
            schema_hint=prompts.INTAKE_SCHEMA_HINT,
            validator=_make_validator(allowed_kp_ids, allowed_chapter_ids),
        )
        data = llm_client.extract_json(result.text)
        if data is None:
            return _degraded("这次没能识别出内容，请手工填写。", source="text")
        return _draft_from_data(
            data,
            question_text=text,
            student_answer=student_answer,
            correct_answer=correct_answer,
            source="text",
        )
    except llm_client.LLMError as exc:
        # exc 的 message 已经是给用户看的人话；技术细节在 llm_client 里已写 stderr
        return _degraded(str(exc), source="text")
    except Exception as exc:  # noqa: BLE001 —— 界面没有 try，任何异常都会白屏
        _warn(f"识别过程出错：{type(exc).__name__}: {exc}")
        return _degraded("这次没能识别出来，请手工填写。", source="text")


# ============================================================
# 对外接口：图片路径
# ============================================================
def analyze_image(image_bytes: bytes, extra_note: str = "") -> IntakeDraft:
    """拍照路径。模型不支持图片、或识别失败时，降级成"请手输"的空草稿。"""
    if not image_bytes:
        return _degraded("没有收到图片内容，请重新上传。", source="image")

    try:
        chapters = knowledge.chapter_candidates()
        all_points = knowledge.all_knowledge_points()
        points = _candidate_kps(extra_note or "", all_points)
        allowed_kp_ids = {point["id"] for point in all_points if point.get("id")}
        allowed_chapter_ids = {row["id"] for row in chapters if row.get("id")}

        result = llm_client.vision(
            prompts.build_intake_image_user_text(extra_note),
            [image_bytes],
            purpose="intake",
            system=prompts.build_intake_system(chapters, points),
            json_mode=True,
            schema_hint=prompts.INTAKE_SCHEMA_HINT,
            validator=_make_validator(allowed_kp_ids, allowed_chapter_ids),
        )
        data = llm_client.extract_json(result.text)
        if data is None:
            return _degraded(
                "这次没能识别出内容，请核对后手输。",
                source="image",
            )
        return _draft_from_data(
            data,
            question_text="",
            student_answer="",
            correct_answer="",
            source="image",
        )
    except llm_client.VisionUnsupported:
        # 模型不支持图片：这是可预期的情况，不是故障（见 docs/limitations.md 第 13 条）
        return _degraded(
            "当前模型不接受图片输入，请改用手输题干。",
            source="image",
        )
    except llm_client.LLMError as exc:
        return _degraded(str(exc), source="image")
    except Exception as exc:  # noqa: BLE001
        _warn(f"图片识别出错：{type(exc).__name__}: {exc}")
        return _degraded("这次没能识别出来，请改用手输。", source="image")


# ============================================================
# 对外接口：落库
# ============================================================
def _resolve_kp_ids(draft: Any) -> list[str]:
    """把草稿上的知识点变成库里真实存在的 id。

    - knowledge_ids：用户/模型选中的已有知识点，过滤掉不存在的
    - new_concepts：模型建议新建的，交给 find_or_create() 查重后决定复用还是新建
      🔴 AI 不直接写库，这一句是那条红线的落点
    """
    kp_ids: list[str] = []

    existing = {point["id"] for point in knowledge.all_knowledge_points() if point.get("id")}
    for kp_id in getattr(draft, "knowledge_ids", []) or []:
        if kp_id in existing and kp_id not in kp_ids:
            kp_ids.append(kp_id)

    for concept in getattr(draft, "new_concepts", []) or []:
        if not isinstance(concept, dict):
            continue
        name = (concept.get("name") or "").strip()
        chapter_id = (concept.get("chapter_id") or "").strip()
        if not name:
            continue
        if not knowledge.ensure_valid_chapter(chapter_id):
            # 章节非法：宁可不建，也不要挂到不存在的章节下
            continue
        try:
            kp_id = knowledge.find_or_create(name, chapter_id, context=None)
        except Exception:  # noqa: BLE001 —— 建点失败不该拖垮入库
            continue
        if kp_id and kp_id not in kp_ids:
            kp_ids.append(kp_id)

    return kp_ids[:_MAX_KNOWLEDGE_IDS]


def _content_key(question_text: str) -> str:
    """按题干内容生成稳定的去重键（去空格、小写后取 sha1 前 16 位）。"""
    normalized = _SPACE_RE.sub("", (question_text or "").strip().lower())
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]
    return f"manual:{digest}"


def commit(draft: Any, image_path: str | None = None) -> int:
    """🔴 用户确认后才落库的唯一入口，返回错题记录 id。

    流程：草稿上的知识点 → 真实 kp_id（必要时新建）→ 写 wrong_question + wq_kp。

    🔴 疑似做对（looks_correct）时拒绝入库：
       这道题本来就不是错题，收进错题本只会污染"该练什么"的推荐。
       抛 ValueError 让界面显示原因 —— 这是最后一道防线。

    ⚠️ 判定是**调用这一刻重新比一次**，而不是读草稿上的 looks_correct 快照。
       原因：用户可能在表单里改过答案，快照会过期；
       改了之后两边不再相等，就应该允许入库（比如他想收录另一种解法）。
    """
    student_answer = (getattr(draft, "student_answer", "") or "").strip()
    correct_answer = (getattr(draft, "correct_answer", "") or "").strip()
    if _looks_correct(student_answer, correct_answer):
        raise ValueError(
            "这道题的学生答案与参考答案一致，看起来已经做对了，不需要归档。"
            "如果确实想收录（例如想记下更好的解法），请先改动其中一处再提交。"
        )

    question_text = (getattr(draft, "question_text", "") or "").strip()
    if not question_text:
        raise ValueError("题干为空，不能入库。")

    kp_ids = _resolve_kp_ids(draft)
    if not kp_ids:
        raise ValueError(
            "这道题还没有关联到知识点，请在「涉及知识点」里至少选一个。"
        )

    data = {
        "question_text": question_text,
        "student_answer": (getattr(draft, "student_answer", "") or "").strip(),
        "correct_answer": (getattr(draft, "correct_answer", "") or "").strip(),
        "solution_steps": getattr(draft, "solution_steps", []) or [],
        "error_type": getattr(draft, "error_type", "unknown") or "unknown",
        "difficulty": getattr(draft, "difficulty", "medium") or "medium",
        "source": getattr(draft, "source", "text") or "text",
        "kp_ids": kp_ids,
    }

    if _DEDUPE_BY_CONTENT:
        return db.upsert_wrong_question_by_source(_content_key(question_text), data, image_path)
    return db.insert_wrong_question(data, image_path)
