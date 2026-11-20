"""学习辅导 AI 助教 · 业务层包。

九个模块按分工各自落地，职责与负责人见本目录 README.md，
函数签名以 ../docs/interfaces.md 为准。

分层约束（硬性）：
- 本包内只有 llm_client.py 允许发起网络请求
- 本包内只有 db.py 允许访问数据库，SQL 一律 ? 参数化
- 所有提示词集中在 prompts.py
"""

__all__ = [
    "db",
    "knowledge",
    "llm_client",
    "prompts",
    "schemas",
    "qa",
    "intake",
    "profile",
    "stubs",
]
