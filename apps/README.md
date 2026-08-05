# Applications

AccessPilot 是前后端分离的 Monorepo，两个应用分别维护、测试和构建：

- `api/`：Python、FastAPI、SQLAlchemy、LangGraph、PostgreSQL/pgvector 后端；包含数据库迁移和 pytest 测试。
- `web/`：React、TypeScript、Vite、assistant-ui 前端；通过 HTTP API 和 SSE 与后端通信。

前后端共享一个 Git 仓库和交付配置，但不在同一进程中运行，也不把业务逻辑混写到前端。
