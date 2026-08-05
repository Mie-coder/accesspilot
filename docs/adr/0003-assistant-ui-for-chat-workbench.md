# ADR 0003：使用 assistant-ui 构建对话申请台

- 状态：已接受
- 日期：2026-07-30

## 背景

AccessPilot 的 React 前端需要展示多轮对话、流式回复、工具调用、草稿确认和错误恢复。后端已经采用自有 FastAPI、LangGraph、Workspace 隔离和 PostgreSQL 持久化，审批与权限开通事实不能迁移到第三方前端云服务。

## 决策

对话申请台使用 assistant-ui 的 React primitives。

第一阶段采用 `LocalRuntime + ChatModelAdapter` 对接 AccessPilot 自有 FastAPI/SSE。assistant-ui 负责对话界面、输入、消息渲染和流式交互；FastAPI、Workspace 和 PostgreSQL 继续负责草稿、确认、审批、授权、审计事件和线程历史。

不使用 assistant-ui Cloud 保存审批或授权事实，也不为了前端组件切换到 LangGraph Cloud。

审批收件箱和申请/审计详情页继续使用普通 React 业务组件，因为它们不是聊天线程。

## 结果

- 可以复用成熟的对话交互和可访问性能力，减少重复编写聊天 UI。
- 前端运行时必须通过稳定适配层调用后端，不能直接访问模型密钥。
- SSE 断线恢复仍遵循 AccessPilot 的事件编号和 Workspace 隔离规则。
- 如果后续需要展示完整 Agent State，可在不改变业务 API 的前提下评估 `AssistantTransport`，但不作为 MVP 前置条件。
