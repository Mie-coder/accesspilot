# AccessPilot v1.3 LangGraph Spec 双模型评审包

**评审类型：** `spec`
**评审日期：** 2026-08-16
**评审对象：** `docs/specs/accesspilot-langgraph-agent-loop-v1.3.md`
**评审模型：** Claude Code Sonnet 5、DeepSeek（独立只读）
**边界：** 只提出质疑，不修改 Spec、Ticket、代码、Git 状态或发布状态

## 1. 目标

让 LangGraph 真实承接 AccessPilot 的生产自然语言 Agent Loop，而不是只作为依赖、教学图或前端标签，并形成可以被代码、测试和运行证据支持的简历深度。

P0 目标包括：

1. FastAPI JSON 与 SSE 聊天入口真实调用类型化 `CompiledStateGraph`；
2. 意图、申请解析、政策 RAG、只读工具、草稿 CAS、错误闭合和申请人确认由节点与条件边承接；
3. PostgreSQL checkpointer 支持跨 App/进程的确认 `interrupt/resume`；
4. PostgreSQL 业务表继续是身份、草稿、正式申请、审批和开通的唯一权威源；
5. 节点、模型解析、RAG 和工具形成真实、脱敏、幂等的只读轨迹；
6. 保留 Legacy 引擎，通过分入口切流和排空后配置回滚控制风险。

## 2. 非目标

- 不做 ReAct、自主工具选择或 Multi-Agent；
- 不做混合检索、重排或 Query Rewrite；
- 不把经理审批、数据负责人审批或 IAM 开通交给 LangGraph/模型；
- 不引入 LangSmith、OpenTelemetry、Token 成本平台或生产 SLA 声明；
- 不展示 Chain of Thought、系统提示、checkpoint 原文或 Provider/工具原始响应；
- 不修改正式简历，直到新 revision 的验收证据完成。

## 3. 已确认决策

1. LangGraph checkpoint 只保存执行位置、pending interrupt 与安全派生状态；Workspace、Request、Approval、Grant 等业务事实仍由原表授权和写入。
2. 申请字段完整后的申请人确认是唯一 P0 interrupt；正式提交仍由显式业务 API 完成。
3. 恢复时重新解析 AuthSession/Principal、读取权威 Workspace 和校验 `draft_revision`。
4. 写节点使用稳定 `operation_id = workspace_id + turn_id + step_key`；事件使用 `(workspace_id, event_key)` 唯一约束。
5. 外部模型调用在崩溃边界只承诺 at-least-once；本地 quota、草稿、事件和终态要求应用级幂等。
6. 前端只消费白名单 `workspace_events`，不透传 LangGraph raw debug/value/state history。
7. Legacy `ConversationService` 至少保留一个完整版本周期，默认引擎只在 parity、故障恢复和回滚演练通过后切换。

## 4. 关键验收标准

完整 AC-01–AC-14 见评审对象，重点包括：

- 真实 API → CompiledStateGraph 调用路径与实际节点序列；
- Legacy/LangGraph 规范化 outcome parity；
- checkpoint 敏感字段扫描；
- App A interrupt 后退出、App B 正确恢复；
- 五个指定崩溃点的 quota/草稿/轨迹/终态幂等；
- 只有政策问答显示 pgvector RAG；
- 真实工具 started/completed 边界；
- SSE seq、唯一终态、取消、回放和安全 chunk 合同不退化；
- Auth/ACL/Decision Packet/审批/IAM 全回归；
- 依赖锁定、迁移、切流与 Legacy 回滚演练；
- 新 revision 的自动化、固定评测、四角色浏览器主链和 Claim Ledger。

## 5. 当前证据与已知限制

### 当前代码证据

- `apps/api/src/accesspilot/agent/graph.py` 只有 `START → ask_question → END` 单节点教学图；
- `apps/api/tests/agent/test_graph.py` 只用 `InMemorySaver`；
- `apps/api/src/accesspilot/main.py` 的生产 JSON/SSE 入口直接调用 `conversation.py`；
- `conversation.py::_process_chat_message` 当前承担路由、模型解析、工具/RAG、草稿 CAS 与输出组合；
- `events.py` 已有安全事件白名单、禁用字段扫描、唯一 turn terminal；
- `PolicyService + pgvector` 已有真实政策 RAG，目录解析不是 RAG；
- `requests.py / approvals.py / provisioning.py` 已有确定性授权与幂等边界。

### 已验证证据

- v1.2 历史基线：API 425 passed、Web 87 passed、固定评测 101/101；这些结果不能自动证明 v1.3；
- 当前轨迹 UI 原型的定向 13 tests 与 Web build 已通过；
- 当前环境实际为 `langgraph 0.6.11`、`langgraph-checkpoint 3.0.1`；Spec 要求实施前升级并锁定已修复版本；
- v1.3 后端、checkpoint、migration、interrupt 和真实节点事件尚未实现。

### 重点已知风险

- checkpoint 与 Workspace/Cursor 形成双状态或恢复漂移；
- interrupt 等待时用户换题、修改字段或重复确认的多轮语义；
- 业务事务已提交但 checkpoint 未写时的节点重放；
- 外部模型调用无法天然 exactly-once；
- Graph 事件与现有 SSE/背景事件流重复或乱序；
- 旧/新 Workspace、pending interrupt 和节点重命名的兼容；
- 依赖升级、官方 checkpointer schema 与 Alembic/运行账号权限的职责边界；
- 规格规模过大，Ticket 难以独立验收或回滚。

## 6. 聚焦评审问题

> 这份 Spec 是否定义了“最小但足以在简历和面试中成立”的生产 LangGraph 接入：既真实获得状态图、PostgreSQL 持久恢复、一次业务 interrupt/resume 和节点轨迹，又不把 checkpoint 变成第二业务事实源、不制造重复副作用、不破坏现有 SSE/权限边界？

请优先寻找 P0/P1 缺口，特别审查：状态权威、interrupt 多轮语义、PostgresSaver 初始化/版本演进、业务事务与 checkpoint 原子性、操作幂等、SSE/轨迹顺序、渐进切流、回滚可执行性和验收可判定性。

## 7. 输出格式

按严重度排序，每条严格使用：

```text
[P0|P1|P2|P3] 问题
证据或原因：
影响：
最小改进建议：
```

最后补充：

1. `总体结论：可进入 Tickets / 修订后进入 Tickets / 需要重新定义范围`；
2. 最多三条最值得保留的设计；
3. 最多三条应从 P0 延后的内容。

不要修改任何文件，不要实现代码，不要仅因使用了 LangGraph 就建议 ReAct、Multi-Agent 或更复杂 RAG。
