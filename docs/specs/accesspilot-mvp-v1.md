# AccessPilot MVP v1 Spec

## 目标

交付一个可本地运行、可容器化部署的虚构企业权限申请 Agent。演示用户通过自然语言完成限时权限申请，经风险审查、两级人工审批和幂等权限开通后，在 React 页面查看全过程及审计证据。

## 已有基线

- Day 1–6 的领域模型、Workspace 隔离、PostgreSQL 持久化、确定性目录工具和 LangGraph 收集骨架已存在。
- Day 7 的 `ParsedReply`、一次纠正重试与 DeepSeek HTTP 适配器处于未提交状态。
- 2026-08-05 后端完整基线：`88 passed, 1 warning`（连接本机 PostgreSQL 测试库）。
- React 页面目前只有静态 `AccessPilot` 文本，依赖尚未安装；本机 Node 18/npm 6 不满足现代 Vite 工作区的可靠基线。

## 功能范围

### 申请与 Agent

- 在独立 Demo Workspace 中保存并恢复会话草稿。
- DeepSeek 将单轮自然语言解析为严格 `ParsedReply`；无密钥时使用明确标识的确定性离线回退。
- 未确认的完整草稿只能停留在等待确认，不得创建正式申请。
- 确认后冻结五字段申请事实：申请人、权限、期限、理由和确认时间。

### 政策与风险审查

- 将原创虚构政策分块保存为 512 维 pgvector。
- 百炼适配器支持 `text-embedding-v4`；无密钥时使用确定性 512 维离线向量供测试和演示。
- 风险审查 Agent 只读申请、目录和 Top 4 政策引用，输出严格 `RiskReview`，失败时进入可恢复错误，不编造政策。

### 审批与开通

- 创建直属经理 → 数据所有者的串行审批步骤。
- 错误角色、错误顺序、重复审批和驳回后继续审批均被拒绝并记录审计。
- 两级审批完成后才能调用权限开通器。
- 开通器使用稳定幂等键，支持成功、失败和“超时但结果未知”；安全重试最多产生一条 `AccessGrant`。

### API、事件与保护

- 提供对话、提交、审批、开通恢复、收件箱、申请详情、故障模式、重置和回放 API。
- 消息、工具摘要、草稿、确认、状态和错误事件持久化。
- SSE 支持 `Last-Event-ID`，只补发遗漏事件，不发送模型隐藏思维链。
- 每 Workspace 有模型调用配额；超限后进入明确只读回放模式。

### React 演示应用

- 使用 assistant-ui `LocalRuntime + ChatModelAdapter` 实现对话申请台。
- 实现审批收件箱和申请/审计详情页。
- 展示草稿卡、政策引用、审批步骤、开通状态、工具结果、错误恢复和只读回放标识。
- 业务事实和线程历史来自 FastAPI/PostgreSQL，不使用 assistant-ui Cloud 作为事实源。

### 评测与交付

- 12 条固定场景覆盖黄金路径、缺项、未确认、政策失败、审批乱序、驳回、超时、重复重试、SSE 重连和配额。
- 提供 Dockerfile、Compose、迁移/种子启动、健康检查、Nginx SSE 示例、资源限制和日志轮转。
- README 提供本地、离线、真实模型和部署运行方式。

## 非目标

- 真实企业数据、SSO、通讯录、IAM 或生产租户隔离。
- assistant-ui Cloud 或 LangGraph Cloud 保存业务事实。
- 自动推送、合并或部署；这些操作必须由用户最终确认。
- 对模型展示或存储隐藏思维链。

## 安全与事实边界

- `.env`、API Key 和服务器细节不得提交或发送到前端。
- 所有人员、系统、权限、政策和审批记录均为原创虚构内容。
- `can_enter_approval` 只表示具备送审资格，不表示审批或开通成功。
- 审批通过与 `AccessGrant` 创建必须保持独立状态和审计证据。

## 测试边界

- 领域状态、确认、审批顺序、幂等与配额使用单元测试。
- PostgreSQL/pgvector、Workspace 隔离、事件回放和 API 使用集成测试。
- DeepSeek/百炼默认使用协议级假客户端；真实密钥只用于单独的手动 smoke test。
- React 使用 Vitest/Testing Library；黄金路径使用真实浏览器检查。
- 最终运行 pytest、Ruff、MyPy、Vitest、ESLint、TypeScript build、Docker Compose config 和 12 条评测。

## 外部依赖与阻塞条件

- 真实 DeepSeek smoke test 需要本地 `DEEPSEEK_API_KEY`。
- 真实百炼 smoke test 需要本地 `DASHSCOPE_API_KEY`。
- 前端需要可用的 Node 22+ 与现代 npm；优先使用工作区自带运行时，不修改用户全局环境。
- 腾讯云部署只在本地验收通过并获得用户明确授权后执行。
