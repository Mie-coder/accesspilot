# AccessPilot v1.3 T31 验收证据

**Ticket：** T31 — 确定性路由、只读工具与政策 RAG 图分支  
**实现基线 revision：** `9df3a00`  
**日期：** 2026-08-17  
**状态：** `Verified`；独立只读验收 AC1–AC3 全部 PASS，P0/P1/P2 均为 0

## 1. 已验证改变

- 真实实现 `hydrate_authoritative_snapshot → route_intent`，以及 help/security/unknown、numeric 只读子集、read-only tools、政策 RAG 三态与 `finalize_public_outcome` 的多节点链路。
- hydrate 从 Runtime Context 的权威服务重新读取 Principal、Workspace、草稿、Cursor、revision 与 quota；checkpoint State 只保留受限派生值。
- 工具名由确定性路由和服务端映射选择，客户端/模型注入 unknown tool、employee、confirmed 或 raw query 在执行/写入前失败。
- 图在 Legacy `handle` 与 `_process_chat_message` 过程式入口被设置为 fail-if-called 时仍能运行，证明不是把 ConversationService 包成单一图节点。

## 2. Numeric Cursor 范围

T31 的“无业务写入”只覆盖：无/失效 Cursor、等待非 duration 字段、非法期限以及超过目录上限等不会改变状态的 numeric 场景。测试断言 draft、revision、Cursor、quota、模型和业务事实均不变。

合法且未超过上限的 duration 会消费 Cursor 并写 draft/revision，因此明确返回 T32 deferred；其 CAS、重复/并发、成功后的下一 Cursor 与流式时机不得计入 T31 能力。

## 3. RAG 与非 RAG 边界

- 只有 generic policy search 调用 `PolicyService.query` 的 pgvector 路径。
- grounded、insufficient_evidence、retrieval_unavailable 分别映射为 grounded/insufficient/unavailable；unavailable 不泄露原始异常、SQL、Provider 或 evidence。
- policy catalog、自审批政策、目录/权限发现、已有权限、申请状态与 entitlement resolution 不调用 search_policies，也不伪造 policy status/evidence。
- 工具/检索节点将精确脱敏的公共 answer/status/count/codes 写入安全 State；grade/compose 只依赖可持久化的脱敏 GraphState，不依赖进程内 raw scratch。

## 4. Parity 与 fresh-runtime 节点级证据

- 14 个固定只读场景使用 fresh-identical fixtures 连续运行两轮；route `28/28`、normalized outcome `28/28`，均为 `100%` 且两轮零差异。
- parity 逐字段比较 intent、business status、draft revision、draft、assistant message 与条件 error code。
- shadow 是独立的 test-only route/branch comparator，不执行 full graph、RAG、工具、embedding、模型、checkpoint 或任何写入。
- fresh-runtime 节点级重放反证 `6 passed`：select→execute→compose、route→numeric、retrieve→grade→三态 compose，以及 Cursor AuthSession mismatch fail closed；这些用例未启用 checkpointer，不代表跨进程恢复。

## 5. 验收结果

- T31：`35 passed`；
- 路由、Legacy、工具、RAG 与 API 相关回归：`262 passed`；
- T30 strict checkpoint：`57 passed`；
- 完整 API：`594 passed, 3 skipped`；三项 skip 为 T26/T29/T30 各自 destructive-isolated PostgreSQL 专项证明，不是 T31 失败；
- Ruff、MyPy（49 个 source files）与 `git diff --check`：通过；
- 独立只读验收：P0/P1/P2 均为 0。

## 6. 简历与面试价值

候选表述：

> 将 AccessPilot 的确定性路由、只读工具与政策 pgvector RAG 拆入真实 LangGraph 多节点链路，固定 generic search 才走 RAG、目录/自审批/权限解析不走 RAG，并以 14 个场景连续两轮验证 route/outcome 28/28 全部一致；补充 6 项 fresh-runtime 节点级反证，证明下游只依赖可持久化的脱敏 GraphState、不依赖进程内 scratch，完整 API 594 项回归通过。

可展开讲解：如何按风险先迁只读分支；为什么 shadow 只比较 route/branch；RAG 三态如何与直接数据库/目录查询区分；为什么 raw ToolResult 不能只存在进程 scratch 并决定恢复后的输出。

## 7. 限制

- T31 未实现模型解析、quota/operation ledger、草稿 CAS 或合法 duration 写入；这些属于 T32。
- fresh-runtime 用例是未启用 checkpointer 的节点级重放；PostgreSQL checkpoint resume 与进程重启恢复仍由 T33/T37 验证。
- `await_requester_confirmation` 仍不 interrupt；T34 才实现 resume。
- 图仍未接入生产 JSON/SSE；T38/T40 才做入口门禁与 canary。
- 本文件只提供简历候选内容；正式简历仍需用户确认后才能修改。
