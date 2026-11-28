"""所有提示词集中在这里 —— 纯字符串常量 + 拼装函数。

维护人：B

三条硬约束（见 app/README.md 与 AGENTS.md 第四条第 8 项）：
- 不发网络请求：那是 app/llm_client.py 的事
- 不读写数据库：数据与候选由 qa.py / intake.py 查好当参数传进来
- 不 import llm_client：否则形成循环依赖

本文件只做两件事：
1. 存"话术"（角色设定、四段结构、字段说明、枚举判别例子）
2. 把"这次的数据"（问题、答案、章节候选、已有知识点）拼成 messages

产物统一是 list[dict]，直接喂给 llm_client.chat()。
"""

from __future__ import annotations

# ============================================================
# 通用
# ============================================================

SUBJECT_DEFAULT = "高等数学"

#: 讲解风格 → 长度要求。界面把中文风格名原样透传进来（见 docs/interfaces.md 第九节）。
QA_STYLE_HINTS = {
    "简洁": "整体控制在 400 字以内，例子只给一个。",
    "详细": "整体可以到 800 字，例子给一个正例和一个反例，关键步骤写清推导理由。",
}

#: 未知风格时的兜底，避免拼出半句话
QA_STYLE_FALLBACK = QA_STYLE_HINTS["简洁"]


# ============================================================
# ① 知识点问答
# ============================================================

_QA_SYSTEM = """你是《{subject}》课程的助教，面向刚学这门课的大学生。

【回答结构】必须严格按下面四段组织，每段用加粗小标题：

**【一句话定义】**
用一句话说清这个概念是什么。不超过 40 字。

**【通俗解释】**
用日常语言解释它为什么存在、怎么理解，尽量不要出现符号。

**【具体例子】**
给一个可以算的具体例子，公式用 LaTeX 的 $...$ 包裹，并写出关键步骤。

**【常见误区】**
指出学生最容易犯的一个错误，说明错在哪里、正确做法是什么。

【硬性规则】
1. 🔴 只回答{subject}范围内的内容。判断依据是：这个概念是否属于本课程教材的章节。
   范围内章节：函数与极限 / 导数与微分 / 微分中值定理与导数应用 / 不定积分 /
   定积分 / 微分方程 / 多元函数微分 / 重积分 / 曲线曲面积分 / 无穷级数 /
   空间解析几何。

   🔴 下面这些即使也都是数学，一律属于范围外，必须拒绝：
   · 集合论：集合、并集、交集、笛卡尔积、子集、等价关系
   · 线性代数：矩阵、行列式、逆矩阵、特征值、向量空间、线性方程组、高斯消元
   · 概率论与数理统计：条件概率、随机变量、期望、方差、分布、假设检验
   · 离散数学：图论、数理逻辑、组合数学、鸽巢原理、排列组合
   · 复变函数、数值分析、运筹学、数学建模
   · 编程、写作、闲聊、情感咨询

   🔴 判断顺序：先看这个概念是否出现在上面列出的高数章节里；不在就拒绝。
   不要因为「它也是数学」就回答 —— 集合、矩阵、笛卡尔积都是其他课程的内容，
   不是高等数学。

   拒绝时仍按四段结构输出：说明本系统只覆盖高等数学，
   并给一个高数范围内的替代例子（例如「如果你问的是极限、导数或积分，我可以帮忙」）。
2. 不要编造定理名称、课本页码、引用出处或数据。确实拿不准时，明确写
   "这一点建议对照教材确认"，不要硬编。
3. 全部用中文回答；数学符号用 $...$ 包裹，不要用 markdown 代码块包公式。
4. 长度要求：{style_hint}
"""

#: 检索到的知识点在提示词里的标题，方便模型区分"参考资料"与"用户问题"
_QA_CONTEXT_HEADER = "【可参考的知识点】（来自系统已积累的知识点库，不一定完整；不足时按你的知识回答）"


def build_qa_messages(
    question: str,
    hits: list[dict] | None = None,
    subject: str = SUBJECT_DEFAULT,
    style: str = "简洁",
    history: list[dict] | None = None,
) -> list[dict]:
    """拼出问答用的 messages。

    参数
        question  用户这一轮的提问（已 strip）
        hits      retrieve_knowledge() 的返回，最多 4 条，可为 None
        subject   学科名，默认「高等数学」
        style     界面选的「简洁」/「详细」
        history   已清洗过的历史（只含 role/content，且首条不是 assistant）

    返回
        [{"role": "system", ...}, ...history, {"role": "user", ...}]

    ⚠️ 历史必须已经清洗过：本函数不做过滤，只负责拼装。
       清洗逻辑在 qa._clean_history()，那里会去掉界面加的 source 字段和失败提示。
    """
    style_hint = QA_STYLE_HINTS.get((style or "").strip(), QA_STYLE_FALLBACK)
    system = _QA_SYSTEM.format(subject=subject, style_hint=style_hint)

    messages: list[dict] = [{"role": "system", "content": system}]
    if history:
        messages.extend(history)

    context = _format_hits(hits)
    if context:
        user_content = f"{_QA_CONTEXT_HEADER}\n{context}\n\n【问题】\n{question}"
    else:
        user_content = f"【问题】\n{question}"
    messages.append({"role": "user", "content": user_content})
    return messages


def _format_hits(hits: list[dict] | None) -> str:
    """把检索结果排成“- 名字：一句话解释”的清单；没有就返回空串。"""
    if not hits:
        return ""
    lines = []
    for hit in hits:
        name = (hit.get("name") or "").strip()
        if not name:
            continue
        explain = (hit.get("explain") or "").strip().replace("\n", " ")
        if len(explain) > 120:
            explain = explain[:120] + "…"
        lines.append(f"- {name}：{explain}" if explain else f"- {name}")
    return "\n".join(lines)


# ============================================================
# ② 错题抽取
# ============================================================

_INTAKE_RULES = """你是错题录入助手。学生做过的一道题（做错了想归档）交给你，
你要把它整理成一个结构化的 JSON 对象。

【输出格式】只输出一个 JSON 对象。不要 markdown 代码块，不要任何解释文字。

【字段】
- question_text   题干原文，保留公式（LaTeX 用 $...$）
- student_answer  学生写的答案原文；学生没写就填 ""
- correct_answer  正确答案
- solution_steps  推导步骤，字符串数组，至少 2 步，每步一句话
- knowledge_ids   从【已有知识点】里挑选命中的 id，最多 3 个；没有合适的就填 []
- new_concepts    建议新建的知识点，数组，元素形如 {"name": "名字", "chapter_id": "ch_xxx"}
                  没有建议就填 []
- difficulty      easy / medium / hard 之一
- error_type      下面五选一
- confidence      0 到 1 之间的小数，表示你这次抽取的把握

【error_type 判别】
- concept_confusion  公式或定义用错（例：把洛必达法则用在不是 0/0、∞/∞ 的极限上）
- calculation_error  思路对但中间算错（符号错、代数变形错、漏积分常数）
- method_wrong       用了不适用的解法（例：该换元积分却硬套分部积分）
- incomplete         漏条件、漏解（例：不定积分漏 +C、方程漏根、忽略定义域）
- unknown            判不出来时填它，界面会转人工选择

【硬性规则】
1. 如果【参考答案】非空，必须以它为准，不要自行重新计算。
2. knowledge_ids 只能填【已有知识点】里出现过的 id，绝不自己编 id。
3. new_concepts 的 chapter_id 只能从【章节候选】里选，绝不发明新章节。
4. 学生答案与参考答案完全一致时（看起来其实做对了），error_type 仍然填 unknown，
   并在 correct_answer 填写你算出的正确结果 —— 界面会提示用户核对，不会自动归档。
5. 公式用 LaTeX；不要写大段分析，JSON 越紧凑越好。
"""

#: 给 llm_client 重试时用的结构提示（chat(schema_hint=...)）。只在模型输出不合规时才会被拼进对话。
INTAKE_SCHEMA_HINT = (
    '{"question_text": str, "student_answer": str, "correct_answer": str, '
    '"solution_steps": [str, ...], "knowledge_ids": [str, ...], '
    '"new_concepts": [{"name": str, "chapter_id": str}], '
    '"difficulty": "easy|medium|hard", '
    '"error_type": "concept_confusion|calculation_error|method_wrong|incomplete|unknown", '
    '"confidence": 0.0~1.0}'
)

_INTAKE_IMAGE_USER_TEXT = """请把图片里的这道题整理成 JSON。

要求：
1. 公式按图片原样转写成 LaTeX，不要"顺手修正"看不出对错的部分。
2. 学生的手写答案如果辨认不清，student_answer 填你能看清的部分，并在 confidence 里降低把握。
3. 只要图里有不止一道题，只处理最上面（最显眼）的那一道，不要合并多题。
"""


def build_intake_system(
    chapters: list[dict] | None = None,
    known_points: list[dict] | None = None,
) -> str:
    """拼错题抽取的 system 提示词：固定规则 + 本次的章节候选与知识点候选。

    🔴 候选列表必须每次现查现传：知识点会随使用增长，新长出来的点下一次就该出现在候选里。
       章节固定 15 个，永远全量注入 —— 不给全，模型就会编造 chapter_id
       （实测出现过 "微积分-不定积分" 这种编出来的 id）。
    """
    parts = [_INTAKE_RULES, _format_chapters(chapters), _format_kps(known_points)]
    return "\n".join(part for part in parts if part)


def build_intake_messages(
    question_text: str,
    student_answer: str = "",
    correct_answer: str = "",
    chapters: list[dict] | None = None,
    known_points: list[dict] | None = None,
    extra_note: str = "",
) -> list[dict]:
    """拼出错题抽取用的 messages（文本路径）。"""
    system = build_intake_system(chapters, known_points)
    user = (
        f"【题干】\n{question_text.strip()}\n\n"
        f"【学生答案】\n{(student_answer or '').strip() or '（未填写）'}\n\n"
        f"【参考答案】\n{(correct_answer or '').strip() or '（未提供，请你自行计算）'}"
    )
    if (extra_note or "").strip():
        user += f"\n\n【用户备注】\n{extra_note.strip()}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_intake_image_user_text(extra_note: str = "") -> str:
    """图片路径的 user 文本（图片本身由 llm_client.vision 以多模态块传进去）。"""
    text = _INTAKE_IMAGE_USER_TEXT
    if (extra_note or "").strip():
        text += f"\n【用户备注】\n{extra_note.strip()}\n"
    return text


def _format_chapters(chapters: list[dict] | None) -> str:
    """15 个章节候选，格式：- ch_xxx 名称：简介"""
    if not chapters:
        return "【章节候选】（未加载到章节表，请先运行 python seed_data.py）"
    lines = ["【章节候选】（chapter_id 只能从这里选，共 %d 个）" % len(chapters)]
    for chapter in chapters:
        cid = (chapter.get("id") or "").strip()
        name = (chapter.get("name") or "").strip()
        brief = (chapter.get("brief") or "").strip()
        if not cid:
            continue
        lines.append(f"- {cid}  {name}：{brief}" if brief else f"- {cid}  {name}")
    return "\n".join(lines)


def _format_kps(known_points: list[dict] | None) -> str:
    """已有知识点候选，格式：- kp_id 名称（章节）"""
    if not known_points:
        return "【已有知识点】（库里还没有知识点；这一题请用 new_concepts 建议新建）"
    lines = ["【已有知识点】（knowledge_ids 只能填这里的 id）"]
    for point in known_points:
        kid = (point.get("id") or "").strip()
        name = (point.get("name") or "").strip()
        chapter_name = (point.get("chapter_name") or "").strip()
        if not kid:
            continue
        lines.append(f"- {kid}  {name}（{chapter_name}）" if chapter_name else f"- {kid}  {name}")
    return "\n".join(lines)


# ============================================================
# ③ 统一出口（供排查用）
# ============================================================

def prompt_manifest() -> dict[str, str]:
    """列出本文件对外提供的拼装函数，供自检脚本核对有没有漏。"""
    return {
        "build_qa_messages": "① 问答",
        "build_intake_messages": "② 错题抽取（文本）",
        "build_intake_system": "② 错题抽取（system 段，图片路径也复用）",
        "build_intake_image_user_text": "② 错题抽取（图片路径的 user 文本）",
    }
