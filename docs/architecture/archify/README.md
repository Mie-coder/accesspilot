# AccessPilot 白盒地图

这组图把 AccessPilot 当前代码、v1.3 Spec、Product Manifest 和 Interview Evidence Pack 中分散的事实整理成三张互补视图。图用于理解和讲解系统，不替代代码、数据库、测试或验收证据。

## 推荐阅读顺序

1. [系统架构图](accesspilot-system-architecture.html)：先看 React、FastAPI、LangGraph、PostgreSQL、模型适配器和模拟 IAM 怎样连接，以及业务权威状态与 checkpoint 的分工。
2. [权限申请主流程](accesspilot-access-lifecycle.html)：再看申请如何从字段收集、明确确认，进入 Decision Packet、两级审批、幂等开通和 AccessGrant。
3. [Agent Loop 运行流](accesspilot-agent-loop.html)：最后看 JSON/SSE 如何进入同一 Flow 2 图引擎，以及 lease/fence、受控分支、interrupt、exact accepted head 和 resume 怎样协作。

每张 HTML 都是自包含的交互式文件，支持亮暗主题、搜索、路径查看、缩放和导出。对应 JSON 是可维护的 Archify 源规格。

## 功能清单

| 功能域 | 当前已实现能力 | 主要入口或权威事实 |
| --- | --- | --- |
| 身份与会话 | 四角色 Mock Login；`AuthSession → Principal → EmployeeRecord`；Cookie、CSRF、Origin 和资源级 ACL | `auth.py`、`auth_sessions`、`employees` |
| Workspace 隔离 | 每个 Workspace 独立草稿、配额、事件、Cursor、Flow 版本和 Agent thread | `workspaces`、`WorkspaceService` |
| 对话与草稿 | 七类确定性意图路由；多轮字段收集；纯数字 Cursor；权限名称解析；draft revision/CAS；明确确认门禁 | `production_graph.py`、`conversation.py`、`WorkspaceRecord.draft` |
| 政策与只读工具 | pgvector 政策检索；grounded / insufficient / unavailable 三态；只读工具白名单；敏感内容拒绝与脱敏 | `PolicyService`、`policy_chunks`、`tools/executor.py` |
| Agent Loop | JSON/SSE sticky 共用 Flow 2；类型化 LangGraph；真实节点和条件边；官方 PostgresSaver；interrupt/resume | `agent/production_graph.py`、`json_orchestrator.py`、checkpoint schema |
| 崩溃与并发恢复 | exact checkpoint head；lease/fence；稳定 input/turn/operation identity；接管重放；旧 executor 写入拒绝 | `agent_turn_executions`、`agent_pending_inputs`、`agent_step_executions` |
| 正式申请与风险证据 | 用户确认后创建 `AccessRequest`；冻结唯一 Decision Packet；风险模型只读建议，不成为授权源 | `access_requests`、`decision_packets`、`requests.py` |
| 两级审批 | 直属经理 → 数据负责人串行审批；身份、角色、顺序、重复和跨资源访问守卫 | `approval_cases`、`approval_steps`、`approvals.py` |
| 权限开通 | 权限管理员显式开通；稳定幂等键；失败重试；timeout/unknown 查询原操作；唯一 AccessGrant | `provisioning_attempts`、`access_grants`、`provisioning.py` |
| 产品工作台 | 对话/轨迹 Tab；权限、政策、草稿、确认、申请时间线和审批/开通工作台；SSE 重连与回放 | `apps/web/src/`、`workspace_events` |
| 安全轨迹 | 最近三轮脱敏节点、工具、RAG、草稿和终态事实；不展示思维链；切换轨迹不触发 API 或副作用 | `AgentTrajectory.tsx`、`agent_step_executions`、安全事件投影 |
| 发布与回滚证据 | Flow 1/Flow 2 切流；活动 pending 预检；Legacy 回滚；固定评测、浏览器重启和完整质量门禁 | `agent/rollback.py`、`scripts/verify-t41.sh`、T41 evidence |

## 一次申请的完整流向

```text
浏览器输入
→ React 产品工作台
→ FastAPI JSON / SSE
→ Workspace sticky Flow 2
→ lease/fence 建立执行并重读权威状态
→ 确定性路由
→ 政策 RAG / 只读工具 / 申请草稿 CAS
→ 申请人明确确认与 exact-head resume
→ AccessRequest + Decision Packet
→ 经理审批 → 数据负责人审批
→ 权限管理员按稳定幂等键开通
→ 唯一 AccessGrant
→ 安全事件和只读轨迹回到产品工作台
```

## 必须保持清楚的四条边界

- PostgreSQL 业务表决定身份、正式申请、审批和 Grant；LangGraph checkpoint 只保存执行位置和可恢复派生状态。
- 模型可以做结构化解析、政策检索辅助和只读建议，但不能审批、开通或授权。
- 申请人确认使用 LangGraph interrupt/resume；正式审批和开通继续由确定性领域服务、ACL、事务和幂等键负责。
- 所有人员、系统、政策、审批和 IAM 数据都是虚构数据；Mock Login 不是生产认证，模拟 IAM 也不连接真实企业系统。

## 事实来源与验证状态

- 产品事实：[Product Manifest](../../evidence/accesspilot-v1.3-product-manifest.md)
- Agent Loop 细节：[v1.3 Spec](../../specs/accesspilot-langgraph-agent-loop-v1.3.md)
- 面试讲解与证据链：[Interview Evidence Pack](../../evidence/accesspilot-v1.3-interview-evidence-pack.md)
- 原始业务流程：[access-flow.md](../access-flow.md)

三张图均通过 Archify `showcase` 的 9/9 自动结构与构图检查，错误 0、警告 0；并通过 1440×900、1600×1000、1920×1080、2048×1320 的亮色布局测量，以及 1440×900 / 2048×1320 的亮暗截图检查。最终截图已经人工检查，未发现节点截断、标签遮挡、连线穿越节点或明显失衡。

> 这些检查证明图的产物、布局和当前作者整理出的关系一致，不会自动证明运行中的每次请求都经过某条路径。运行事实仍应回到代码、数据库、测试和验收证据核对。
