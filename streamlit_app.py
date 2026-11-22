"""界面入口 —— 学习辅导 AI 助教 · 高等数学（Streamlit）。

维护人：D。契约：docs/interfaces.md 第八节。

三条硬规矩（本文件的自查口径，改代码前先看这三条）
------------------------------------------------------------
1. 🔴 不写 SQL。要数据一律调 backend.xxx()，SQL 只在 app/db.py 里。
2. 🔴 不发网络请求。全项目只有 app/llm_client.py 能发，
   所以这里不 import openai / requests / httpx / urllib。
3. 🔴 不引 HTML/CSS/JS 或前端框架。只用 Streamlit 原生组件，
   不用 unsafe_allow_html、不塞 <script>、不自己画 CSS。

后端怎么接
------------------------------------------------------------
只 `from app import stubs as backend`，全程 `backend.xxx()`。
app/stubs.py 里是门面：队友的 qa.py / intake.py / profile.py 一进 main，
同一条调用会自动走真实现，本文件一行都不用改。
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app import stubs as backend

SUBJECT = "高等数学"

#: 传给模型的对话历史窗口（条数，user+assistant 合计）。
#: 界面显示全部历史，但只把最近几条发给模型 —— 不截断的话聊久了会把
#: 上下文和费用一起顶爆，而且早期闲聊对回答没有帮助。
HISTORY_WINDOW = 6

#: 上传图片的大小上限（MB）。超了就提示先压缩 —— 别把 10 MB 的手机原图
#: 塞进 base64 再发模型，token 和等待时间都会爆。
MAX_IMAGE_MB = 5

#: 开场白：只做页面渲染，🔴 绝不写进 st.session_state.history。
#: 原因：发给模型的 messages 序列若以 assistant 开头，多数模型的
#: chat template 会直接报 "No user query found in messages"。
OPENING = (
    "你好，我是这门《高等数学》的辅导助教。\n\n"
    "可以问我：概念怎么理解、这一步为什么这么变形、某道错题错在哪。\n\n"
    "只回答高等数学范围内的内容；范围外的问题我会礼貌拒绝并把你引回课程。"
)

st.set_page_config(
    page_title="学习辅导 AI 助教 · 高等数学",
    page_icon="📘",
    layout="wide",
)


# ============================================================
# 公共小工具
# ============================================================
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
# 侧边栏
# ============================================================
def _sidebar() -> None:
    with st.sidebar:
        st.header("⚙️ 设置")

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
        st.caption("数据来源（门面当前接到的实现）")
        status = backend.sources()
        n_real = sum(1 for v in status.values() if v == "real")
        st.caption(f"真实现 {n_real} 项 · 桩 {len(status) - n_real} 项")
        with st.expander("逐个函数", expanded=False):
            for name, kind in status.items():
                st.caption(("✅ " if kind == "real" else "🧪 ") + f"{name}（{kind}）")
        st.caption("🧪 = 桩，✅ = 队友的真实实现已就位")

        st.divider()
        if not backend.db_ready():
            st.warning("数据库还没建：先跑 `python seed_data.py`（第 5 步）")

        if st.button("🗑 清空对话"):
            st.session_state.history = []
            st.session_state.pending_draft = None
            st.session_state.practice_items = None
            st.session_state.practice_result = None
            st.rerun()


# ============================================================
# ① 知识点问答
# ============================================================
def _tab_qa() -> None:
    st.subheader("① 知识点问答")
    st.caption("回答固定四段：一句话定义 / 通俗解释 / 具体例子 / 常见误区")

    with st.chat_message("assistant"):
        st.markdown(OPENING)

    for msg in st.session_state.history:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg["role"] == "assistant":
                # 回答来源：演示/答辩时要能当场说清"这是桩还是真模型"
                if msg.get("source") == "real":
                    st.caption("✅ 真实模型回答")
                else:
                    st.caption("🧪 桩回答（`app/qa.py` 就绪后自动切真实现）")

    prompt = (st.chat_input("问一个高等数学问题，例如：洛必达法则什么时候能用？") or "").strip()
    if not prompt:  # 空白或全空格不触发调用
        return

    st.session_state.history.append({"role": "user", "content": prompt})
    try:
        with st.spinner("思考中…"):
            reply = backend.answer(
                prompt,
                SUBJECT,
                st.session_state.style,
                # 只带最近几条，且不含刚追加的这条
                history=st.session_state.history[:-1][-HISTORY_WINDOW:],
            )
    except Exception as exc:  # noqa: BLE001 —— 模型没配/网络断，页面给提示而不是白屏
        reply = (
            f"⚠️ 这次调用失败了：{exc}\n\n"
            "检查侧边栏的「模型配置状态」缺哪几项，或稍后重试。"
        )

    st.session_state.history.append(
        {
            "role": "assistant",
            "content": reply,
            "source": backend.sources().get("answer", "stub"),
        }
    )
    st.rerun()


# ============================================================
# ② 错题录入
# ============================================================
def _tab_intake() -> None:
    st.subheader("② 错题录入")
    st.caption("第一步：把做错的题交进来（手输或拍照）→ 第二步：核对识别结果、改错、确认入库")

    mode = st.radio("录入方式", ["手输题干", "上传照片"], horizontal=True)

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
            with st.spinner("正在抽取题干、判定错因、匹配知识点…"):
                st.session_state.pending_draft = backend.analyze_text(
                    question, student_answer, correct_answer
                )
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
        if st.button("识别并归类", type="primary", disabled=upload is None):
            if upload is None:  # 双保险：按钮禁用后仍防住空请求
                st.warning("先选一张图片再点识别。")
            else:
                with st.spinner("正在识别图片…"):
                    st.session_state.pending_draft = backend.analyze_image(
                        upload.getvalue(), extra_note=""
                    )

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
    st.caption("掌握度 = 基础正确率 × 难度加权 × 时间衰减；练过不足 3 次一律「样本不足」，不参与最薄弱排名")

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

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("知识点总数", summary.get("kp_total", 0))
    c2.metric("错题总数", summary.get("wrong_total", 0))
    c3.metric("未掌握", summary.get("weak_count", 0))
    c4.metric("练习次数", summary.get("practice_total", 0))
    st.caption(
        f"模型累计调用 {summary.get('llm_calls', 0)} 次 · "
        f"累计 token {summary.get('llm_tokens', 0)}"
    )

    # 最薄弱：只在「未掌握 / 基本掌握」里挑掌握度最低的（样本不足不参与）
    candidates = df[df["status"].isin(["未掌握", "基本掌握"])].sort_values("mastery")
    if len(candidates):
        top = candidates.iloc[0]
        st.info(f"当前最薄弱的知识点：**{top['kp_name']}**（{top['chapter_name']}，掌握度 {top['mastery']:.0%}）")
    else:
        st.info("暂时没有可排名的薄弱项 —— 先做几道练习。")

    st.dataframe(df, hide_index=True)

    st.caption("各知识点基础正确率对比")
    st.bar_chart(df.set_index("kp_name")[["correct_rate"]])


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

    items = st.session_state.practice_items
    if not items:
        st.info("点上面的按钮生成一组练习。本轮题目来自桩数据，用于验证答题与判分的排版。")
        return

    st.caption(st.session_state.practice_note)
    answers: dict[str, str] = {}
    for index, item in enumerate(items, start=1):
        st.markdown(f"**{index}. {item.get('stem', '')}**")
        if item.get("type") == "choice":
            options = list(item.get("options") or {})
            answers[item["id"]] = st.radio(
                f"第 {index} 题",
                options,
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
        st.caption(f"难度：{backend.DIFFICULTY_LABELS.get(item.get('difficulty'), item.get('difficulty'))}")

    if st.button("提交并判分"):
        try:
            st.session_state.practice_result = backend.record_result(items, answers)
        except Exception as exc:  # noqa: BLE001
            st.error(f"判分失败：{exc}")
            return

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

    st.title("📘 学习辅导 AI 助教 · 高等数学")
    st.caption("知识点问答 / 错题录入与归类 / 掌握度分析 / 针对性练习")

    status = backend.sources()
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
