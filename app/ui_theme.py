"""纸张 / 新粗野主义（neo-brutalist）材质层 —— 只做外观，可整段删除回退。

================================================================
参考的视觉语言
================================================================
米色纸底 + 细网格、2px 墨线描边、硬投影（错位黑块）、
低饱和的薄荷 / 粉 / 蓝 / 卡其色块、方正小圆角、加粗标题、
小号大写分组标签。没有任何插画或图形元素，靠线、块、留白立层级。

================================================================
为什么必须注入 CSS
================================================================
Streamlit 只开放四个主题色，border/阴影/硬投影/网格底/标签页造型
都不在配置面上，组件是 React 渲染的，只能注入一段 CSS 改外观。
仓库 `streamlit_app.py` 文件头写着「不写 unsafe_allow_html、不塞 <style>」，
所以做折中：全部 CSS 只在本文件，`streamlit_app.py` 只加两行。

回退：删本文件 + `.streamlit/config.toml` + `streamlit_app.py` 那两行，
界面回到纯原生样式，业务代码一行不用改。

================================================================
注意
================================================================
· 只改视觉，不动任何逻辑、数据、函数签名、文案与组件数量。
· 告警块不要用 backdrop-filter；stAlertContent* 自带底色，必须单独覆盖。
"""

from __future__ import annotations

import re

import streamlit as st

# ============================================================
# 调色板（米色纸 + 墨线 + 四块低饱和色）
# ============================================================
PAPER = "#F5EFE1"
CARD = "#FBF7EC"
INK = "#141414"
MINT = "#DCEBDD"
PINK = "#F6DCDC"
BLUE = "#DCE8F3"
TAN = "#F3E3C3"
HILITE = "#FFE8A3"
MUTED = "#5C5548"


_CSS = """
:root{
  --paper:#F5EFE1;
  --card:#FBF7EC;
  --ink:#141414;
  --mint:#DCEBDD;
  --pink:#F6DCDC;
  --blue:#DCE8F3;
  --tan:#F3E3C3;
  --hilite:#FFE8A3;
  --muted:#5C5548;
  --line:2px solid #141414;
  --shadow:5px 5px 0 #141414;
  --shadow-sm:3px 3px 0 #141414;
  --font: "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", -apple-system,
          "Segoe UI", Inter, system-ui, sans-serif;
}

/* ---------- 纸底 + 网格 + 极轻噪点 ---------- */
html, body{ background: var(--paper) !important; }
body{
  font-family: var(--font);
  color: var(--ink);
  -webkit-font-smoothing: antialiased;
}
body::before{
  content:""; position:fixed; inset:0; z-index:-1; pointer-events:none;
  background-image:
    repeating-linear-gradient(0deg, rgba(20,20,20,.045) 0 1px, transparent 1px 28px),
    repeating-linear-gradient(90deg, rgba(20,20,20,.045) 0 1px, transparent 1px 28px);
}
body::after{
  content:""; position:fixed; inset:0; z-index:-1; pointer-events:none; opacity:.05;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='140' height='140' filter='url(%23n)'/%3E%3C/svg%3E");
  mix-blend-mode:multiply;
}

/* ---------- App 透明化 ---------- */
.stApp{ background: transparent !important; }
[data-testid="stAppViewContainer"]{ background: transparent !important; }
[data-testid="stHeader"]{ background: transparent !important; height:0; }
#MainMenu, footer{ visibility:hidden; }

/* ---------- 栅格 ---------- */
[data-testid="stMainBlockContainer"], .block-container{
  max-width:1040px; padding:2.2rem 24px 4rem;
}
@media (max-width:767px){
  [data-testid="stMainBlockContainer"], .block-container{ padding:1.3rem 20px 3rem; }
}
[data-testid="stVerticalBlock"]{ gap:.95rem; }

/* ---------- 文字 ---------- */
h1, [data-testid="stHeadingWithActionElements"] h1{
  font-size:34px !important; font-weight:800 !important;
  letter-spacing:-.02em; color:var(--ink); line-height:1.2;
  margin-bottom:.2rem;
}
h2, [data-testid="stHeadingWithActionElements"] h2{
  font-size:19px !important; font-weight:800 !important; letter-spacing:-.01em;
  color:var(--ink);
}
h3{ font-size:15px !important; font-weight:700 !important; color:var(--ink); }
p, li{ color:#2B2721; }
a{ color:var(--ink); text-decoration:underline; text-underline-offset:3px; }
hr{ border:none !important; border-top:var(--line) !important; margin:1rem 0 !important; }

/* 小号大写分组标签 */
[data-testid="stCaptionContainer"], .stCaption{
  font-size:12px !important; line-height:1.6 !important; letter-spacing:.06em !important;
  color:var(--muted) !important;
}

/* ---------- 卡片 ----------
   ⚠️ Streamlit 1.67 已不再产出 data-testid="stVerticalBlockBorderWrapper"
   （扫前端 bundle 0 次命中），所以 st.container(border=True) 只能沿用
   它自带的细边框。这里保留旧选择器兼容老版本 Streamlit。

   曾经试过用 :has() 给「含公式的元素块」补纸卡，但那会把练习题干里
   的行内公式（$...$）也一起套上黑框+投影，属于过度命中，已撤回。 */
[data-testid="stVerticalBlockBorderWrapper"]{
  background: var(--card) !important;
  border: var(--line) !important;
  border-radius:4px !important;
  box-shadow: var(--shadow) !important;
}
[data-testid="stVerticalBlockBorderWrapper"] > div{ padding:.4rem .35rem; }

/* 1.67 兜底：只有「容器块里装的是独立公式（.katex-display）」才上纸卡。
   用 .katex-display 而不是 .katex，是为了不误伤练习题干里的行内公式 $...$。 */
[data-testid="stElementContainer"] > [data-testid="stVerticalBlock"]:has(.katex-display){
  background: var(--card) !important;
  border: var(--line) !important;
  border-radius:4px !important;
  box-shadow: var(--shadow) !important;
  padding:10px 12px !important;
}

/* ---------- 侧边栏 ---------- */
[data-testid="stSidebar"]{
  background: #EFE7D6 !important;
  border-right: var(--line) !important;
}
[data-testid="stSidebar"] *{ color: var(--ink); }
[data-testid="stSidebar"] hr{ border-top:1px solid rgba(20,20,20,.35) !important; }

/* ---------- 标签页：方块分段 ---------- */
[data-testid="stTabs"] [role="tablist"]{
  gap:8px; background:transparent; border:none; padding:0;
}
[data-testid="stTabs"] [role="tab"]{
  border: var(--line) !important;
  background: var(--card) !important;
  color: var(--ink) !important;
  border-radius:4px !important;
  padding:.42rem .8rem !important;
  font-size:13px !important; font-weight:700 !important;
  box-shadow: var(--shadow-sm) !important;
  transition: transform 160ms ease, box-shadow 160ms ease, background 160ms ease;
}
[data-testid="stTabs"] [role="tab"]:hover{ background: var(--hilite) !important; }
[data-testid="stTabs"] [role="tab"][aria-selected="true"]{
  background: var(--mint) !important;
  transform: translate(2px,2px);
  box-shadow: 1px 1px 0 #141414 !important;
}
[data-baseweb="tab-highlight"], [data-baseweb="tab-border"]{ display:none !important; }
[data-testid="stTabs"] [role="tabpanel"]{ padding-top:1rem; }

/* ---------- 数据表 ---------- */
[data-testid="stDataFrame"]{
  border: var(--line) !important;
  border-radius:4px !important;
  overflow:hidden !important;
  background:#FFFDF6 !important;
  box-shadow: var(--shadow-sm) !important;
}

/* ---------- 进度条（按 Streamlit 1.67 真实 DOM 精确命中） ----------
   结构：stProgress
           ├─ div（文字标签，StreamlitMarkdown）
           └─ [role=progressbar] > [data-testid=stProgressBarTrack] > div（填充）
   之前用 stProgress > div 泛匹配，把「文字块」也压成 10px 高并加上边框，
   文字溢出到进度条里、又被填充块盖住 —— 这里只对轨道和填充生效。 */
[data-testid="stProgress"]{ background: transparent !important; border:none !important; height:auto !important; }
[data-testid="stProgress"] > div:first-child{
  height:auto !important; background: transparent !important;
  border:none !important; box-shadow:none !important;
  padding:0 0 6px !important;
}
[data-testid="stProgress"] p{
  margin:0 !important; color: var(--ink) !important;
  font-size:12px !important; line-height:1.45 !important; letter-spacing:.02em;
}
[data-testid="stProgress"] [role="progressbar"]{
  background: transparent !important; border:none !important;
  height:auto !important; padding:0 !important;
}
[data-testid="stProgressBarTrack"]{
  background:#E7DECB !important;
  border:1px solid var(--ink) !important;
  border-radius:2px !important;
  height:10px !important;
  overflow:hidden !important;
  box-shadow:none !important;
}
[data-testid="stProgressBarTrack"] > div{
  background: var(--mint) !important;
  border-right:1px solid var(--ink) !important;
  border-radius:0 !important;
}

/* ---------- 指标 ---------- */
[data-testid="stMetricLabel"], [data-testid="stMetricLabel"] *{
  font-size:11px !important; letter-spacing:.1em !important; text-transform:uppercase;
  color:var(--muted) !important; font-weight:700 !important;
}
[data-testid="stMetricValue"], [data-testid="stMetricValue"] *{
  font-size:38px !important; font-weight:800 !important; letter-spacing:-.03em;
  color:var(--ink) !important; font-variant-numeric: tabular-nums;
}

/* ---------- 展开器 / 表单 ---------- */
[data-testid="stExpander"]{
  border: var(--line) !important; border-radius:4px !important;
  background: var(--card) !important;
  box-shadow: var(--shadow-sm) !important;
  overflow:hidden;
}
[data-testid="stExpander"] summary{
  font-size:12.5px !important; font-weight:700 !important; color:var(--ink) !important;
  padding:.5rem .6rem !important;
}
[data-testid="stExpander"] summary:hover{ background: var(--hilite) !important; }
[data-testid="stForm"]{
  border: var(--line) !important; border-radius:4px !important;
  background:#FFFDF6 !important;
  box-shadow: var(--shadow-sm) !important;
  padding:1rem !important;
}

/* ---------- 输入控件 ---------- */
[data-baseweb="input"], [data-baseweb="textarea"], [data-baseweb="select"] > div{
  background:#FFFDF6 !important;
  border:1px solid var(--ink) !important;
  border-radius:3px !important;
  color:var(--ink) !important;
  box-shadow: inset 0 0 0 1px rgba(20,20,20,.06) !important;
}
[data-baseweb="input"]:focus-within, [data-baseweb="textarea"]:focus-within,
[data-baseweb="select"] > div:focus-within{
  outline:2px dashed var(--ink); outline-offset:2px;
}
input, textarea{ color:var(--ink) !important; background:transparent !important; }
input::placeholder, textarea::placeholder{ color:#8C8477 !important; }
[data-testid="stWidgetLabel"] p, [data-testid="stWidgetLabel"] label{
  font-size:12px !important; font-weight:700 !important; color:var(--ink) !important;
}

/* 下拉浮层 */
[data-baseweb="popover"] [data-baseweb="menu"], [data-baseweb="popover"] ul{
  background:#FFFDF6 !important;
  border: var(--line) !important;
  border-radius:3px !important;
  box-shadow: var(--shadow) !important;
}
[data-baseweb="popover"] li:hover{ background: var(--mint) !important; }

/* 多选标签 */
[data-testid="stMultiSelect"] [data-baseweb="tag"]{
  background: var(--tan) !important;
  border:1px solid var(--ink) !important;
  border-radius:2px !important; color:var(--ink) !important;
}

/* 单选 */
[data-testid="stRadio"] label{ font-weight:600 !important; color:var(--ink) !important; }

/* ---------- 按钮：多色，文字始终墨色（不被底色影响） ---------- */
/* 1) 次级按钮（如「提交并判分」）：雾蓝 */
[data-testid="stButton"] button,
[data-testid="stBaseButton-secondary"]{
  background: var(--blue) !important;
  border: var(--line) !important;
  border-radius:4px !important;
  font-weight:700 !important;
  box-shadow: var(--shadow-sm) !important;
  transition: transform 140ms ease, box-shadow 140ms ease, background 140ms ease;
}
[data-testid="stButton"] button:hover,
[data-testid="stBaseButton-secondary"]:hover{ background:#C9DDEF !important; }

/* 2) 主按钮（「识别并归类」「生成一组练习」）：薄荷 */
[data-testid="stBaseButton-primary"]{
  background: var(--mint) !important;
  border: var(--line) !important;
  border-radius:4px !important;
  font-weight:800 !important;
  box-shadow: var(--shadow-sm) !important;
  transition: transform 140ms ease, box-shadow 140ms ease, background 140ms ease;
}
[data-testid="stBaseButton-primary"]:hover{ background:#CBE2CD !important; }

/* 3) 表单提交（「确认入库」）：卡其 */
[data-testid="stFormSubmitButton"] button{
  background: var(--tan) !important;
  border: var(--line) !important; border-radius:4px !important;
  font-weight:800 !important;
  box-shadow: var(--shadow-sm) !important;
  transition: transform 140ms ease, box-shadow 140ms ease, background 140ms ease;
}
[data-testid="stFormSubmitButton"] button:hover{ background:#EBD5AE !important; }

/* 4) 侧边栏按钮（「清空对话」）：干枯玫瑰粉 */
[data-testid="stSidebar"] [data-testid="stButton"] button{
  background: var(--pink) !important;
  transition: transform 140ms ease, box-shadow 140ms ease, background 140ms ease;
}
[data-testid="stSidebar"] [data-testid="stButton"] button:hover{ background:#F0CBCB !important; }

/* 5) ④ 页的「生成一组练习」再单独一色（淡紫）—— 选择器只在本页命中，
      没命中时会回落到上面的薄荷，不影响可读性 */
[data-testid="stTabs"] [role="tabpanel"]:nth-of-type(4) [data-testid="stBaseButton-primary"]{
  background:#E8E0F0 !important;
}
[data-testid="stTabs"] [role="tabpanel"]:nth-of-type(4) [data-testid="stBaseButton-primary"]:hover{
  background:#DED4EA !important;
}

/* 按下：所有按钮一起位移收影 */
[data-testid="stButton"] button:active,
[data-testid="stBaseButton-primary"]:active,
[data-testid="stFormSubmitButton"] button:active{
  transform: translate(2px,2px) !important;
  box-shadow: 1px 1px 0 var(--ink) !important;
}

/* 文字：正常 / 悬停 / 按下 / 聚焦 / 禁用，一律墨色，不被任何底色带走 */
[data-testid="stButton"] button,
[data-testid="stButton"] button *,
[data-testid="stBaseButton-primary"],
[data-testid="stBaseButton-primary"] *,
[data-testid="stBaseButton-secondary"],
[data-testid="stBaseButton-secondary"] *,
[data-testid="stFormSubmitButton"] button,
[data-testid="stFormSubmitButton"] button *{
  color: var(--ink) !important;
  -webkit-text-fill-color: var(--ink) !important;
  opacity: 1 !important;
}
[data-testid="stButton"] button svg,
[data-testid="stBaseButton-primary"] svg,
[data-testid="stFormSubmitButton"] button svg{ stroke: var(--ink) !important; }

/* 禁用态：底色变淡、边框虚线，文字仍是完整墨色 */
[data-testid="stButton"] button:disabled,
[data-testid="stBaseButton-primary"]:disabled,
[data-testid="stFormSubmitButton"] button:disabled{
  background: #EFE9DA !important;
  border-style: dashed !important;
  box-shadow:none !important;
  cursor:not-allowed;
}

/* ---------- 对话 ---------- */
[data-testid="stChatMessage"]{
  background: var(--card) !important;
  border: var(--line) !important;
  border-radius:4px !important;
  box-shadow: var(--shadow-sm) !important;
  padding:.7rem .8rem !important;
}
[data-testid="stChatInput"]{
  background:#FFFDF6 !important;
  border: var(--line) !important;
  border-radius:999px !important;
  box-shadow: var(--shadow-sm) !important;
}
[data-testid="stChatInput"] textarea{ background:transparent !important; }

/* ---------- 提示条：色块 + 墨线 ---------- */
[data-testid="stAlert"]{
  background: var(--blue) !important;
  border: var(--line) !important;
  border-radius:4px !important;
  color: var(--ink) !important;
  box-shadow: var(--shadow-sm) !important;
}
[data-testid="stAlert"] [data-testid^="stAlertContent"]{
  background: none !important; color: var(--ink) !important;
}
[data-testid="stAlert"] p{ color: var(--ink) !important; }
[data-testid="stAlert"] svg{ color: var(--ink) !important; }
[data-testid="stAlert"]:has([data-testid="stAlertContentWarning"]){ background: var(--tan) !important; }
[data-testid="stAlert"]:has([data-testid="stAlertContentError"]){ background: var(--pink) !important; }
[data-testid="stAlert"]:has([data-testid="stAlertContentSuccess"]){ background: var(--mint) !important; }
[data-testid="stAlert"]:has([data-testid="stAlertContentInfo"]){ background: var(--blue) !important; }

/* ---------- 代码块 / 公式 ---------- */
[data-testid="stCodeBlock"] pre{
  background:#FFFDF6 !important;
  border:1px solid var(--ink) !important;
  border-radius:3px !important;
  color:#2B2721 !important;
}
.katex, .katex *{ color: var(--ink) !important; }
.katex-display{
  overflow-x:auto; overflow-y:hidden; padding:6px 0;
}
.katex-display::-webkit-scrollbar{ height:5px; }
.katex-display::-webkit-scrollbar-thumb{ background:rgba(20,20,20,.35); }

/* ---------- 滚动条 ---------- */
*::-webkit-scrollbar{ width:10px; height:10px; }
*::-webkit-scrollbar-thumb{ background:#CFC5AE; border:1px solid var(--ink); }
*::-webkit-scrollbar-track{ background:transparent; }

/* ---------- 焦点 ---------- */
:focus-visible{
  outline:3px solid var(--ink) !important;
  outline-offset:2px !important;
  border-radius:3px;
}

"""


# ============================================================
# LaTeX → 纯文本（只做展示降级，不动数据）
# ============================================================
_SYMBOLS = [
    (r"\displaystyle", ""),
    (r"\left", ""),
    (r"\right", ""),
    (r"\quad", " "),
    (r"\qquad", " "),
    (r"\,", " "),
    (r"\;", " "),
    (r"\!", ""),
    (r"\cdot", "·"),
    (r"\times", "×"),
    (r"\div", "÷"),
    (r"\pm", "±"),
    (r"\leq", "≤"),
    (r"\le", "≤"),
    (r"\geq", "≥"),
    (r"\ge", "≥"),
    (r"\neq", "≠"),
    (r"\ne", "≠"),
    (r"\approx", "≈"),
    (r"\infty", "∞"),
    (r"\to", "→"),
    (r"\rightarrow", "→"),
    (r"\pi", "π"),
    (r"\alpha", "α"),
    (r"\beta", "β"),
    (r"\theta", "θ"),
    (r"\lambda", "λ"),
    (r"\mu", "μ"),
    (r"\sigma", "σ"),
    (r"\partial", "∂"),
    (r"\int", "∫"),
    (r"\iint", "∬"),
    (r"\iiint", "∭"),
    (r"\oint", "∮"),
    (r"\oiint", "∯"),
    (r"\nabla", "∇"),
    (r"\sum", "Σ"),
    (r"\prod", "∏"),
    (r"\arcsin", "arcsin"),
    (r"\arccos", "arccos"),
    (r"\arctan", "arctan"),
    (r"\sinh", "sinh"),
    (r"\cosh", "cosh"),
    (r"\sin", "sin"),
    (r"\cos", "cos"),
    (r"\tan", "tan"),
    (r"\cot", "cot"),
    (r"\sec", "sec"),
    (r"\csc", "csc"),
    (r"\ln", "ln"),
    (r"\log", "log"),
    (r"\exp", "exp"),
    (r"\max", "max"),
    (r"\min", "min"),
    (r"\lim", "lim"),
]


def _take_group(text: str, start: int) -> tuple[str, int]:
    """从 text[start] == '{' 开始取配平的整组内容，返回 (内容, 右括号之后的位置)。"""
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : index], index + 1
    return text[start + 1 :], len(text)


def _skip_spaces(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t":
        index += 1
    return index


def _replace_two_arg(text: str, command: str, render) -> str:
    r"""把 \frac{a}{b} 这类「命令 + 两组参数」按 render(a, b) 逐层替换（支持嵌套）。"""
    out: list[str] = []
    cursor = 0
    while True:
        hit = text.find(command, cursor)
        if hit == -1:
            out.append(text[cursor:])
            break
        out.append(text[cursor:hit])
        pos = _skip_spaces(text, hit + len(command))
        if pos < len(text) and text[pos] == "{":
            first, pos = _take_group(text, pos)
            pos = _skip_spaces(text, pos)
            if pos < len(text) and text[pos] == "{":
                second, pos = _take_group(text, pos)
                out.append(render(first, second))
                cursor = pos
                continue
        out.append(command)
        cursor = hit + len(command)
    return "".join(out)


def _replace_one_arg(text: str, command: str, render) -> str:
    r"""把 \sqrt{x} 这类单参数命令按 render(x) 替换（支持嵌套）。"""
    out: list[str] = []
    cursor = 0
    while True:
        hit = text.find(command, cursor)
        if hit == -1:
            out.append(text[cursor:])
            break
        out.append(text[cursor:hit])
        pos = _skip_spaces(text, hit + len(command))
        if pos < len(text) and text[pos] == "{":
            inner, pos = _take_group(text, pos)
            out.append(render(inner))
            cursor = pos
            continue
        out.append(command)
        cursor = hit + len(command)
    return "".join(out)


def plain_math(text: str) -> str:
    r"""把 LaTeX 片段降级成纯文本，只用于表格等不渲染公式的位置。

    🔴 只做展示降级，不动数据：数据库里的 question_text 仍然是 LaTeX，
       需要精确排版时继续用 st.latex / st.markdown。

    转换顺序有依赖：`\lim_{x\to 0}` 必须**先整体识别**成 lim(x→0)，
    否则下标会先被换成 lim_(x→0)，极限规则就再也匹配不上了。
    """
    s = str(text or "").replace("$$", "").replace("$", "")

    # ① 极限整体：\lim_{x\to 0} → lim(x→0)
    def _lim(match: "re.Match[str]") -> str:
        inner = match.group(1).replace(r"\to", "→")
        inner = re.sub(r"\s*→\s*", "→", inner).strip()
        return f"lim({inner})"

    s = re.sub(r"\\lim_\{([^{}]*)\}", _lim, s)

    # ② 分式：\frac{a}{b} → (a)/(b)，循环到没有为止（照顾嵌套）
    for _ in range(6):
        before = s
        s = _replace_two_arg(s, r"\dfrac", lambda a, b: f"({a})/({b})")
        s = _replace_two_arg(s, r"\frac", lambda a, b: f"({a})/({b})")
        if s == before:
            break

    # ③ 根号：\sqrt{x} → √(x)
    for _ in range(6):
        before = s
        s = _replace_one_arg(s, r"\sqrt", lambda a: f"√({a})")
        if s == before:
            break

    # ④ \text{...} / \mathrm{...} 只留里面的字
    s = _replace_one_arg(s, r"\text", lambda a: a)
    s = _replace_one_arg(s, r"\mathrm", lambda a: a)

    # ⑤ 常用符号
    for token, replacement in _SYMBOLS:
        s = s.replace(token, replacement)

    # ⑥ 上下标：x^{2} → x^2；x_{n} → x_n
    s = re.sub(r"\^\{([^{}]*)\}", r"^\1", s)
    s = re.sub(r"_\{([^{}]*)\}", r"_\1", s)

    # ⑦ 收尾：去掉剩下的反斜杠与花括号，压缩空白
    s = s.replace("\\", "").replace("{", "").replace("}", "")
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


def inject() -> None:
    """注入纸张风格 CSS。放在 `_sidebar()` 之后、正文之前调用。"""
    st.markdown(f"<style>{_CSS}</style>", unsafe_allow_html=True)
