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

🔴 `streamlit_app.py`（界面层）不许出现 SQL、不许直接发网络请求。

函数签名以 [../docs/interfaces.md](../docs/interfaces.md) 为准；
改了签名要在同一次改动里更新契约文件，并单独提交一条 commit。
