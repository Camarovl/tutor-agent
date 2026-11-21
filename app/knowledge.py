"""知识点挂载 —— 🔴 整个「知识库从错题里长出来」设计的支点。

职责（维护人 C₁，见 app/README.md 与 docs/interfaces.md 第二节）：

    chapter_candidates()            15 个章节，唯一允许 AI 挂靠的层级
    find_or_create(...)             🔴 核心：查重 → 命中复用 → 未命中才新建
    ensure_valid_chapter(...)       校验章节 id 合法
    all_knowledge_points()          现有全部知识点，供提示词注入候选列表

为什么必须查重：
    不做去重的话，库里会同时出现「不定积分」「不定的积分」「求不定积分」
    三条指同一件事的记录，掌握度统计算成三个不同知识点，功能③彻底作废。

分层约束：本文件不写 SQL —— 所有读写都走 app.db。
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata

from . import db

# ------------------------------------------------------------
# 关于知识点 id 的约定
#   kp_ + 名称的规范化指纹（小写、去标点、去空白）。
#   用途是「可读 + 确定性」：同一个名字永远得到同一个 id，
#   万一并发新建也只会撞 INSERT OR IGNORE，不会产生两条。
# ------------------------------------------------------------

# 中文里对「是不是同一个概念」几乎没有区分度的词。
# 只在相似度比较时剔除，不参与 id 生成，也不改动入库的名字。
#
# ⚠️ 只放真正的虚词和提问用语。「不」「反」「常」这类否定/限定字绝不能进，
#    否则「定积分」和「不定积分」会被当成同一个，而它们是两章不同的内容。
_STOPWORDS = (
    "求一下", "求", "计算", "如何", "怎么", "怎样", "关于", "请问",
    "什么是", "什么叫", "的定义", "的概念", "定义", "概念", "问题", "题目",
    "的", "了",
)
_STOPWORD_RE = re.compile("|".join(map(re.escape, sorted(_STOPWORDS, key=len, reverse=True))))

# 名称里允许保留的字符：汉字、字母、数字。
_KEEP_RE = re.compile(r"[^\u4e00-\u9fff0-9a-z]+")


# ============================================================
# 名称规范化
# ============================================================
def _normalize(text: str) -> str:
    """把名字规范化成一个可比较的形式。

    「不定的积分」「不定积分」「 不定 积分 」→ 都变成「不定积分」

    步骤：全角转半角 → 统一小写 → 去掉无区分度的词 → 只留汉字/字母/数字。
    """
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text).lower().strip()
    s = _STOPWORD_RE.sub("", s)
    return _KEEP_RE.sub("", s)


def _make_kp_id(name: str) -> str:
    """由名字生成确定性的知识点 id：kp_ + 规范化后的名字。"""
    key = _normalize(name) or "unnamed"
    return "kp_" + key


# ============================================================
# 相似度判断
# ============================================================
def _core(text: str) -> str:
    """去掉虚字（的/了等）后的核心形式，只用于相似度比较。

    「不定的积分」→「不定积分」
    """
    return _normalize(text).replace("的", "").replace("了", "")


def _is_same_concept(a: str, b: str) -> bool:
    """判断两个知识点名字是不是同一个概念。

    判据（任一成立即视为同一个），全部基于去掉虚字后的「核心形式」：
    ① 核心形式完全相同
    ② 一个是另一个的子串，且较短的那个 ≥ 较长名字的 80%
       并且两者只差一个字以内
       （「不定积分」↔「不定积分法」→ 同一个；差 1 个字，短占 4/5=80%）
       （「定积分」↔「不定积分」→ 不算，虽然只差 1 个字，
        但 "不" 是决定性的限定词，且 3/4=75% < 80%）
    ③ 核心形式的字符完全相同、只是顺序不同
       （兜住「积分不定」这种打乱顺序的说法）

    🔴 阈值为什么是 80%：高数里「定积分/不定积分/反常积分/广义积分」
       这些名字共享二字尾巴，任何偏宽松的判据都会把它们并成一条，
       掌握度统计算成 3 个不同知识点，功能③直接作废。
    """
    ca, cb = _core(a), _core(b)
    if not ca or not cb:
        return False

    # ① 核心形式相同
    if ca == cb:
        return True

    short, long = (ca, cb) if len(ca) <= len(cb) else (cb, ca)

    # ② 子串包含 + 长度占比 + 只差一个字以内
    if len(short) >= 2 and short in long:
        ratio = len(short) / len(long)
        if ratio >= 0.8 and (len(long) - len(short)) <= 1:
            return True

    # ③ 字符完全相同、只是顺序不同
    if len(ca) >= 2 and sorted(ca) == sorted(cb):
        return True

    return False


# ============================================================
# 只读查询（供 B 注入提示词、供界面展示）
# ============================================================
def chapter_candidates() -> list[dict]:
    """返回 15 个章节的 id / name / brief。

    🔴 这是 AI 唯一允许挂靠的层级 —— 禁止发明新章节。
    """
    return db.chapter_rows()


def all_knowledge_points() -> list[dict]:
    """返回现有全部知识点（id / name / chapter_id / chapter_name），供提示词注入候选列表。

    顺序：按章节，再按名字，保证提示词内容稳定、可复现。
    """
    return db.list_kps_with_chapter()


def ensure_valid_chapter(chapter_id: str) -> bool:
    """校验章节 id 是否合法。

    🔴 这是「知识库自由生长但不乱」的边界：
    返回 False 时调用方必须拒绝新建，不能退化成「随便挂一个」。
    """
    if not chapter_id:
        return False
    return db.chapter_exists(chapter_id)


# ============================================================
# 🔴 核心：查重 → 命中复用 → 未命中才新建
# ============================================================
def find_or_create(
    name_hint: str,
    chapter_id: str,
    context: str | None = None,
) -> str:
    """找一个已存在的知识点并返回它的 id；找不到才在指定章节下新建。

    参数
        name_hint   模型建议的知识点名字，如「洛必达法则」
        chapter_id  建议挂靠的章节 id，必须是 15 个之一
        context     创建新知识点时写进 explain 的初稿（通常是一段讲解）
                    复用已有知识点时**不会**覆盖它的 explain

    返回
        知识点 id（字符串）

    失败
        章节非法、名字为空 → 抛 ValueError（调用方负责降级，不要静默吞掉）

    查找顺序（与 docs/interfaces.md 第二节一致）
        ① 先精确匹配名字（数据库层，最快也最准）
        ② 再拿全部知识点逐个做相似度判断（_is_same_concept）
        ③ 命中 → 直接返回已有 id，一个字节都不写
        ④ 都没命中 → 在 chapter_id 下新建
        ⑤ 返回新 id
    """
    name = (name_hint or "").strip()
    if not name:
        raise ValueError("知识点名字不能为空")
    if not ensure_valid_chapter(chapter_id):
        raise ValueError(f"章节 id 不合法：{chapter_id!r}（只能是 15 个章节之一）")

    # ① 精确匹配：一模一样名字直接命中，省掉后面全表比较
    target_core = _core(name)
    hit = db.find_kp_by_name(name)
    if hit is not None:
        return hit["id"]

    # ② 相似度匹配：知识点表不大，全量拉出来比即可
    for r in db.list_kps():
        if _core(r["name"]) == target_core or _is_same_concept(name, r["name"]):
            return r["id"]

    # ④ 都没命中 → 新建
    new_id = _make_kp_id(name)
    try:
        db.insert_kp(new_id, name, chapter_id, context, "intake")
        return new_id
    except sqlite3.IntegrityError:
        # id 或 name 撞了：说明刚好有人并发建了同名的，直接复用它的 id。
        # 这是可预期的竞争，不算错误。
        row = db.find_kp_by_id_or_name(new_id, name)
        if row is not None:
            return row["id"]
        raise
