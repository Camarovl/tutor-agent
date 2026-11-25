# app 目录职责划分

| 文件 | 职责 | 负责人 |
|---|---|---|
| `llm_client.py` | 🔴 全项目唯一发网络请求的文件 | B |
| `prompts.py` | 所有提示词集中在这里 | B |
| `qa.py` | 功能① 知识点问答 | B |
| `intake.py` | 功能② 错题录入与归类（含 `commit()` 落库入口） | B |
| `schemas.py` | 校验大模型返回的 JSON 是否合规 | C₂ |
| `knowledge.py` | 🔴 知识点的查找 / 相似度查重 / 新建挂靠 | C₁ |
| `db.py` | 🔴 全项目唯一碰数据库的文件，SQL 一律 `?` 参数化 | C₁ |
| `profile.py` | 功能③④ 掌握度估计、出题选池、判分回写 | C₂ |
| `stubs.py` | 桩函数：队友实现未就绪时先跑通界面 | D |
| `ui_theme.py` | 视觉层：CSS 注入与纸张风格主题，只做外观 | D |

根目录 `.streamlit/config.toml` 也是视觉层（Streamlit 主题色），归 D。

🔴 `streamlit_app.py`（界面层）不许出现 SQL、不许直接发网络请求。
🔴 `ui_theme.py` 同样不许碰业务逻辑、SQL、网络、session_state —— 它只产出 CSS。
删掉 `ui_theme.py` 与 `.streamlit/config.toml` 后界面应回到原生样式且功能完好。

函数签名以 [../docs/interfaces.md](../docs/interfaces.md) 为准；
改了签名要在同一次改动里更新契约文件，并单独提交一条 commit。
