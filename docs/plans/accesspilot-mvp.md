# AccessPilot MVP 实现计划

## 全局约束

- 所有员工、系统、权限、政策和审批数据均为虚构内容。
- 技术栈：React + TypeScript + Vite；FastAPI；LangChain/LangGraph；PostgreSQL + pgvector；DeepSeek Chat；百炼 `text-embedding-v4`（512 维）。
- 黄金路径是限时申请 `InsightHub / 客户数据导出` 权限。
- 申请人确认、直属经理审批和数据所有者审批必须完成后才能开通权限。
- 审批和权限开通是两个独立状态；开通过程支持幂等和故障注入。
- SSE 只发送消息、工具和状态摘要，不发送模型隐藏思维链。
- 公开演示使用隔离的 Workspace、调用配额和明确的只读回放模式。
- 部署目标是现有腾讯云主机，使用独立 Docker Compose 和 Nginx。
- 架构流程图：[访问申请与权限开通流程](../architecture/access-flow.md)。

## 任务 1：项目基础与领域模型

创建 monorepo 骨架、Python 和 Web 工具链、领域 Pydantic 模型、状态转换、虚构种子目录、`CONTEXT.md` 和两个 ADR。采用测试先行，为字段校验、审批顺序和状态转换添加测试。

## 任务 2：持久化与业务 API

实现面向 PostgreSQL 的 SQLAlchemy 持久化，并提供 SQLite 测试配置；实现 Demo Workspace 隔离、申请/审批/授权/审计仓储、支持幂等的权限开通器模拟器，以及 Workspace、审批、申请详情、角色/故障切换和重置接口。采用测试先行补充 API 和集成测试。

## 任务 3：Agent 运行时、RAG 与流式输出

实现模型和向量服务接口、DeepSeek 与百炼适配器、确定性的离线回退、受控编排 Agent 与风险审查 Agent 的 LangGraph 流程、工具校验、确认守卫、可恢复状态、SSE 事件存储与回放、配额和异常恢复。采用测试先行覆盖工作流、SSE、RAG 和安全不变量。

## 任务 4：React 演示应用

实现三个页面：对话申请台、审批收件箱、申请与审计详情。添加角色、故障模式、重置和回放标识等演示工具栏。展示流式事件、工具卡片、草稿确认、政策引用和权限开通恢复流程，并补充组件和交互测试。

## 任务 5：评测、部署与验收

添加 12 条场景评测及运行器、Dockerfile、Compose、Nginx SSE 配置、迁移与种子启动、健康检查、资源限制、日志轮转、环境变量示例和简明 README。运行完整后端/前端测试、类型检查、Lint、构建、Docker 校验和最终需求审计。
