"""界面入口 —— 学习辅导 AI 助教 · 高等数学（Streamlit）。

维护人：D。契约：docs/interfaces.md 第八节。

三条硬规矩（本文件的自查口径，改代码前先看这三条）
------------------------------------------------------------
1. 🔴 不写 SQL。要数据一律调 backend.xxx()，SQL 只在 app/db.py 里。
2. 🔴 不发网络请求。全项目只有 app/llm_client.py 能发，
   所以这里不 import openai / requests / httpx / urllib。
3. 🔴 不引 HTML/CSS/JS 或前端框架。所有视觉都靠 Streamlit 原生组件：
   卡片用 container(border=True)、公式用 st.latex（KaTeX）、
   进度用 ProgressColumn / st.progress、动效靠 st.empty() 定时重绘。
   不写 unsafe_allow_html，不塞 <style>/<script>。

视觉效果怎么做的（没有一行 CSS）
------------------------------------------------------------
· 公式主视觉：hero 三条数学 + 一条轮播公式（首次进入播一次）
· 卡片层次：container(border=True) 分组，深色主题由 .streamlit 配置决定
· 数值动感：指标卡首次渲染时从 0 滚到真实值（每会话一次，约 0.3 秒）
· 过程动感：识别走 st.status 分步状态、判分后 st.toast 弹提示
· 掌握度：ProgressColumn 显示进度条，状态用 🔴🟡🟢⚪ 四档图标

后端怎么接
------------------------------------------------------------
只 `from app import stubs as backend`，全程 `backend.xxx()`。
app/stubs.py 里是门面：队友的 qa.py / intake.py / profile.py 一进 main，
同一条调用会自动走真实现，本文件一行都不用改。
"""

from __future__ import annotations

import time

import altair as alt
import pandas as pd
import streamlit as st

from app import stubs as backend

SUBJECT = "高等数学"

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


def _kp_options() -> tuple[list[str], dict[str, str]]:
    """知识点下拉框的选项：只允许选库里已有的，不许自由输入（第 8 步的硬要求）。

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
        "practice_items": None,  # ④ 页当前这组题
        "practice_note": "",  # ④ 页的题目来源说明
        "practice_result": None,  # ④ 页最近一次判分结果
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
    """读门面状态，返回 (逐函数状态, 异常项, 一行汇总, 读取失败原因)。

    导入期异常（语法错误、模块顶层就抛错）会让 `sources()` 直接抛出来，
    那不是「接不上」而是「读不出来」——在这里兜住并显示红字，
    否则整页会变成 traceback。真正该改的还是 `_real()` 的异常口径（归 D 但需拍板）。
    """
    try:
        status = backend.sources()
        bad = backend.problems()
        ratio = backend.summary()
    except Exception as exc:  # noqa: BLE001 —— 界面兜底，别让导入期异常把页面打白
        return {}, {}, "", f"{type(exc).__name__}: {exc}"
    return status, bad, ratio, ""


def _status_strip() -> None:
    """一行系统状态 + 门面接入详情 —— 让「门面到底接到了什么」看得见。

    比例与异常都通过 `backend.summary()` / `backend.problems()` 拿，
    界面不自己数 —— 否则会出现两份口径，谁也说不清哪份是对的。
    """
    ready = backend.db_ready()
    try:
        chapters = len(backend.chapter_candidates()) if ready else 0
        kps = len(backend.all_knowledge_points()) if ready else 0
        wrong = backend.wrong_question_count() if ready else 0
    except Exception:  # noqa: BLE001
        chapters = kps = wrong = 0

    status, bad, ratio, read_error = _facade_state()
    if read_error:
        st.error(
            "**门面状态读不出来**：" + read_error
            + "\n\n正常情况下导入期异常会被门面记成「接入异常」，能走到这里说明连 stubs 的兜底也失效了。"
        )
        return

    bits = [
        f"◈ 章节 **{chapters}**",
        f"◈ 知识点 **{kps}**",
        f"◈ 错题 **{wrong}**",
        f"◈ 接入 **{ratio}**",
        f"◈ 风格 **{st.session_state.style}**",
    ]
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
        st.caption("✅ = 已接上队友的真实现；🧪 = 仍在跑桩。异常原因非空才是问题，空着说明队友只是还没写。")


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

        st.divider()
        st.caption("模型配置状态")
        missing = backend.missing_config_keys()
        if missing:
            st.warning("模型未配置")
            st.caption("缺：" + "、".join(missing))
            st.caption("复制 .env.example 为 .env，填好三项后重启页面生效。")
        else:
            st.success("模型已配置")

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
            st.warning("数据库还没建：先跑 `python seed_data.py`（第 5 步）")

        if st.button("🗑 清空对话"):
            st.session_state.history = []
            st.session_state.pending_draft = None
            st.session_state.practice_items = None
            st.session_state.practice_result = None
            st.rerun()

        st.caption("v0.4 · 界面桩阶段（模块落地后自动切真实现）")


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
                if msg["role"] == "assistant":
                    # 回答来源：演示/答辩时要能当场说清"这是桩、真模型，还是这次失败了"
                    source = msg.get("source")
                    if source == "real":
                        st.caption("✅ 真实模型回答")
                    elif source == "error":
                        st.caption("⚠️ 这次没有拿到模型回答（错误信息见上方）")
                    else:
                        st.caption("🧪 桩回答（`app/qa.py` 就绪后自动切真实现）")

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
        reply = (
            f"⚠️ 这次调用失败了：{exc}\n\n"
            "检查侧边栏的「模型配置状态」缺哪几项，或稍后重试。"
        )

    # 来源标记说的是「这一条回答是谁给的」，不是「门面现在接的是谁」：
    # 真实现抛错时门面依然是 real，但这条消息是错误提示，不能标成 ✅ 真实回答。
    source = "error" if failed else backend.sources().get("answer", "stub")
    st.session_state.history.append({"role": "assistant", "content": reply, "source": source})
    st.rerun()


# ============================================================
# ② 错题录入
# ============================================================
def _tab_intake() -> None:
    st.subheader("② 错题录入")
    st.caption("第一步：把做错的题交进来（手输或拍照）→ 第二步：核对识别结果、改错、确认入库")

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
        st.caption("数据库里还没有知识点。先跑 `python seed_data.py`，知识点会在问答/录入过程中自己长出来。")

    with st.container(border=True):
        with st.form("intake_confirm_form"):
            question_text = st.text_area("题干", value=draft.question_text, height=90)
            col_a, col_b = st.columns(2)
            student_answer = col_a.text_area("我的答案", value=draft.student_answer, height=70)
            correct_answer = col_b.text_area("正确答案", value=draft.correct_answer, height=70)
            steps_text = st.text_area(
                "解题步骤（每行一步）",
                value="\n".join(draft.solution_steps or []),
                height=100,
            )
            selected_kp = st.multiselect(
                "涉及知识点（只能选库里已有的；不合适的走下面的「建议新建」）",
                options=kp_ids,
                default=[k for k in (draft.knowledge_ids or []) if k in kp_ids],
                format_func=lambda k: kp_labels.get(k, k),
            )
            col_c, col_d = st.columns(2)
            error_type = col_c.selectbox(
                "错因类型",
                options=list(backend.ERROR_TYPE_LABELS),
                index=list(backend.ERROR_TYPE_LABELS).index(draft.error_type)
                if draft.error_type in backend.ERROR_TYPE_LABELS
                else len(backend.ERROR_TYPE_LABELS) - 1,
                format_func=lambda k: backend.ERROR_TYPE_LABELS[k],
            )
            difficulty = col_d.selectbox(
                "难度",
                options=list(backend.DIFFICULTY_LABELS),
                index=list(backend.DIFFICULTY_LABELS).index(draft.difficulty)
                if draft.difficulty in backend.DIFFICULTY_LABELS
                else 1,
                format_func=lambda k: backend.DIFFICULTY_LABELS[k],
            )
            if draft.new_concepts:
                st.caption("模型建议新建的知识点（是否真建由 C₁ 的 find_or_create 查重后决定）：")
                for concept in draft.new_concepts:
                    st.caption(f"· {concept.get('name')} → {concept.get('chapter_id')}")
            submitted = st.form_submit_button("确认入库")

    if submitted:
        st.toast("表单已提交（本轮还没写库）", icon="🧪")
        st.info(
            "本轮是第 4 步：表单能改、能提交，但**还不会真的写库**。\n\n"
            "第 8 步接上 `backend.commit(draft)` 后，这里就是唯一的落库入口。"
        )

    _recent_intake()


def _recent_intake() -> None:
    """最近录入列表。db 已就绪就直接读真数据，没就绪就明确说缺什么。"""
    st.divider()
    st.subheader("最近录入")
    if not backend.db_ready():
        st.caption("数据库尚未初始化，列表为空。跑过 `python seed_data.py` 后这里会显示真实记录（第 8 步接 `db.recent_wrong_questions()`）。")
        return
    try:
        rows = backend.recent_wrong_questions(5)
    except Exception as exc:  # noqa: BLE001
        st.warning(f"读取最近录入失败：{exc}")
        return
    if not rows:
        st.caption("还没有录入过错题。")
        return
    table = pd.DataFrame(
        [
            {
                "编号": row.get("id"),
                "题干": (row.get("question_text") or "")[:40],
                "知识点": "、".join(k["name"] for k in row.get("knowledge_points", [])) or "—",
                "错因": backend.ERROR_TYPE_LABELS.get(row.get("error_type"), row.get("error_type") or "—"),
                "时间": row.get("created_at"),
            }
            for row in rows
        ]
    )
    st.dataframe(table, hide_index=True)


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
        st.error(f"读取掌握度失败：{exc}")
        return

    # 契约守卫：列名/顺序被人改坏了，这里立刻报出来，别让它静默渲染出一张错表
    missing_cols = [c for c in backend.MASTERY_COLUMNS if c not in df.columns]
    if missing_cols:
        st.error(
            "mastery_table() 的返回列与 docs/interfaces.md 第一节不一致，缺："
            + "、".join(missing_cols)
        )
        return
    df = df[backend.MASTERY_COLUMNS]

    missing_keys = [k for k in backend.SUMMARY_KEYS if k not in summary]
    if missing_keys:
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
        f"累计 token {summary.get('llm_tokens', 0)}"
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
        # 柱状图交给 Altair 生成 spec（Streamlit 自带依赖，渲染走 st.altair_chart）。
        # 之前手写 vega spec 有两个坑：
        #   ① "padding": 5 让 ArrowVegaLiteChart 主题合并时报
        #      "Cannot create property 'bottom' on number '5'"
        #   ② 手写 axis 的旋转/裁切组合会把 x 轴标签整条吞掉
        # 现在竖排标签（labelAngle=90）+ labelOverlap=False 强制全部显示，
        # 布局与轴区尺寸交给库算（autosize: fit + contains: padding）。
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
    st.caption("按薄弱知识点出题；做错的题会回流错题本（第 9 步接 C₂ 的完整链路）")

    if st.button("生成一组练习", type="primary"):
        try:
            items, note = backend.build_practice_session(3)
        except Exception as exc:  # noqa: BLE001
            st.error(f"出题失败：{exc}")
            return
        st.session_state.practice_items = items
        st.session_state.practice_note = note
        st.session_state.practice_result = None
        st.toast(f"已生成 {len(items)} 道题", icon="📐")

    items = st.session_state.practice_items
    if not items:
        st.info("点上面的按钮生成一组练习。本轮题目来自桩数据，用于验证答题与判分的排版。")
        return

    st.caption(st.session_state.practice_note)
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
                st.warning(f"未知题型：{item.get('type')}（契约 v1 只支持 choice / fill）")
            st.caption(
                f"难度：{backend.DIFFICULTY_LABELS.get(item.get('difficulty'), item.get('difficulty'))}"
            )

    if st.button("提交并判分"):
        # 未作答不判分：单选/填空都可能有默认值，直接提交等于拿默认值当答案，
        # 会产出假成绩（等 C₂ 的 record_result 落地后还会写进 practice_log，污染掌握度）。
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
            st.error(f"判分失败：{exc}")
            return

        # 数量对不上就别渲染：zip 会按短的截断，3 题可能显示成 2/2，比报错更糟
        received = len(results) if hasattr(results, "__len__") else "?"
        if not isinstance(results, list) or len(results) != len(items):
            st.error(f"判分结果数量不对（{len(items)} 题收到 {received} 个结果），已停止渲染以免给出错误成绩。")
            st.session_state.practice_result = None
            return

        st.session_state.practice_result = results
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
    _hero()
    _status_strip()

    # 模式提示：stub 是演示开关（没配密钥也能跑），real 是交付前自查（缺一个就报错）
    status, _bad, _ratio, read_error = _facade_state()
    if read_error:
        pass  # 状态条已经把读取失败的原因打在页面上了，这里不重复刷屏
    elif backend.mode() == "stub":
        st.caption("🔧 `TUTOR_BACKEND=stub`：当前全部走桩数据，不会调用模型。")
    elif backend.mode() == "real":
        missing = [name for name, kind in status.items() if kind != "real"]
        if missing:
            st.error(
                "**TUTOR_BACKEND=real（严格自查模式）**：以下函数还没接上真实现 —— "
                + "、".join(f"`{name}`" for name in missing)
            )

    st.divider()

    if any(kind == "stub" for kind in status.values()):
        st.caption(
            "🧪 界面已跑通，部分函数仍是桩（见侧边栏「数据来源」）。"
            "队友的模块合进 main 后页面会自动切到真实数据，界面代码不用改。"
        )

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
