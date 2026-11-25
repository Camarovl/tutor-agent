"""大模型输出的校验器 —— 不合格的 JSON 不进业务层，先在这里被拦住。

维护人：C₂。契约：docs/interfaces.md 第五节「抽取输出契约」与第六节「校验器」。

为什么需要这一层：
    模型返回的 JSON 看起来像那么回事，但经常缺字段、把 difficulty 写成「简单」、
    或者自己发明一个知识点 id / 章节 id。直接落库会把统计口径弄脏，而且错误
    要等到掌握度页面才暴露。校验器把问题挡在入口，并把「具体哪里不对」回喂给
    模型重试（见 app/llm_client.py 的 chat(validator=...)）。

本文件只做纯校验：不读数据库、不发网络请求、不写库。
    - 默认的章节候选集是唯一例外：make_intake_validator() 不传
      allowed_chapter_ids 时，会懒加载 app.knowledge.chapter_candidates()；
      测试可以显式传入，避免依赖数据库。
"""

from __future__ import annotations

import math
from typing import Any, Callable

#: 模型必须输出的 9 个字段。缺任何一个都算不合格。
REQUIRED_FIELDS = (
    "question_text",
    "student_answer",
    "correct_answer",
    "solution_steps",
    "knowledge_ids",
    "new_concepts",
    "difficulty",
    "error_type",
    "confidence",
)

DIFFICULTIES = ("easy", "medium", "hard")

ERROR_TYPES = (
    "concept_confusion",
    "calculation_error",
    "method_wrong",
    "incomplete",
    "unknown",
)

MAX_KNOWLEDGE_IDS = 3


def _is_array(value: Any) -> bool:
    """JSON 数组解析出来是 list；这里也接受 tuple，方便测试直接构造。"""
    return isinstance(value, (list, tuple))


def _is_number(value: Any) -> bool:
    """bool 是 int 的子类，必须单独排除，否则 True 会被当成 1。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _default_chapter_ids() -> list[str]:
    """从知识层取 15 个章节 id。

    懒加载：只有调用方没传 allowed_chapter_ids 时才碰数据库，
    这样单元测试可以完全不依赖 data/app.db。
    """
    from . import knowledge  # 局部导入，避免 schemas 在导入期依赖数据库

    try:
        chapters = knowledge.chapter_candidates()
    except Exception as exc:  # noqa: BLE001 —— 把底层错误翻译成能看懂的话
        raise RuntimeError(
            "无法加载章节候选集：请先运行 python seed_data.py 建库灌入 15 个章节"
        ) from exc

    ids = [
        str(chapter["id"])
        for chapter in chapters
        if isinstance(chapter, dict) and chapter.get("id")
    ]
    if not ids:
        raise RuntimeError("章节候选集为空：请先运行 python seed_data.py")
    return ids


def make_intake_validator(
    allowed_kp_ids: Any,
    allowed_chapter_ids: Any = None,
) -> Callable[[Any], str | None]:
    """构造「错题抽取」校验器。

    参数
        allowed_kp_ids         现有知识点 id 列表/集合（取 knowledge.all_knowledge_points() 里的 id）
        allowed_chapter_ids    15 个章节 id；不传时从 knowledge.chapter_candidates() 取

    返回
        一个函数：输入模型解析后的 dict，合规返回 None，
        不合规返回一句中文错误描述（会被回喂给模型重试）。

    校验顺序与契约第六节一致：
        ① 是不是 JSON 对象
        ② 缺哪些必填字段（一次列全）
        ③ question_text 非空
        ④ solution_steps 是非空数组
        ⑤ knowledge_ids 是数组、最多 3 个、每一项都在候选集里
        ⑥ new_concepts 的 chapter_id 在 15 章内
        ⑦ difficulty / error_type 在枚举里
        ⑧ confidence 是 0~1 的数字

    🔴 knowledge_ids 越界是最关键的一条：如果允许模型自由发明知识点 id，
       「不定积分」「不定的积分」「求不定积分」会各建一条，掌握度统计作废。
    """
    if allowed_kp_ids is None:
        raise ValueError("allowed_kp_ids 不能为 None")
    allowed_kps = {str(kp_id) for kp_id in allowed_kp_ids}

    if allowed_chapter_ids is None:
        allowed_chapters = set(_default_chapter_ids())
    else:
        allowed_chapters = {str(chapter_id) for chapter_id in allowed_chapter_ids}
    if not allowed_chapters:
        raise ValueError("allowed_chapter_ids 不能为空")

    def validate(data: Any) -> str | None:
        # ① 顶层必须是对象。JSON 字符串、数组、None 都在这里被挡住。
        if not isinstance(data, dict):
            return f"输出必须是 JSON 对象（dict），实际是 {type(data).__name__}"

        # ② 缺字段一次列全，让模型一轮就能补齐，不用逐个试。
        missing = [name for name in REQUIRED_FIELDS if name not in data]
        if missing:
            return "缺少必填字段：" + "、".join(missing)

        # ③ 题干必须有内容；空白串也算空。
        question_text = data["question_text"]
        if not isinstance(question_text, str) or not question_text.strip():
            return "question_text 必须是非空字符串"

        # 学生答案/正确答案允许空串（拍照场景可能没有学生作答），但类型必须是字符串。
        for key in ("student_answer", "correct_answer"):
            if not isinstance(data[key], str):
                return f"{key} 必须是字符串（允许空串）"

        # ④ 解题步骤必须是非空数组，且每一项都是非空字符串。
        steps = data["solution_steps"]
        if not _is_array(steps) or not steps:
            return "solution_steps 必须是非空数组"
        if any(not isinstance(step, str) or not step.strip() for step in steps):
            return "solution_steps 的每一项都必须是非空字符串"

        # ⑤ 已有知识点 id：必须是数组，最多 3 个，且全部来自候选集。
        kp_ids = data["knowledge_ids"]
        if not _is_array(kp_ids):
            return "knowledge_ids 必须是数组"
        if len(kp_ids) > MAX_KNOWLEDGE_IDS:
            return f"knowledge_ids 最多 {MAX_KNOWLEDGE_IDS} 个，实际 {len(kp_ids)} 个"
        if any(not isinstance(kp_id, str) or not kp_id.strip() for kp_id in kp_ids):
            return "knowledge_ids 的每一项都必须是非空字符串"

        unknown = sorted({kp_id for kp_id in kp_ids if kp_id not in allowed_kps})
        if unknown:
            return (
                "knowledge_ids 里有不在候选列表内的值："
                + "、".join(unknown)
                + f"（只能选已有知识点 id，候选共 {len(allowed_kps)} 个）"
            )

        # ⑥ 建议新建的知识点：名字要能读，章节必须是 15 个之一。
        concepts = data["new_concepts"]
        if not _is_array(concepts):
            return "new_concepts 必须是数组"
        for index, concept in enumerate(concepts):
            if not isinstance(concept, dict):
                return f"new_concepts[{index}] 必须是对象"
            name = concept.get("name")
            if not isinstance(name, str) or not name.strip():
                return f"new_concepts[{index}].name 必须是非空字符串"
            chapter_id = concept.get("chapter_id")
            if not isinstance(chapter_id, str) or not chapter_id.strip():
                return f"new_concepts[{index}].chapter_id 必须是非空字符串"
            if chapter_id not in allowed_chapters:
                return (
                    f"new_concepts[{index}].chapter_id 不在 15 个章节内：{chapter_id!r}"
                    "（禁止发明新章节）"
                )

        # ⑦ 两个枚举值必须用英文，数据库和统计都按英文值走。
        difficulty = data["difficulty"]
        if not isinstance(difficulty, str) or difficulty not in DIFFICULTIES:
            return (
                "difficulty 必须是 " + " / ".join(DIFFICULTIES)
                + f" 之一，实际 {difficulty!r}"
            )

        error_type = data["error_type"]
        if not isinstance(error_type, str) or error_type not in ERROR_TYPES:
            return (
                "error_type 必须是五个枚举之一（"
                + " / ".join(ERROR_TYPES)
                + f"），实际 {error_type!r}"
            )

        # ⑧ confidence 必须是 0~1 的有限数字；字符串 "0.8" 和 bool 都不算。
        confidence = data["confidence"]
        if not _is_number(confidence):
            return f"confidence 必须是 0~1 之间的数字，实际 {confidence!r}"
        try:
            confidence_value = float(confidence)
        except (TypeError, ValueError, OverflowError):
            # 极大整数转 float 会 OverflowError；仍然按「不是合法数字」处理，别让校验器自己崩。
            return f"confidence 必须是 0~1 之间的数字，实际 {confidence!r}"
        if not math.isfinite(confidence_value) or not 0 <= confidence_value <= 1:
            return f"confidence 必须是 0~1 之间的数字，实际 {confidence!r}"

        return None

    return validate
