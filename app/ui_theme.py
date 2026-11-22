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

/* ---------- 卡片：纸块 + 硬投影 ---------- */
[data-testid="stVerticalBlockBorderWrapper"]{
  background: var(--card) !important;
  border: var(--line) !important;
  border-radius:4px !important;
  box-shadow: var(--shadow) !important;
}
[data-testid="stVerticalBlockBorderWrapper"] > div{ padding:.4rem .35rem; }

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

/* ---------- 进度条 ---------- */
[data-testid="stProgress"] > div{
  background: #E7DECB !important;
  border:1px solid var(--ink) !important;
  border-radius:2px !important; height:10px !important;
}
[data-testid="stProgress"] > div > div{
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


def inject() -> None:
    """注入纸张风格 CSS。放在 `_sidebar()` 之后、正文之前调用。"""
    st.markdown(f"<style>{_CSS}</style>", unsafe_allow_html=True)
