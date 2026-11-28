"""界面入口 —— 学习辅导 AI 助教 · 高等数学（Streamlit）。

维护人：D。契约：docs/interfaces.md 第八节。

三条硬规矩（本文件的自查口径，改代码前先看这三条）
------------------------------------------------------------
1. 🔴 不写 SQL。要数据一律调 backend.xxx()，SQL 只在 app/db.py 里。
2. 🔴 不发网络请求。全项目只有 app/llm_client.py 能发，
   所以这里不 import openai / requests / httpx / urllib。
3. 🔴 不引 HTML/CSS/JS 或前端框架。视觉全部走 Streamlit 原生组件；
   样式集中在 app/ui_theme.py 一处注入，本文件不写 <style>。

两种展示状态
------------------------------------------------------------
正常：只给「使用者」看的东西 —— 章节 / 知识点 / 错题 / 讲解风格、
      业务提示、可自救的下一步。
调试（TUTOR_DEBUG=1）：把开发期信息放回来 —— 门面接入比例、逐函数状态、
      模型配置缺项、契约守卫报错、异常原文。
判断标准：这句话是在告诉使用者当前状态，还是在给开发者指路？后者归调试。
"""

from __future__ import annotations

import hashlib
import os
import re
import time
import types

import altair as alt
import pandas as pd
import streamlit as st

from app import stubs as backend
from app import ui_theme

SUBJECT = "高等数学"

#: 调试开关：默认关闭。开启后显示门面状态、模型配置缺项、异常原文等开发信息。
_DEBUG = os.environ.get("TUTOR_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}

#: 后端异常/说明串里这些片段属于「开发者信息」，非调试模式要过滤掉。
_TECH_HINT_RE = re.compile(
    r"python|seed_data|\.py\b|\.env|未接入|app/|sqlite|契约|TODO|None",
    re.IGNORECASE,
)

#: 开场白：只做页面渲染，🔴 绝不写进 st.session_state.history。
#: 原因：发给模型的 messages 序列若以 assistant 开头，多数模型的
#: chat template 会直接报 "No user query found in messages"。
OPENING = (
    "你好，我是这门《高等数学》的辅导助教。\n\n"
    "可以问我：概念怎么理解、这一步为什么这么变形、某道错题错在哪。\n\n"
    "只回答高等数学范围内的内容；范围外的问题我会礼貌拒绝并把你引回课程。"
)

#: 传给模型的对话历史窗口（条数，user+assistant 合计）。
#: 界面显示全部历史，但只把最近几条发给模型 —— 不截断的话聊久了会把
#: 上下文和费用一起顶爆，而且早期闲聊对回答没有帮助。
HISTORY_WINDOW = 6

#: 上传图片的大小上限（MB）。超了就提示先压缩 —— 别把 10 MB 的手机原图
#: 塞进 base64 再发模型，token 和等待时间都会爆。
MAX_IMAGE_MB = 5

#: hero 区常驻的三条公式：极限 / 微积分基本定理 / 巴塞尔问题
HERO_FORMULAS = [
    r"\lim_{x\to 0}\frac{\sin 3x}{x}=3",
    r"\frac{d}{dx}\int_{0}^{x} f(t)\,dt=f(x)",
    r"\sum_{n=1}^{\infty}\frac{1}{n^{2}}=\frac{\pi^{2}}{6}",
]

#: 首次进入时轮播的公式（播完停在第一条）。纯装饰，但这是「高数成分」最直观的一笔。
TICKER_FORMULAS = [
    r"e^{i\pi}+1=0",
    r"\lim_{n\to\infty}\left(1+\frac{1}{n}\right)^{n}=e",
    r"\int_{-\infty}^{+\infty} e^{-x^{2}}\,dx=\sqrt{\pi}",
    r"\nabla f=\left(\frac{\partial f}{\partial x},\ \frac{\partial f}{\partial y}\right)",
]

#: 掌握度四档 → 图标（数据库存中文标签，展示层只做映射）
STATUS_ICONS = {
    "未掌握": "🔴 未掌握",
    "基本掌握": "🟡 基本掌握",
    "已掌握": "🟢 已掌握",
    "样本不足": "⚪ 样本不足",
}

st.set_page_config(
    page_title="学习辅导 AI 助教 · 高等数学",
    page_icon="∑",
    layout="wide",
)


# ============================================================
# 公共小工具
# ============================================================
def _first_time(key: str) -> bool:
    """这段动效是否还要播。同一会话只播一次，否则每次交互都卡一下。"""
    if st.session_state.get(key):
        return False
    st.session_state[key] = True
    return True


def _count_up(slot, label: str, value: int, steps: int = 6, delay: float = 0.04) -> None:
    """指标卡数字从 0 滚到真实值 —— 只为了「系统在跑」的感觉，0.25 秒内结束。"""
    for step in range(1, steps + 1):
        slot.metric(label, int(round(value * step / steps)))
        time.sleep(delay)
    slot.metric(label, value)


def _user_text(raw: str, fallback: str) -> str:
    """把后端抛出的说明串转成使用者能读的句子。

    后端为了排障会在错误里写「先运行 python seed_data.py」「app/xxx.py」这类
    开发者信息 —— 那是给我们看的，不是给使用者看的。非调试模式按句过滤，
    只留业务原因；一句话都留不下时用 fallback。
    """
    text = str(raw or "").strip()
    if not text:
        return fallback
    if _DEBUG:
        return text
    keep = [
        part.strip()
        for part in re.split(r"[。；;\n]", text)
        if part.strip() and not _TECH_HINT_RE.search(part)
    ]
    cleaned = "。".join(keep).strip("。 ")
    return cleaned or fallback


def _practice_note(note: str) -> str:
    """出题来源说明：后端可能带「还没跑 seed_data.py」「未接入生成」这类字眼。"""
    text = (note or "").strip()
    if _DEBUG or not text:
        return text
    if _TECH_HINT_RE.search(text):
        return "本次练习题来自题库"
    return text


def _practice_signature(items: list[dict]) -> str:
    """一轮练习的指纹：题目 id 顺序拼起来取哈希。

    用途只有一个 —— 判断「当前这组题」是否已经判过分。
    重新生成练习会换一批 id，指纹自然变化，所以不会误拦新一轮。
    """
    raw = "|".join(str(item.get("id", "")) for item in items or [])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _kp_options() -> tuple[list[str], dict[str, str]]:
    """知识点下拉框的选项：只允许选库里已有的，不许自由输入。

    返回 (id 列表, id → 显示名)。数据库没建好时返回空，界面给提示而不是抛异常。
    """
    try:
        kps = backend.all_knowledge_points()
    except Exception:  # noqa: BLE001 —— 界面层兜底，别让空库把整页打挂
        kps = []
    ids = [k["id"] for k in kps]
    labels = {k["id"]: f"{k['name']}（{k.get('chapter_name', '')}）" for k in kps}
    return ids, labels


def _init_state() -> None:
    """会话状态的默认值。用 setdefault，避免每次 rerun 把用户的选择冲掉。"""
    defaults = {
        "history": [],  # 问答历史（不含开场白）
        "style": backend.STYLES[0],  # 讲解风格：简洁 / 详细
        "pending_draft": None,  # ② 页刚识别出来、还没确认入库的草稿
        "draft_seq": 0,  # 草稿序号：换草稿时让表单控件换一批 key，避免旧值粘住
        "intake_flash": "",  # ② 页入库成功后的提示（rerun 后会消失，所以先存起来）
        "practice_items": None,  # ④ 页当前这组题
        "practice_note": "",  # ④ 页的题目来源说明
        "practice_result": None,  # ④ 页最近一次判分结果
        "practice_graded": None,  # 本轮题目的签名：判过分就不再给提交按钮
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


# ============================================================
# 视觉层：hero + 状态条
# ============================================================
def _hero() -> None:
    """标题区：一句定位 + 三条公式 + 首次进入的公式轮播。"""
    left, right = st.columns([0.62, 0.38], gap="large")

    with left:
        st.title("∑ 高等数学 · 智能辅导助教")
        st.caption(
            "LIMITS → DERIVATIVES → INTEGRALS → SERIES　｜　"
            "知识点从真实错题里长出来，掌握度按确定性系数估算"
        )

    with right:
        slot = st.empty()
        if _first_time("hero_ticker"):
            for formula in TICKER_FORMULAS:
                slot.latex(formula)
                time.sleep(0.22)
        slot.latex(TICKER_FORMULAS[0])

    columns = st.columns(3)
    for column, formula in zip(columns, HERO_FORMULAS):
        with column:
            with st.container(border=True):
                st.latex(formula)


def _facade_state() -> tuple[dict[str, str], dict[str, str], str, str]:
    """读门面状态，返回 (逐函数状态, 异常项, 一行汇总, 读取失败原因)。仅调试用。"""
    try:
        status = backend.sources()
        bad = backend.problems()
        ratio = backend.summary()
    except Exception as exc:  # noqa: BLE001 —— 兜底，别让导入期异常把页面打白
        return {}, {}, "", f"{type(exc).__name__}: {exc}"
    return status, bad, ratio, ""


def _status_strip() -> None:
    """一行状态：章节 / 知识点 / 错题 / 讲解风格（使用者需要的数字）。

    门面接入比例与异常明细属于开发信息，只在 TUTOR_DEBUG 下追加。
    """
    ready = backend.db_ready()
    try:
        chapters = len(backend.chapter_candidates()) if ready else 0
        kps = len(backend.all_knowledge_points()) if ready else 0
        wrong = backend.wrong_question_count() if ready else 0
    except Exception:  # noqa: BLE001
        chapters = kps = wrong = 0

    bits = [
        f"◈ 章节 **{chapters}**",
        f"◈ 知识点 **{kps}**",
        f"◈ 错题 **{wrong}**",
        f"◈ 风格 **{st.session_state.style}**",
    ]

    if not _DEBUG:
        st.caption("　　".join(bits))
        return

    status, bad, ratio, read_error = _facade_state()
    if read_error:
        st.error("**门面状态读不出来**：" + read_error)
        return
    bits.append(f"◈ 接入 **{ratio}**")
    if bad:
        bits.append(f"⚠️ **{len(bad)} 个接入异常**")
    st.caption("　　".join(bits))

    if bad:
        st.error(
            "**门面接入异常**：这些函数本该走真实现，但导入或取函数失败，已回落桩 ——\n\n"
            + "\n".join(f"- `{name}`：{reason}" for name, reason in bad.items())
        )

    with st.expander(f"查看 {len(status)} 个函数的接入状态", expanded=bool(bad)):
        owners = backend.facade()
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "函数": name,
                        "当前": "✅ 真实现" if kind == "real" else "🧪 桩",
                        "实现模块": owners.get(name, "—"),
                        "异常原因": bad.get(name, ""),
                    }
                    for name, kind in status.items()
                ]
            ),
            hide_index=True,
            column_config={
                "函数": st.column_config.TextColumn("函数", width="medium"),
                "当前": st.column_config.TextColumn("当前", width="small"),
                "实现模块": st.column_config.TextColumn("实现模块", width="small"),
                "异常原因": st.column_config.TextColumn("异常原因", width="large"),
            },
        )


# ============================================================
# 侧边栏
# ============================================================
def _sidebar() -> None:
    with st.sidebar:
        st.header("⚙️ 控制台")

        # 讲解风格：原样透传给 backend.answer 的 style 参数
        st.session_state.style = st.selectbox(
            "讲解风格",
            backend.STYLES,
            index=backend.STYLES.index(st.session_state.style),
            help="简洁：一句话定义 + 一个例子；详细：补推导过程与反例",
        )

        # 模型配置：只在「真的没配好」时提示（且不点名缺哪个变量）
        missing = backend.missing_config_keys()
        if missing:
            st.divider()
            if _DEBUG:
                st.caption("模型配置状态")
                st.warning("模型未配置")
                st.caption("缺：" + "、".join(missing))
                st.caption("复制 .env.example 为 .env，填好三项后重启页面生效。")
            else:
                st.warning(
                    "模型服务尚未配置，问答与拍照识别暂不可用。"
                    "配置 API Key 后重启页面即可。"
                )

        if _DEBUG:
            st.divider()
            st.caption("门面接入状态")
            side_status, side_bad, side_ratio, side_error = _facade_state()
            if side_error:
                st.error("接入状态读取失败：" + side_error)
            else:
                st.caption(side_ratio)
                if side_bad:
                    st.error(f"{len(side_bad)} 个函数接入异常，见顶部状态条")
                with st.expander("逐个函数", expanded=False):
                    for name, kind in side_status.items():
                        reason = side_bad.get(name)
                        st.caption(
                            ("✅ " if kind == "real" else "🧪 ")
                            + f"{name}（{kind}）"
                            + (f" · ❌ {reason}" if reason else "")
                        )
            st.caption("🧪 = 桩，✅ = 真实现，❌ = 模块在但接不上")

        with st.expander("📐 公式速查", expanded=False):
            st.latex(r"\lim_{x\to 0}\frac{\sin x}{x}=1")
            st.latex(r"\left(e^{x}\right)'=e^{x}")
            st.latex(r"\int u\,dv=uv-\int v\,du")

        st.divider()
        if not backend.db_ready():
            st.warning("系统尚未初始化，请先执行一次数据初始化。")

        if st.button("🗑 清空对话"):
            st.session_state.history = []
            st.session_state.pending_draft = None
            st.session_state.practice_items = None
            st.session_state.practice_result = None
            st.rerun()

        if _DEBUG:
            st.caption("v0.5 · 调试模式（TUTOR_DEBUG=1）")


# ============================================================
# ① 知识点问答
# ============================================================
def _tab_qa() -> None:
    st.subheader("① 知识点问答")
    st.caption("回答固定四段：一句话定义 / 通俗解释 / 具体例子 / 常见误区　｜　越界问题会被礼貌拒绝")

    with st.container(border=True):
        with st.chat_message("assistant"):
            st.markdown(OPENING)

        for msg in st.session_state.history:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])
                if msg["role"] == "assistant" and _DEBUG:
                    source = msg.get("source")
                    if source == "real":
                        st.caption("✅ 真实模型回答")
                    elif source == "error":
                        st.caption("⚠️ 这次没有拿到模型回答")
                    else:
                        st.caption("🧪 示例回答")

    prompt = (st.chat_input("问一个高等数学问题，例如：洛必达法则什么时候能用？") or "").strip()
    if not prompt:  # 空白或全空格不触发调用
        return

    st.session_state.history.append({"role": "user", "content": prompt})
    failed = False
    try:
        with st.spinner("推导中…"):
            reply = backend.answer(
                prompt,
                SUBJECT,
                st.session_state.style,
                # 只带最近几条，且不含刚追加的这条
                history=st.session_state.history[:-1][-HISTORY_WINDOW:],
            )
    except Exception as exc:  # noqa: BLE001 —— 模型没配/网络断，页面给提示而不是白屏
        failed = True
        if _DEBUG:
            reply = (
                f"⚠️ 这次调用失败了：{exc}\n\n"
                "检查侧边栏的「模型配置状态」缺哪几项，或稍后重试。"
            )
        else:
            reply = "这次没能拿到回答，请稍后重试或换个问法。"

    # 来源标记说的是「这一条回答是谁给的」，不是「门面现在接的是谁」。
    source = "error" if failed else backend.sources().get("answer", "stub")
    st.session_state.history.append({"role": "assistant", "content": reply, "source": source})
    st.rerun()


# ============================================================
# ② 错题录入
# ============================================================
def _tab_intake() -> None:
    st.subheader("② 错题录入")
    st.caption("第一步：把做错的题交进来（手输或拍照）→ 第二步：核对识别结果、改错、确认入库")

    if st.session_state.get("intake_flash"):
        st.success(st.session_state.pop("intake_flash"))

    mode = st.radio("录入方式", ["手输题干", "上传照片"], horizontal=True, key="intake_mode")
    # 换录入方式时清掉上一轮的草稿 —— 否则会拿「手输」识别出来的草稿，
    # 在「上传照片」页上确认入库，等于把 A 的识别结果当成 B 的。
    if st.session_state.get("_last_intake_mode") != mode:
        st.session_state["_last_intake_mode"] = mode
        st.session_state.pending_draft = None

    if mode == "手输题干":
        question = st.text_area(
            "题干（纯文本或 LaTeX，如 lim x->0 sin(x)/x）",
            height=90,
            placeholder="求极限 lim(x→0) sin3x/x",
        )
        col_s, col_c = st.columns(2)
        student_answer = col_s.text_area("我当时写的答案", height=70)
        correct_answer = col_c.text_input("参考答案（可留空，交给模型判）")

        question = question.strip()
        if not question:
            st.caption("题干必填：至少写一句题干，识别才有输入。")
        if st.button("识别并归类", type="primary", disabled=not question):
            with st.status("识别中：抽取题干 → 判定错因 → 匹配知识点", expanded=True) as state:
                st.write("① 抽取题干与答案")
                time.sleep(0.15)
                st.write("② 判定错因、设定难度")
                time.sleep(0.15)
                draft = backend.analyze_text(question, student_answer, correct_answer)
                st.write("③ 匹配知识点（命中就复用，没命中才建议新建）")
                st.session_state.pending_draft = draft
                st.session_state["draft_seq"] = st.session_state.get("draft_seq", 0) + 1
                state.update(label="识别完成", state="complete", expanded=False)
    else:
        upload = st.file_uploader(
            "上传题目照片（高数公式的 ∫、lim、上下限识别成功率有限，识别后务必人工核对）",
            type=["jpg", "jpeg", "png"],
        )
        if upload is not None:
            size_mb = upload.size / (1024 * 1024)
            st.image(upload, caption=f"{upload.name} · {size_mb:.1f} MB")
            if size_mb > MAX_IMAGE_MB:
                st.error(f"图片 {size_mb:.1f} MB 超过 {MAX_IMAGE_MB} MB 上限，先压缩或截图后再传。")
        too_big = upload is not None and upload.size / (1024 * 1024) > MAX_IMAGE_MB
        if st.button("识别并归类", type="primary", disabled=upload is None or too_big):
            if upload is None:  # 双保险：按钮禁用后仍防住空请求
                st.warning("先选一张图片再点识别。")
            else:
                with st.status("识别图片中…", expanded=True) as state:
                    st.write("① 上传图片、准备多模态请求")
                    time.sleep(0.15)
                    draft = backend.analyze_image(upload.getvalue(), extra_note="")
                    st.write("② 抽取题干 → 判定错因 → 匹配知识点")
                    st.session_state.pending_draft = draft
                    st.session_state["draft_seq"] = st.session_state.get("draft_seq", 0) + 1
                    state.update(label="识别完成", state="complete", expanded=False)

    draft = st.session_state.pending_draft
    if draft is None:
        st.caption("识别结果会出现在这里，每个字段都能改。")
        _recent_intake()
        return

    if getattr(draft, "degraded", False):
        st.warning("模型没识别成功。下面是空表单，请手工填写 —— 主流程不断。")

    st.divider()
    st.subheader("确认与修正")
    source_label = {"text": "手输", "image": "拍照"}.get(draft.source, draft.source)
    st.caption(
        f"识别来源：{source_label} · 置信度 {draft.confidence:.0%} · "
        f"错因判定：{backend.ERROR_TYPE_LABELS.get(draft.error_type, draft.error_type)}"
    )
    if draft.note:
        st.caption(f"识别备注：{draft.note}")

    kp_ids, kp_labels = _kp_options()
    if not kp_ids:
        st.caption("系统里还没有可用的知识点。")

    seq = st.session_state.get("draft_seq", 0)
    default_error = (
        draft.error_type if draft.error_type in backend.ERROR_TYPE_LABELS else "unknown"
    )
    default_difficulty = (
        draft.difficulty if draft.difficulty in backend.DIFFICULTY_LABELS else "medium"
    )
    suggestion_labels = [
        f"{c.get('name')}（{c.get('chapter_id')}）" for c in (draft.new_concepts or [])
    ]

    # 🔴 用 session_state 预置「初始值」，控件本身不传 value/default：
    #    传了 value 又带 key 时，Streamlit 每次 rerun 都会拿默认值覆盖用户输入，
    #    表现出来就是「改了字，一提交又变回识别结果」。
    st.session_state.setdefault(f"i{seq}_q", draft.question_text)
    st.session_state.setdefault(f"i{seq}_s", draft.student_answer)
    st.session_state.setdefault(f"i{seq}_c", draft.correct_answer)
    st.session_state.setdefault(f"i{seq}_steps", "\n".join(draft.solution_steps or []))
    st.session_state.setdefault(
        f"i{seq}_kp", [k for k in (draft.knowledge_ids or []) if k in kp_ids]
    )
    st.session_state.setdefault(f"i{seq}_err", default_error)
    st.session_state.setdefault(f"i{seq}_diff", default_difficulty)
    if suggestion_labels:
        st.session_state.setdefault(f"i{seq}_new", suggestion_labels)

    # 🔴 不用 st.form：表单内的控件值在提交前不会写回 session_state，
    #    Streamlit 的自动化测试也改不动它们 —— 验收时没法端到端验证。
    with st.container(border=True):
        question_text = st.text_area("题干", height=90, key=f"i{seq}_q")
        col_a, col_b = st.columns(2)
        student_answer = col_a.text_area("我的答案", height=70, key=f"i{seq}_s")
        correct_answer = col_b.text_area("正确答案", height=70, key=f"i{seq}_c")
        steps_text = st.text_area(
            "解题步骤（每行一步）",
            height=100,
            key=f"i{seq}_steps",
        )
        selected_kp = st.multiselect(
            "涉及知识点（只能选库里已有的；不合适的走下面的「建议新建」）",
            options=kp_ids,
            format_func=lambda k: kp_labels.get(k, k),
            key=f"i{seq}_kp",
        )
        col_c, col_d = st.columns(2)
        error_type = col_c.selectbox(
            "错因类型",
            options=list(backend.ERROR_TYPE_LABELS),
            format_func=lambda k: backend.ERROR_TYPE_LABELS[k],
            key=f"i{seq}_err",
        )
        difficulty = col_d.selectbox(
            "难度",
            options=list(backend.DIFFICULTY_LABELS),
            format_func=lambda k: backend.DIFFICULTY_LABELS[k],
            key=f"i{seq}_diff",
        )

        # 模型建议新建的知识点：默认全选，但用户能否决 —— 查重是启发式的，
        # 近义名（洛必达 / 洛必达法则）不会被合并，必须让人在提交前拦。
        checked_labels: list[str] = []
        if suggestion_labels:
            checked_labels = st.multiselect(
                "模型建议新建的知识点（勾选的才会创建）",
                options=suggestion_labels,
                key=f"i{seq}_new",
            )
            st.caption("已存在于库里的会自动识别复用，不会重复创建")

        submitted = st.button("确认入库", type="primary", key=f"i{seq}_submit")

    if submitted:
        # 用「表单里改过之后」的值造草稿，而不是识别时的快照。
        steps = [line.strip() for line in (steps_text or "").splitlines() if line.strip()]
        picked_concepts = [
            concept
            for concept, label in zip(draft.new_concepts or [], suggestion_labels)
            if label in checked_labels
        ]
        draft_out = types.SimpleNamespace(
            question_text=(question_text or "").strip(),
            student_answer=(student_answer or "").strip(),
            correct_answer=(correct_answer or "").strip(),
            solution_steps=steps,
            knowledge_ids=list(selected_kp or []),
            new_concepts=picked_concepts,
            error_type=error_type,
            difficulty=difficulty,
            source=getattr(draft, "source", "text") or "text",
            degraded=False,
        )
        try:
            new_id = backend.commit(draft_out)
        except ValueError as exc:
            # 业务规则（做对拒收 / 没挂知识点 / 题干为空 / 重复提交）由后端给出人话
            st.error(_user_text(str(exc), "这道题暂时没法入库，请检查填写内容。"))
        except Exception as exc:  # noqa: BLE001 —— 写库失败不该把页面打挂
            if _DEBUG:
                st.error(f"入库没成功：{type(exc).__name__}: {exc}")
            else:
                st.error("入库没成功，请稍后重试。")
        else:
            st.session_state.pending_draft = None
            st.session_state["intake_flash"] = f"已入库：错题 #{new_id}"
            st.rerun()

    _recent_intake()


def _recent_intake() -> None:
    """最近录入列表。db 已就绪就直接读真数据，没就绪就明确说缺什么。"""
    st.divider()
    st.subheader("最近录入")
    if not backend.db_ready():
        st.caption("系统尚未初始化，列表为空。")
        return
    try:
        rows = backend.recent_wrong_questions(5)
    except Exception as exc:  # noqa: BLE001
        st.warning("读取最近录入失败，请稍后重试。" + (f"（{exc}）" if _DEBUG else ""))
        return
    if not rows:
        st.caption("还没有录入过错题。")
        return
    # 逐行渲染而不是 st.dataframe：表格里放不了按钮，而删错题必须每行一个入口。
    for row in rows:
        wq_id = row.get("id")
        # 🔴 表格/文本不跑 KaTeX，直接显示 LaTeX 源码很难读 —— 只做展示降级，不动数据
        stem = ui_theme.plain_math(row.get("question_text"))[:60]
        names = "、".join(k["name"] for k in row.get("knowledge_points", [])) or "—"
        error_label = backend.ERROR_TYPE_LABELS.get(row.get("error_type"), row.get("error_type") or "—")
        info_col, action_col = st.columns([0.82, 0.18], vertical_alignment="center")
        with info_col:
            st.markdown(f"**#{wq_id}**　{stem}")
            st.caption(f"{names} · {error_label} · {row.get('created_at')}")
        with action_col:
            if st.button("删除", key=f"del_{wq_id}"):
                st.session_state["pending_delete"] = wq_id

    pending = st.session_state.get("pending_delete")
    if pending is not None:
        st.warning(f"确认删除错题 #{pending}？删除后不可恢复，知识点本身不受影响。")
        confirmed = st.checkbox("确认删除", key=f"confirm_delete_{pending}")
        col_yes, col_no = st.columns(2)
        if confirmed and col_yes.button("确认删除该题", type="primary", key=f"do_delete_{pending}"):
            try:
                deleted = backend.delete_wrong_question(int(pending))
            except Exception as exc:  # noqa: BLE001 —— 删库失败不该把页面打挂
                st.error("删除没成功，请稍后重试。" + (f"（{exc}）" if _DEBUG else ""))
            else:
                if deleted:
                    st.session_state["pending_delete"] = None
                    st.toast("已删除", icon="🗑")
                    st.rerun()
                else:
                    if backend.sources().get("delete_wrong_question") != "real":
                        # 门面还没接上真实现（C₁ 正在补 db.delete_wrong_question）
                        st.info("当前是示例模式，不会真的删除数据。")
                    else:
                        st.error("删除没成功：这道题可能已经被删掉了，刷新页面再看。")
        if col_no.button("取消", key=f"cancel_delete_{pending}"):
            st.session_state["pending_delete"] = None
            st.rerun()


# ============================================================
# ③ 掌握度分析
# ============================================================
def _tab_mastery() -> None:
    st.subheader("③ 掌握度分析")
    st.caption("练过不足 3 次一律「样本不足」，不参与最薄弱排名")
    st.latex(r"\text{掌握度}=\text{基础正确率}\ \times\ \text{难度加权}\ \times\ \text{时间衰减}\quad(\le 1.0)")

    try:
        df = backend.mastery_table()
        summary = backend.summary_text()
    except Exception as exc:  # noqa: BLE001
        st.error("掌握度数据读取失败，请稍后重试。" + (f"（{exc}）" if _DEBUG else ""))
        return

    # 契约守卫：列名/顺序被人改坏了，这里立刻报出来（给开发看，用户看红字也没用）
    missing_cols = [c for c in backend.MASTERY_COLUMNS if c not in df.columns]
    if missing_cols:
        if _DEBUG:
            st.error(
                "mastery_table() 的返回列与 docs/interfaces.md 第一节不一致，缺："
                + "、".join(missing_cols)
            )
        return
    df = df[backend.MASTERY_COLUMNS]

    missing_keys = [k for k in backend.SUMMARY_KEYS if k not in summary]
    if missing_keys and _DEBUG:
        st.error("summary_text() 缺键：" + "、".join(missing_keys))

    cards = [
        ("知识点总数", summary.get("kp_total", 0)),
        ("错题总数", summary.get("wrong_total", 0)),
        ("未掌握", summary.get("weak_count", 0)),
        ("练习次数", summary.get("practice_total", 0)),
    ]
    animate = _first_time("metrics_animated")
    for column, (label, value) in zip(st.columns(4), cards):
        with column:
            with st.container(border=True):
                if animate:
                    _count_up(st.empty(), label, int(value))
                else:
                    st.metric(label, int(value))
    st.caption(
        f"模型累计调用 {summary.get('llm_calls', 0)} 次 · "
        f"累计用量 {summary.get('llm_tokens', 0)}"
    )

    # 最薄弱：只在「未掌握 / 基本掌握」里挑掌握度最低的（样本不足不参与）
    # NaN 兜底：契约要求 0.0，但真实实现很容易因为除零/LEFT JOIN 给 NaN，
    # 而 st.progress(NaN) 会直接抛异常把整页打挂 —— 这里统一压到 [0, 1]。
    def _ratio(value) -> float:
        if pd.isna(value):
            return 0.0
        return min(max(float(value), 0.0), 1.0)

    candidates = df[df["status"].isin(["未掌握", "基本掌握"])].sort_values("mastery")
    with st.container(border=True):
        if len(candidates):
            top = candidates.iloc[0]
            mastery_ratio = _ratio(top["mastery"])
            st.markdown(f"**当前最薄弱**：{top['kp_name']}　·　{top['chapter_name']}")
            st.progress(mastery_ratio, text=f"掌握度 {mastery_ratio:.0%}")
        else:
            st.caption("暂时没有可排名的薄弱项 —— 先做几道练习。")

    view = df.assign(
        状态=df["status"].map(STATUS_ICONS).fillna(df["status"]),
        正确率=(df["correct_rate"].apply(_ratio) * 100).round(0),
        掌握度=(df["mastery"].apply(_ratio) * 100).round(0),
    )
    st.dataframe(
        view[["kp_name", "chapter_name", "attempts", "correct", "正确率", "掌握度", "状态"]],
        hide_index=True,
        column_config={
            "kp_name": st.column_config.TextColumn("知识点", width="medium"),
            "chapter_name": st.column_config.TextColumn("所属章节", width="medium"),
            "attempts": st.column_config.NumberColumn("练习", width="small"),
            "correct": st.column_config.NumberColumn("做对", width="small"),
            "正确率": st.column_config.ProgressColumn(
                "基础正确率", min_value=0, max_value=100, format="%d%%"
            ),
            "掌握度": st.column_config.ProgressColumn(
                "掌握度", min_value=0, max_value=100, format="%d%%"
            ),
            "状态": st.column_config.TextColumn("状态", width="small"),
        },
    )

    st.caption("各知识点基础正确率对比（%）")
    if len(view):
        # 柱状图交给 Altair 生成 spec（Streamlit 自带依赖）。
        # 竖排标签 + labelOverlap=False 强制全部显示；Y 轴固定 0~100。
        chart = (
            alt.Chart(view[["kp_name", "正确率"]])
            .mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
            .encode(
                x=alt.X(
                    "kp_name:N",
                    sort=None,
                    axis=alt.Axis(
                        title=None,
                        labelAngle=90,
                        labelAlign="left",
                        labelBaseline="middle",
                        labelFontSize=11,
                        labelLimit=0,
                        labelOverlap=False,
                        labelPadding=6,
                    ),
                ),
                y=alt.Y(
                    "正确率:Q",
                    scale=alt.Scale(domain=[0, 100]),
                    axis=alt.Axis(title=None, values=[0, 20, 40, 60, 80, 100]),
                ),
                tooltip=[
                    alt.Tooltip("kp_name:N", title="知识点"),
                    alt.Tooltip("正确率:Q", title="正确率（%）"),
                ],
            )
            .properties(width="container", height=360)
        )
        st.altair_chart(chart)
    else:
        st.caption("还没有知识点可以统计。")


# ============================================================
# ④ 针对性练习
# ============================================================
def _tab_practice() -> None:
    st.subheader("④ 针对性练习")
    st.caption("按薄弱知识点出题；做错的题会自动回流错题本")

    if st.button("生成一组练习", type="primary"):
        try:
            items, note = backend.build_practice_session(3)
        except Exception as exc:  # noqa: BLE001
            st.error("出题失败，请稍后重试。" + (f"（{exc}）" if _DEBUG else ""))
            return
        st.session_state.practice_items = items
        st.session_state.practice_note = note
        st.session_state.practice_result = None
        st.session_state.practice_graded = None  # 新一轮，重新允许提交
        st.toast(f"已生成 {len(items)} 道题", icon="📐")

    items = st.session_state.practice_items
    if not items:
        st.info("点上面的按钮生成一组练习。")
        return

    st.caption(_practice_note(st.session_state.practice_note))
    answers: dict[str, str] = {}
    for index, item in enumerate(items, start=1):
        with st.container(border=True):
            st.markdown(f"**{index}. {item.get('stem', '')}**")
            if item.get("type") == "choice":
                options = list(item.get("options") or {})
                answers[item["id"]] = st.radio(
                    f"第 {index} 题",
                    options,
                    index=None,  # 默认不预选：否则「什么都不点就提交」会拿第一个选项当答案
                    format_func=lambda key, opts=item["options"]: f"{key}. {opts[key]}",
                    key=f"choice_{item['id']}",
                    label_visibility="collapsed",
                )
            elif item.get("type") == "fill":
                answers[item["id"]] = st.text_input(
                    f"第 {index} 题",
                    key=f"fill_{item['id']}",
                    label_visibility="collapsed",
                    placeholder="填写答案，例如 x^2+C",
                )
            else:
                st.warning("这道题的题型暂不支持自动判分。")
            st.caption(
                f"难度：{backend.DIFFICULTY_LABELS.get(item.get('difficulty'), item.get('difficulty'))}"
            )

    # 🔴 同一轮只允许判分一次：record_result 会直接写 practice_log，
    #    重复点会让练习次数翻倍、掌握度被抬高、错题重复回流。
    signature = _practice_signature(items)
    already_graded = st.session_state.get("practice_graded") == signature
    if already_graded:
        st.info("本轮已判分，如需重做请重新生成练习")
    elif st.button("提交并判分"):
        # 未作答不判分：单选/填空都可能有默认值，直接提交等于拿默认值当答案，
        # 会产出假成绩，也会把假作答写进练习记录、污染掌握度。
        unanswered = [
            index
            for index, item in enumerate(items, start=1)
            if not str(answers.get(item["id"]) or "").strip()
        ]
        if unanswered:
            st.warning("这些题还没作答：" + "、".join(f"第 {i} 题" for i in unanswered))
            return

        try:
            results = backend.record_result(items, answers)
        except Exception as exc:  # noqa: BLE001
            st.error("判分失败，请稍后重试。" + (f"（{exc}）" if _DEBUG else ""))
            return

        # 数量对不上就别渲染：zip 会按短的截断，3 题可能显示成 2/2，比报错更糟
        received = len(results) if hasattr(results, "__len__") else "?"
        if not isinstance(results, list) or len(results) != len(items):
            st.error(f"判分结果数量不对（{len(items)} 题收到 {received} 个结果），已停止渲染以免给出错误成绩。")
            st.session_state.practice_result = None
            return

        st.session_state.practice_result = results
        st.session_state.practice_graded = signature  # 标记这一轮已经判过
        st.toast("判分完成", icon="🎯")

    results = st.session_state.practice_result
    if not results:
        return

    st.divider()
    correct_count = sum(1 for ok in results if ok)
    st.metric("本次得分", f"{correct_count} / {len(results)}")
    for index, (item, ok) in enumerate(zip(items, results), start=1):
        (st.success if ok else st.error)(f"第 {index} 题：{'答对了' if ok else '答错了'}")
        st.caption(f"正确答案：{item.get('answer')}")
        st.caption(f"解析：{item.get('explanation', '—')}")


# ============================================================
# 入口
# ============================================================
def main() -> None:
    _init_state()
    _sidebar()
    ui_theme.inject()
    _hero()
    _status_strip()

    if _DEBUG:
        status, _bad, _ratio, read_error = _facade_state()
        if read_error:
            pass  # 状态条已经报过
        elif backend.mode() == "stub":
            st.caption("🔧 TUTOR_BACKEND=stub：全部走示例数据，不会调用模型。")
        elif backend.mode() == "real":
            missing = [name for name, kind in status.items() if kind != "real"]
            if missing:
                st.error(
                    "**TUTOR_BACKEND=real（严格自查模式）**：以下函数还没接上真实现 —— "
                    + "、".join(f"`{name}`" for name in missing)
                )
        if any(kind == "stub" for kind in status.values()):
            st.caption("🧪 部分函数仍是桩，界面已跑通。")

    st.divider()

    tab_qa, tab_intake, tab_mastery, tab_practice = st.tabs(
        ["① 知识点问答", "② 错题录入", "③ 掌握度分析", "④ 针对性练习"]
    )
    with tab_qa:
        _tab_qa()
    with tab_intake:
        _tab_intake()
    with tab_mastery:
        _tab_mastery()
    with tab_practice:
        _tab_practice()


main()
