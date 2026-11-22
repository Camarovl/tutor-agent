"""桩函数 + 真实现门面 —— 队友代码未就绪时，先把界面跑起来。

维护人：D。契约：docs/interfaces.md（桩的范围见第九节）。

本文件有两个角色，别混在一起看
------------------------------------------------------------
1) 门面（facade）
   界面只写 `from app import stubs as backend`，然后 `backend.answer(...)`。
   这里的每个函数先找真实现（app.qa / app.intake / app.profile /
   app.knowledge / app.db）：导入成功就转发，导入失败才回落到桩。
   队友的模块一进 main，界面自动用真数据，`streamlit_app.py` 一行都不用改。

   🔴 只吞「导入失败」（ImportError / 函数不存在），
      真模块存在但运行时抛异常照常向上抛 —— 否则真实 bug 会被伪装成
      「桩在跑」，排障时会往完全错误的方向找。

2) 桩（stub）
   返回形状必须与契约完全一致：11 个列名、6 个键、IntakeDraft 的字段。
   桩写错形状的代价是将来换真实现时界面炸在 KeyError 上，
   而根因是「当初桩没照契约造」，很难查。

分层约束：本文件不发网络请求、不写 SQL。
数据一律经 app.db / app.knowledge，与界面层同一条口径。
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parent.parent

# ============================================================
# 契约常量 —— 照 docs/interfaces.md 抄，改这里等于改接口
# ============================================================

#: mastery_table() 的 11 列，顺序固定（界面按位置渲染）
MASTERY_COLUMNS = [
    "kp_id",
    "kp_name",
    "chapter_id",
    "chapter_name",
    "attempts",
    "correct",
    "correct_rate",
    "difficulty_factor",
    "decay_factor",
    "mastery",
    "status",
]

#: summary_text() 的 6 个键，界面顶部指标卡直接按下标读
SUMMARY_KEYS = [
    "kp_total",
    "wrong_total",
    "practice_total",
    "weak_count",
    "llm_calls",
    "llm_tokens",
]

#: 模型服务三项必填环境变量（代码里不留默认值，见 .env.example）
REQUIRED_ENV = ["LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"]

#: mastery_table().status 的四个取值（中文标签，与分级阈值一一对应）
STATUS_LABELS = ["未掌握", "基本掌握", "已掌握", "样本不足"]

#: error_type 五个枚举值的界面中文名（数据库里存英文，展示层映射一次）
ERROR_TYPE_LABELS = {
    "concept_confusion": "概念混淆（公式/定义用错了）",
    "calculation_error": "计算失误（思路对，中间算错）",
    "method_wrong": "方法选错（用了不适用的解法）",
    "incomplete": "审题不全（漏条件、漏解）",
    "unknown": "判不出（转人工选择）",
}

#: difficulty 三个英文值 → 界面中文
DIFFICULTY_LABELS = {"easy": "易", "medium": "中", "hard": "难"}

#: 讲解风格（界面侧边栏的选项，原样透传给 qa.answer 的 style 参数）
STYLES = ["简洁", "详细"]


# ============================================================
# 运行模式与失败记录 —— 让「门面静默回落」变成看得见
# ============================================================
#: 只读这一个环境变量，不碰 .env、不读密钥。
#:   auto（默认）：能导入就用真实现
#:   stub       ：全部强制用桩 —— 演示时没配 API Key 也不报错
#:   real       ：全部必须是真实现，缺一个就在页面顶部报错（交付前自查）
def _check_modes() -> str:
    """读取并校验 TUTOR_BACKEND。非法值不抛异常（否则整页白屏），只警告并回落 auto。"""
    raw = os.environ.get("TUTOR_BACKEND", "auto")
    mode = (raw or "auto").strip().lower() or "auto"
    if mode not in ("auto", "stub", "real"):
        print(
            f"[stubs] 未知的 TUTOR_BACKEND={raw!r}，已回落到 auto"
            "（可选值：auto / stub / real）",
            file=sys.stderr,
        )
        return "auto"
    return mode


_MODE = _check_modes()

#: 键是 "模块名.函数名"，值是给人看的原因。
#: 只记录「模块存在但接不上」这种异常；模块还没写属于正常状态，不记录。
_FAILURES: dict[str, str] = {}


# ============================================================
# 门面：找真实现
# ============================================================
def _real(module_name: str, func_name: str):
    """取真实现；模块还不存在、或取不到函数时返回 None。

    三种情况要分清楚，因为处理方式完全不同：
      ① 模块文件还没写        → 正常（队友还没开工），不记录，回落桩
      ② 模块存在但导入就炸     → 异常（多半是 from .prompts import X 拼错了），记录原因
      ③ 模块能导入但没有这个函数 → 异常（签名/名字对不上），记录原因

    🔴 只吞 ImportError/AttributeError。真实现的**运行期**异常照常向上抛 ——
       否则真实报错也会被伪装成「桩在跑」。
    """
    if _MODE == "stub":
        return None

    key = f"{module_name}.{func_name}"
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        # find_spec 用来区分「文件不存在」和「文件在但导入失败」
        if importlib.util.find_spec(module_name) is not None:
            _FAILURES[key] = f"模块存在但导入失败：{exc}"
        else:
            _FAILURES.pop(key, None)
        return None

    fn = getattr(module, func_name, None)
    if fn is None:
        _FAILURES[key] = f"{module_name} 里没有 {func_name}()"
        return None

    _FAILURES.pop(key, None)  # 修好之后把旧记录清掉，别一直挂着红
    return fn


#: 哪些函数归谁实现 —— 侧边栏据此显示「真 / 桩」，第 5 步同步契约第九节清单也靠它
_FACADE = {
    "answer": ("app.qa", "answer"),
    "mastery_table": ("app.profile", "mastery_table"),
    "summary_text": ("app.profile", "summary_text"),
    "build_practice_session": ("app.profile", "build_practice_session"),
    "grade": ("app.profile", "grade"),
    "record_result": ("app.profile", "record_result"),
    "analyze_text": ("app.intake", "analyze_text"),
    "analyze_image": ("app.intake", "analyze_image"),
    "commit": ("app.intake", "commit"),
    "all_knowledge_points": ("app.knowledge", "all_knowledge_points"),
    "chapter_candidates": ("app.knowledge", "chapter_candidates"),
    "recent_wrong_questions": ("app.db", "recent_wrong_questions"),
    "wrong_question_count": ("app.db", "wrong_question_count"),
}


def sources() -> dict[str, str]:
    """每个函数当前是走真实现（real）还是桩（stub）。

    ⚠️ 返回格式固定为 {函数名: "real" | "stub"}，界面靠它判断，别改。
    """
    return {
        name: ("real" if _real(mod, func) else "stub")
        for name, (mod, func) in _FACADE.items()
    }


def problems() -> dict[str, str]:
    """只返回「模块已存在但接不上」的项：函数名 → 原因。

    正常的「队友还没写」不会出现在这里 —— 那种情况没有排查价值，
    混进来只会让人每天看见一片红，最后谁都懒得看。
    """
    return {
        name: _FAILURES[f"{mod}.{func}"]
        for name, (mod, func) in _FACADE.items()
        if f"{mod}.{func}" in _FAILURES
    }


def summary() -> str:
    """一行汇总，形如「真 9 / 桩 4（其中 1 个异常）」；异常为 0 时不显示后半句。"""
    status = sources()
    real = sum(1 for kind in status.values() if kind == "real")
    text = f"真 {real} / 桩 {len(status) - real}"
    bad = len(problems())
    if bad:
        text += f"（其中 {bad} 个异常）"
    if _MODE != "auto":
        text += f"［TUTOR_BACKEND={_MODE}］"
    return text


def mode() -> str:
    """当前门面模式：auto / stub / real（界面用来判断要不要做严格自查）。"""
    return _MODE


def facade() -> dict[str, str]:
    """函数名 → 真实现所在模块。只读拷贝，供界面显示「归谁实现」。"""
    return {name: module for name, (module, _func) in _FACADE.items()}


def db_ready() -> bool:
    """数据库是否已建好（schema 灌过、章节有数据）。

    界面在跑 seed_data.py 之前是空的，别让页面直接抛 sqlite3 异常。
    """
    if not (ROOT_DIR / "data" / "app.db").exists():
        return False
    real = _real("app.db", "chapter_rows")
    if real is None:
        return False
    try:
        return bool(real())
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        return False


# ============================================================
# 错题草稿 —— 与契约第五节 IntakeDraft 字段一一对应
# ============================================================
@dataclass
class IntakeDraft:
    """识别结果。B 的 app/intake.py 落地后，界面照旧调 backend.analyze_text()，
    只是这个类会改成从 app.intake 导入（门面吃掉差异，界面不动）。"""

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


# ============================================================
# ① 问答
# ============================================================
def answer(question: str, subject: str = "高等数学", style: str = "简洁", history=None) -> str:
    """功能①的入口。真实现归 B（app/qa.py），签名见契约第四节。"""
    real = _real("app.qa", "answer")
    if real is not None:
        return real(question, subject, style, history=history)
    return _stub_answer(question, subject, style)


def _stub_answer(question: str, subject: str, style: str) -> str:
    """桩：返回四段结构 markdown，形状与 B 的提示词要求一致。"""
    detail = (
        "详细版会在真实实现里展开推导过程，并多给一个反例。"
        if style == "详细"
        else "简洁版只给一个例子。"
    )
    return f"""> 🧪 **桩函数输出**（`app/stubs.py`）—— B 的 `app/qa.py` 就绪后自动切真实现。

**【一句话定义】**
你问的「{question}」属于{subject}的核心概念之一，这里先给出桩文本占位。

**【通俗解释】**
桩函数不做真实推理，只演示页面的渲染效果（markdown、公式、分点）。{detail}

**【具体例子】**
例：求 $\\lim_{{x\\to 0}}\\frac{{\\sin 3x}}{{x}}$ —— 用等价无穷小 $\\sin 3x\\sim 3x$，结果为 3。

**【常见误区】**
把「可导」当成「连续」的充分必要条件来套用。
"""


# ============================================================
# ③ 掌握度（返回形状是硬契约）
# ============================================================
def mastery_table() -> pd.DataFrame:
    """功能③的核心表。真实现归 C₂（app/profile.py），列名归 C₁ 定死。"""
    real = _real("app.profile", "mastery_table")
    if real is not None:
        return real()
    return pd.DataFrame(_STUB_MASTERY_ROWS, columns=MASTERY_COLUMNS)


def summary_text() -> dict:
    """界面顶部指标卡的数据源。真实现归 C₂。"""
    real = _real("app.profile", "summary_text")
    if real is not None:
        return real()
    rows = _STUB_MASTERY_ROWS
    return {
        "kp_total": len(rows),
        "wrong_total": 4,
        "practice_total": sum(r["attempts"] for r in rows),
        "weak_count": sum(1 for r in rows if r["status"] == "未掌握"),
        "llm_calls": 37,
        "llm_tokens": 48210,
    }


def _mst(
    kp_id: str,
    kp_name: str,
    chapter_id: str,
    chapter_name: str,
    attempts: int,
    correct: int,
    difficulty_factor: float,
    decay_factor: float,
    status: str,
) -> dict:
    """按契约公式算一行：掌握度 = 正确率 × 难度加权 × 时间衰减，截断到 1.0。

    没练过（attempts=0）时前三项都是 0.0，状态固定「样本不足」——
    契约要求这类行必须出现，否则界面上的表会缺行。
    """
    correct_rate = round(correct / attempts, 4) if attempts else 0.0
    mastery = min(correct_rate * difficulty_factor * decay_factor, 1.0) if attempts else 0.0
    return {
        "kp_id": kp_id,
        "kp_name": kp_name,
        "chapter_id": chapter_id,
        "chapter_name": chapter_name,
        "attempts": attempts,
        "correct": correct,
        "correct_rate": correct_rate,
        "difficulty_factor": difficulty_factor if attempts else 0.0,
        "decay_factor": decay_factor if attempts else 0.0,
        "mastery": round(mastery, 4),
        "status": status,
    }


# 6 行桩数据，刻意覆盖四个档位（演示时排行榜才看得出效果）
_STUB_MASTERY_ROWS = [
    _mst("kp_luobida", "洛必达法则", "ch_mean_value", "微分中值定理与导数应用", 6, 2, 1.05, 1.0, "未掌握"),
    _mst("kp_huanyuan", "换元积分法", "ch_indefinite_integral", "不定积分", 4, 2, 1.0, 0.9, "基本掌握"),
    _mst("kp_taile", "泰勒公式", "ch_mean_value", "微分中值定理与导数应用", 3, 3, 1.15, 1.0, "已掌握"),
    _mst("kp_dingji", "定积分的换元", "ch_definite_integral", "定积分", 2, 1, 1.0, 1.0, "样本不足"),
    _mst("kp_miji", "幂级数收敛半径", "ch_series", "无穷级数", 0, 0, 1.0, 1.0, "样本不足"),
    _mst("kp_yijie", "一阶线性微分方程", "ch_ode_basic", "常微分方程（一阶）", 5, 4, 1.0, 0.9, "已掌握"),
]


# ============================================================
# ② 错题录入
# ============================================================
def analyze_text(question_text: str, student_answer: str = "", correct_answer: str = "") -> IntakeDraft:
    """手输路径。真实现归 B（app/intake.py）。"""
    real = _real("app.intake", "analyze_text")
    if real is not None:
        return real(question_text, student_answer, correct_answer)
    return _stub_draft(question_text, student_answer, correct_answer, source="text")


def analyze_image(image_bytes: bytes, extra_note: str = "") -> IntakeDraft:
    """拍照路径。真实现归 B；Day5 结论若是不支持图片，这条路径会降级成手输。"""
    real = _real("app.intake", "analyze_image")
    if real is not None:
        return real(image_bytes, extra_note=extra_note)
    draft = _stub_draft(
        "（桩数据）求极限 $\\lim_{x\\to 0}\\frac{\\sin 3x}{x}$",
        "3",
        "3",
        source="image",
    )
    draft.note = "桩函数没有真的识别图片，只演示流程" + (f"；用户备注：{extra_note}" if extra_note else "")
    return draft


def _stub_draft(q: str, s: str, c: str, source: str) -> IntakeDraft:
    """桩草稿：字段齐全、confidence 0.82、给出一个「新建知识点」的建议。

    刻意让 knowledge_ids 为空、new_concepts 有值：
    这样第 8 步能演示「现有知识点都不合适 → 建议在章节下新建」那条路径。
    """
    return IntakeDraft(
        question_text=(q or "").strip() or "（桩数据）求极限 lim(x→0) sin3x/x",
        student_answer=(s or "").strip(),
        correct_answer=(c or "").strip() or "3",
        solution_steps=[
            "用等价无穷小替换：当 x→0 时 sin 3x ~ 3x",
            "原式 = lim(x→0) 3x / x = 3",
        ],
        knowledge_ids=[],
        new_concepts=[{"name": "等价无穷小替换", "chapter_id": "ch_function_limit"}],
        difficulty="easy",
        error_type="method_wrong",
        confidence=0.82,
        source=source,
        degraded=False,
        note="桩函数输出，未经模型识别",
    )


def commit(draft, image_path: str | None = None) -> int:
    """用户确认后才落库的唯一入口。真实现归 B（app/intake.py）。

    🔴 桩不写数据库，直接返回 0 —— 假装写成功会让「确认入库」变成假的，
       第 8 步排查时根本分不清是真的存了还是桩糊弄过去了。
    """
    real = _real("app.intake", "commit")
    if real is not None:
        return real(draft, image_path=image_path)
    return 0


# ============================================================
# ④ 针对性练习
# ============================================================
def build_practice_session(count: int = 3):
    """按薄弱点出题，返回 (题目列表, 来源说明)。真实现归 C₂。"""
    real = _real("app.profile", "build_practice_session")
    if real is not None:
        return real(count)
    return _STUB_PRACTICE_ITEMS[: max(1, int(count))], "本次来自薄弱知识点（桩数据）"


def grade(item: dict, user_answer) -> bool:
    """判分（纯函数，不读库不联网）。真实现归 C₂。"""
    real = _real("app.profile", "grade")
    if real is not None:
        return real(item, user_answer)
    return _stub_grade(item, user_answer)


def record_result(items: list, answers) -> list[bool]:
    """判分 + 回写练习记录。真实现归 C₂。"""
    real = _real("app.profile", "record_result")
    if real is not None:
        return real(items, answers)
    return [_stub_grade(item, answers.get(item["id"], "")) for item in items]


def _stub_grade(item: dict, user_answer) -> bool:
    """按契约的判分规则实现桩：choice 宽容大小写与空格，fill 去空格比对。"""
    expected = str(item.get("answer") or "").strip()
    got = str(user_answer or "").strip()
    kind = item.get("type")
    if kind == "choice":
        return got.upper() == expected.upper()
    if kind == "fill":
        norm = lambda s: s.replace(" ", "").replace("　", "").lower()  # noqa: E731
        return norm(got) == norm(expected)
    return False  # 未知题型直接判错（契约：v1 只做 choice / fill）


_STUB_PRACTICE_ITEMS = [
    {
        "id": "qp_0001",
        "type": "choice",
        "chapter_id": "ch_mean_value",
        "kp_ids": ["kp_luobida"],
        "stem": "求极限 $\\lim_{x\\to 0}\\frac{\\sin 3x}{x}$",
        "options": {"A": "0", "B": "1", "C": "3", "D": "不存在"},
        "answer": "C",
        "explanation": "等价无穷小：sin3x ~ 3x，故极限为 3。",
        "difficulty": "easy",
    },
    {
        "id": "qp_0002",
        "type": "fill",
        "chapter_id": "ch_indefinite_integral",
        "kp_ids": ["kp_huanyuan"],
        "stem": "计算 $\\int 2x\\,dx$（填空，含积分常数）",
        "options": {},
        "answer": "x^2+C",
        "explanation": "∫2x dx = x² + C。",
        "difficulty": "easy",
    },
    {
        "id": "qp_0003",
        "type": "choice",
        "chapter_id": "ch_ode_basic",
        "kp_ids": ["kp_yijie"],
        "stem": "方程 $y'+y=0$ 的通解是？",
        "options": {"A": "y=Ce^{-x}", "B": "y=Ce^{x}", "C": "y=Cx", "D": "y=C"},
        "answer": "A",
        "explanation": "一阶线性齐次方程，特征根 r = -1，故 y = Ce^{-x}。",
        "difficulty": "medium",
    },
]


# ============================================================
# 模型配置状态 —— 读环境变量，不联网
# ============================================================
def is_configured() -> bool:
    """三项必填变量是否齐全。真实现归 B（llm_client.is_configured）。"""
    return not missing_config_keys()


def missing_config_keys() -> list[str]:
    """缺哪几项，缺就返回名字（侧边栏直接列给用户看）。"""
    _load_dotenv_if_possible()
    return [k for k in REQUIRED_ENV if not os.getenv(k)]


def _load_dotenv_if_possible() -> None:
    """桩阶段顺手读一下 .env，好让侧边栏状态与真实配置一致。

    正式实现由 B 的 llm_client.py 负责加载 .env；这里不重复造轮子，
    只是为了第 4 步能看到「配没配」。
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    env_file = ROOT_DIR / ".env"
    if env_file.exists():
        load_dotenv(env_file)


# ============================================================
# 数据层的小转发（这些现在就是真的，C₁ 已进 main）
# ============================================================
def all_knowledge_points() -> list[dict]:
    """现有全部知识点，供「涉及知识点」下拉框使用（只列已有的，不许自由输入）。"""
    real = _real("app.knowledge", "all_knowledge_points")
    return real() if real is not None else []


def chapter_candidates() -> list[dict]:
    """15 个章节（归类抽屉），供「新建知识点」时的章节下拉框使用。"""
    real = _real("app.knowledge", "chapter_candidates")
    return real() if real is not None else []


def recent_wrong_questions(limit: int = 20) -> list[dict]:
    """最近录入的错题。C₁ 的 db.py 已实现，这里只是转发。"""
    real = _real("app.db", "recent_wrong_questions")
    return real(limit) if real is not None else []


def wrong_question_count() -> int:
    real = _real("app.db", "wrong_question_count")
    return real() if real is not None else 0
