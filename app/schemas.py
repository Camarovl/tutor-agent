"""大模型输出的校验器 —— 不合格的 JSON 不进业务层，先在这里被拦住。

维护人：C₂。契约：docs/interfaces.md 第五节「抽取输出契约」与第六节「校验器」。

为什么需要这一层：
    模型返回的 JSON 看起来像那么回事，但经常缺字段、把 difficulty 写成「简单」、
    或者自己发明一个知识点 id / 章节 id。直接落库会把统计口径弄脏，而且错误
    要等到掌握度页面才暴露。校验器把问题挡在入口，并把「具体哪里不对」回喂给
    模型重试（见 app/llm_client.py 的 chat(validator=...)）。

本文件只做纯校验：不读数据库、不发网络请求、不写库。
    - 默认的章节候选集是唯一例外：make_intake_validator() 内部调用
      _default_chapter_ids() 懒加载 app.knowledge.chapter_candidates()；
      测试可以临时替换这个私有函数来注入章节，不改变公开签名。
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

QUESTION_TYPES = ("choice", "fill")

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

    懒加载：只有 make_intake_validator() 需要章节时才碰数据库；
    测试通过临时替换本函数注入章节，这样公开签名保持单参数。
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


def _default_kp_ids() -> list[str]:
    """取现有知识点 id，供练习出题校验器检查 kp_ids 是否越界。"""
    from . import knowledge

    try:
        points = knowledge.all_knowledge_points()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "无法加载知识点候选集：请先运行 python seed_data.py 建库灌入演示知识点"
        ) from exc

    ids = [
        str(point["id"])
        for point in points
        if isinstance(point, dict) and point.get("id")
    ]
    if not ids:
        raise RuntimeError("知识点候选集为空：请先运行 python seed_data.py")
    return ids


def make_intake_validator(allowed_kp_ids: Any) -> Callable[[Any], str | None]:
    """构造「错题抽取」校验器。

    参数
        allowed_kp_ids    现有知识点 id 列表/集合（取 knowledge.all_knowledge_points() 里的 id）

    15 个章节由 _default_chapter_ids() 内部从 knowledge.chapter_candidates() 取；
    测试可以临时替换这个私有函数来注入章节，不改变公开签名。

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
    allowed_chapters = set(_default_chapter_ids())

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


def make_practice_validator(
    allowed_chapter_ids: Any,
    expected_min: int = 1,
) -> Callable[[Any], str | None]:
    """构造「练习出题」校验器。

    参数
        allowed_chapter_ids  15 个章节 id（题目只能挂在已有章节下）
        expected_min         至少几道题；默认 1

    输入形状
        列表 / 元组，或 {"items": [...]}，或单个题目对象。
        兼容这三种是因为模型可能返回数组，也可能按 json_mode 包一层。

    返回
        合规 None；不合规返回中文错误串，供 llm_client 回喂重试。

    🔴 kp_ids 必须在现有知识点候选集里，否则练完写 practice_log 会撞外键。
    """
    allowed_chapters = {str(chapter_id) for chapter_id in allowed_chapter_ids}
    if not allowed_chapters:
        raise ValueError("allowed_chapter_ids 不能为空")
    try:
        minimum = int(expected_min)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"expected_min 必须是整数：{expected_min!r}") from exc
    minimum = max(0, minimum)

    # 知识点候选集只能在工厂里取；测试时会先建好临时/演示库。
    allowed_kps = set(_default_kp_ids())

    def validate(data: Any) -> str | None:
        if isinstance(data, dict):
            if "items" in data:
                items = data["items"]
            elif "id" in data:
                items = [data]
            else:
                return "练习题必须是数组，或包含 items 数组的对象"
        elif isinstance(data, (list, tuple)):
            items = list(data)
        else:
            return f"练习题必须是数组或对象，实际是 {type(data).__name__}"

        if not isinstance(items, (list, tuple)):
            return "items 必须是数组"
        if len(items) < minimum:
            return f"至少需要 {minimum} 道题，实际 {len(items)} 道"

        for index, item in enumerate(items, start=1):
            prefix = f"第 {index} 题"
            if not isinstance(item, dict):
                return f"{prefix}必须是对象"

            qid = item.get("id")
            if not isinstance(qid, str) or not qid.startswith("qp_"):
                return f"{prefix} id 必须是 qp_ 开头的字符串，实际 {qid!r}"

            qtype = item.get("type")
            if qtype not in QUESTION_TYPES:
                return (
                    f"{prefix} type 必须是 " + " / ".join(QUESTION_TYPES)
                    + f" 之一，实际 {qtype!r}"
                )

            chapter_id = item.get("chapter_id")
            if not isinstance(chapter_id, str) or chapter_id not in allowed_chapters:
                return f"{prefix} chapter_id 不在 15 个章节内：{chapter_id!r}"

            kp_ids = item.get("kp_ids")
            if not _is_array(kp_ids) or not kp_ids:
                return f"{prefix} kp_ids 必须是非空数组"
            if len(kp_ids) > MAX_KNOWLEDGE_IDS:
                return f"{prefix} kp_ids 最多 {MAX_KNOWLEDGE_IDS} 个，实际 {len(kp_ids)} 个"
            if any(not isinstance(kp_id, str) or not kp_id.strip() for kp_id in kp_ids):
                return f"{prefix} kp_ids 的每一项都必须是非空字符串"
            unknown = sorted({kp_id for kp_id in kp_ids if kp_id not in allowed_kps})
            if unknown:
                return (
                    f"{prefix} kp_ids 有不在候选列表内的值："
                    + "、".join(unknown)
                    + f"（候选共 {len(allowed_kps)} 个）"
                )

            stem = item.get("stem")
            if not isinstance(stem, str) or not stem.strip():
                return f"{prefix} stem 必须是非空字符串"
            answer = item.get("answer")
            if not isinstance(answer, str) or not answer.strip():
                return f"{prefix} answer 必须是非空字符串"
            explanation = item.get("explanation")
            if not isinstance(explanation, str) or not explanation.strip():
                return f"{prefix} explanation 必须是非空字符串"

            difficulty = item.get("difficulty")
            if not isinstance(difficulty, str) or difficulty not in DIFFICULTIES:
                return (
                    f"{prefix} difficulty 必须是 " + " / ".join(DIFFICULTIES)
                    + f" 之一，实际 {difficulty!r}"
                )

            options = item.get("options")
            if qtype == "choice":
                if not isinstance(options, dict) or len(options) < 2:
                    return f"{prefix} choice 至少需要 2 个选项"
                keys = {str(key).strip().upper() for key in options}
                if answer.strip().upper() not in keys:
                    return (
                        f"{prefix} 标准答案 {answer!r} 不是有效选项编号，"
                        f"可选编号为 {sorted(keys)}"
                    )
            elif options not in ({}, None):
                return f"{prefix} fill 的 options 应为空对象"

        return None

    return validate
