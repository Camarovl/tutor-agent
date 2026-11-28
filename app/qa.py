"""功能① 知识点问答 —— 检索本地知识点 → 拼提示词 → 调模型 → 返回 markdown。

维护人：B
契约：docs/interfaces.md 第四节

分层：本文件发请求一律经 app/llm_client.py；数据一律经 app/knowledge.py；
      提示词一律经 app/prompts.py。本文件不写 SQL、不 import openai。
"""

from __future__ import annotations

import re
import sys

from app import knowledge, llm_client, prompts

#: retrieve_knowledge 的默认返回条数（契约里的 limit=4）
_DEFAULT_LIMIT = 4

#: 命中片段里 explain 截断长度，避免把整段讲解塞进提示词
_EXPLAIN_CLIP = 120

# ------------------------------------------------------------
# 中文分词：不用 split()
#   🔴 中文没有空格，split() 会把整句当一个词，检索必然失效。
#   🔴 也不引入 jieba 这类依赖：几行 2-gram（相邻两字）就够用，
#      高数知识点名都不长，2-gram 的重叠率足以区分「洛必达」和「泰勒」。
# ------------------------------------------------------------

_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_]*")


def _grams(text: str) -> set[str]:
    """把一段文字切成「汉字 2-gram + 英文单词」的集合。"""
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


def _overlap(query_grams: set[str], text: str) -> float:
    """查询词与一段文本的重叠率：命中数 / 查询词数，取值 0~1。"""
    if not query_grams:
        return 0.0
    target = _grams(text)
    if not target:
        return 0.0
    return len(query_grams & target) / len(query_grams)


# ------------------------------------------------------------
# 检索
# ------------------------------------------------------------
def retrieve_knowledge(question: str, limit: int = _DEFAULT_LIMIT) -> list[dict]:
    """在已有知识点里找和问题相关的几条。

    为什么不用向量检索：知识点表规模小（几十到几百条），
    2-gram 重叠已经够用，还不额外引入 embedding 依赖和一次网络请求
    （见 docs/limitations.md 第 6 条：查重/检索都是启发式的，配人工确认兜底）。

    打分：名字命中权重 2，讲解命中权重 1；名字更重要，因为用户是照着名字问的。

    返回：[{kp_id, name, chapter_id, chapter_name, explain, score}]，按分数降序，最多 limit 条。
    """
    query = (question or "").strip()
    if not query:
        return []

    try:
        points = knowledge.all_knowledge_points()
    except Exception:
        # 库还没建好时不要让问答整体挂掉：当作没有参考资料，让模型凭自己的知识答。
        return []

    query_grams = _grams(query)
    scored: list[tuple[float, dict]] = []

    for point in points:
        name = point.get("name") or ""
        explain = point.get("explain") or ""
        name_score = _overlap(query_grams, name)
        explain_score = _overlap(query_grams, explain) if explain else 0.0
        score = name_score * 2.0 + explain_score
        if score <= 0:
            continue
        clipped = explain[: _EXPLAIN_CLIP] + ("…" if len(explain) > _EXPLAIN_CLIP else "")
        scored.append(
            (
                score,
                {
                    "kp_id": point.get("id"),
                    "name": name,
                    "chapter_id": point.get("chapter_id"),
                    "chapter_name": point.get("chapter_name"),
                    "explain": clipped,
                    "score": round(score, 4),
                },
            )
        )

    scored.sort(key=lambda item: (-item[0], item[1].get("name") or ""))
    try:
        count = max(1, int(limit))
    except (TypeError, ValueError):
        count = _DEFAULT_LIMIT
    return [item[1] for item in scored[:count]]


# ------------------------------------------------------------
# 历史清洗
# ------------------------------------------------------------
def _clean_history(history: list[dict] | None) -> list[dict]:
    """把界面传来的历史洗成「只有 role + content」的干净消息。

    要做三件事：
    1. 丢掉界面加的 source 字段（"real"/"stub"/"error"）——
       它是给人看的，发给模型既浪费 token，换供应商还可能被判非法参数。
    2. 丢掉调用失败时写进去的错误文本（source == "error"）——
       那不是模型的回答，留在上下文里会让它以为自己说过一段报错。
    3. 保证首条不是 assistant —— 部分模型的 chat template 会直接报
       "No user query found in messages"。

    只做清洗，不做截断：窗口大小由界面控制（streamlit_app.py 里 HISTORY_WINDOW）。
    """
    cleaned: list[dict] = []
    for message in history or []:
        if not isinstance(message, dict):
            continue
        if message.get("source") == "error":
            continue
        role = message.get("role")
        content = message.get("content")
        if role not in ("user", "assistant"):
            continue
        if not isinstance(content, str) or not content.strip():
            continue
        cleaned.append({"role": role, "content": content})

    while cleaned and cleaned[0]["role"] == "assistant":
        cleaned.pop(0)

    return cleaned


# ------------------------------------------------------------
# 对外接口
# ------------------------------------------------------------
def answer(
    question: str,
    subject: str = prompts.SUBJECT_DEFAULT,
    style: str = "简洁",
    history: list[dict] | None = None,
) -> str:
    """回答一个高等数学问题，返回 markdown。

    失败（LLMError）向上抛：界面已经做了 try/except，会把异常显示成
    「这次调用失败了」并标记 source="error"，比在这里吞掉更容易排障。
    """
    text = (question or "").strip()
    if not text:
        return "请先输入一个[高等数学]的问题，例如「洛必达法则什么时候能用？」"

    hits = retrieve_knowledge(text)
    messages = prompts.build_qa_messages(
        question=text,
        hits=hits,
        subject=(subject or prompts.SUBJECT_DEFAULT).strip() or prompts.SUBJECT_DEFAULT,
        style=style,
        history=_clean_history(history),
    )
    result = llm_client.chat(messages, purpose="qa")
    reply = (result.text or "").strip()
    if not reply:
        # 技术细节进 stderr，异常 message 只给用户看
        print(
            f"[qa] 模型返回空回答（completion_tokens={result.completion_tokens}, "
            f"reasoning_tokens={result.reasoning_tokens}）",
            file=sys.stderr,
        )
        raise llm_client.LLMError("模型这次没有给出内容，请重试或换个问法")
    return reply


def list_subject_topics() -> list[str]:
    """界面展示「本系统覆盖哪些内容」。

    返回 15 个章节名，而不是知识点名：
    - 章节是固定骨架，任何时候都有值，不会出现"空列表"；
    - 知识点会随使用不断长出来，今天 3 条明天 10 条，拿它当"覆盖范围"会误导用户。
    """
    try:
        return [row.get("name") or row.get("id") for row in knowledge.chapter_candidates()]
    except Exception:
        return []
