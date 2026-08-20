# AccessPilot v1.3 Claim Ledger

**T42 状态：** `Verified`；独立验收修复轮 P0/P1/P2=0/0/0
**代码与证据 revision：** `9e757fd433f10fbff22fba9654c54cc3b4e9bec2`
**产品结论：** `product_verified=true`
**面试结论：** `interview_ready=pending_user_verification`

本 Ledger 只将 [T41 本地提交](accesspilot-v1.3-t41-acceptance-2026-08-20.md)已证明的产品事实转换为有限 Claim。每条反向链都固定在同一 revision，并分开真实实现入口、可执行测试、T41 运行证据和限制。框架名、测试文件名或全绿数字本身都不是独立入口证据。

Ownership 门禁：AI 参与了实现草稿、测试脚手架和文档整理；`product_verified=true` 只证明产品事实。候选人未独立复现、讲解并回答边界问题前，不得把 `interview_ready` 改为通过，也不得把候选文本自动写入正式简历。

## 反向索引

| Claim ID | 能力 | 真实产品入口 | T41 当前 revision 证明 | 必讲限制 |
|---|---|---|---|---|
| CL-LG-01 | LangGraph 真实主链 | JSON + SSE → orchestrator → compiled graph | 全 API/Web、parity、真实浏览器 | 受控状态图，不是自主循环 |
| CL-LG-02 | official PostgreSQL checkpoint | 显式 setup、App lifecycle、fenced exact head | 跨 App 重启、checkpoint 计数、rollback | 本地 disposable PostgreSQL，不是生产 HA |
| CL-LG-03 | interrupt/resume | 申请人确认 interrupt → rehydrate → CAS | 浏览器停 API 后原 DB/head 恢复 | 只是申请人确认，不代理正式审批 |
| CL-LG-04 | grounded pgvector RAG | generic policy → retrieval/grade/compose | POL 引用与真实 retrieval 轨迹 | 无 Recall@k 或外部 Provider 质量结论 |
| CL-LG-05 | 只读真实轨迹 | fenced event producer → API/SSE → UI projection | 7/7 terminal、4/4 step、三视口 | 不是思维链或可操作控制台 |
| CL-LG-06 | 六个故障恢复 | test-only fault point → takeover/fence/idempotency | T41 API gate 重跑六点套件 | Provider 仍是 at-least-once |

<a id="cl-lg-01"></a>

## CL-LG-01 — LangGraph 真实主链

**有限 Claim：** 在 AccessPilot 本地作品集原型中，Flow 2 的 JSON 与 SSE 产品入口共用同一 `LangGraphConversationOrchestrator`，实际调用由多个业务节点和条件边组成的 `CompiledStateGraph`，不是将旧 `ConversationService` 包成单节点。

**证据 revision：** `9e757fd433f10fbff22fba9654c54cc3b4e9bec2`

**实现入口：**

- [main.py](../../apps/api/src/accesspilot/main.py) — `create_chat_message` 是 JSON 公开路由，`create_chat_message_stream` 是 SSE 公开路由。
- [json_orchestrator.py](../../apps/api/src/accesspilot/agent/json_orchestrator.py) — `LangGraphConversationOrchestrator` 持有 execution、saver、生产图与 JSON/SSE 共用运行合同。
- [production_graph.py](../../apps/api/src/accesspilot/agent/production_graph.py) — `build_production_graph` 编译真实节点拓扑，`_compile_production_graph` 显式建立固定边和条件边。

**测试：**

- [test_t38_json_langgraph.py](../../apps/api/tests/api/test_t38_json_langgraph.py) — `test_injected_json_orchestrator_enters_real_compiled_production_path` 断言 JSON 进入完整节点路径。
- [test_t40_sse_langgraph.py](../../apps/api/tests/api/test_t40_sse_langgraph.py) — `test_injected_sse_enters_real_graph_with_one_http_turn_identity` 断言 SSE 的 HTTP/graph turn 身份和真实图路径。
- [test_t30_graph_state.py](../../apps/api/tests/agent/test_t30_graph_state.py) — `test_production_graph_has_exact_nodes_condition_maps_and_representative_paths` 锁定节点、条件边和代表路径。

**T41 运行证据：** [T41 证据包](accesspilot-v1.3-t41-acceptance-2026-08-20.md) §3 的新 revision 全量门禁与 33 个语义场景两轮零差异，以及 §4 的真实 Vite/FastAPI/PostgreSQL Flow 2 浏览器主链，共同证明入口和业务结果。

**限制：** 这是一个服务端约束的状态图，路由、工具和授权边界由代码固定；它不证明 ReAct 自主循环、Multi-Agent 编排、真实企业上线或生产规模。

<a id="cl-lg-02"></a>

## CL-LG-02 — official PostgreSQL checkpoint

**有限 Claim：** 使用官方 `langgraph-checkpoint-postgres` PostgresSaver 保存图状态，并以独立 checkpoint schema/角色、App 级连接池生命周期、exact locator、candidate/accepted head 和 fenced CAS 约束可恢复头。

**证据 revision：** `9e757fd433f10fbff22fba9654c54cc3b4e9bec2`

**实现入口：**

- [checkpoint_init.py](../../apps/api/src/accesspilot/checkpoint_init.py) — `run_official_checkpoint_setup` 是唯一显式官方 setup 入口，`initialize_checkpointing` 串联 Alembic、setup 和 readiness。
- [checkpoint.py](../../apps/api/src/accesspilot/agent/checkpoint.py) — `PostgresCheckpointRuntime` 管理每 App 连接池，`FencedPostgresSaverAdapter` 限定 execution context，`AcceptedCheckpointHeadStore` 只提升已验证 candidate。
- [main.py](../../apps/api/src/accesspilot/main.py) — `create_app` 的 lifespan 只在启用图模式时构建并关闭 checkpoint runtime。

**测试：**

- [test_t29_postgres.py](../../apps/api/tests/db/test_t29_postgres.py) — `test_t29_real_postgres_roles_lifecycle_exact_head_and_no_drift` 用真实 PostgreSQL 验证双角色、生命周期、exact head 和 no-drift。
- [test_t30_postgres.py](../../apps/api/tests/db/test_t30_postgres.py) — `test_t30_real_postgres_strict_checkpoint_scan_and_malicious_zero_write` 扫描官方 saver 的 raw/decoded checkpoint 安全边界。
- [test_t40_sse_langgraph.py](../../apps/api/tests/api/test_t40_sse_langgraph.py) — `test_real_postgres_product_cross_entry_resume_uses_lifespan_saver` 验证产品 lifespan saver 和跨 JSON/SSE 恢复。

**T41 运行证据：** [T41 证据包](accesspilot-v1.3-t41-acceptance-2026-08-20.md) §4 记录同一 DB/checkpoint 停启 API 后的恢复，并只报行数 53 checkpoints / 223 blobs / 1158 writes；§5 在 disposable PostgreSQL 中保留 accepted head 完成 Legacy 回滚对账。

**限制：** 验收只在本地 disposable PostgreSQL 上运行，没有生产集群、备份恢复或 HA 演练；checkpoint 也不是身份、授权、审批或 Grant 的业务权威源。

<a id="cl-lg-03"></a>

## CL-LG-03 — interrupt/resume

**有限 Claim：** 完整申请草稿在申请人确认处调用 `interrupt()`；恢复时从应用侧 pending 记录种入精确 checkpoint head，以一次 `Command(resume)` 进入 rehydrate，再用 revision CAS 确认或将非确认输入在同一 resume 内重新路由。

**证据 revision：** `9e757fd433f10fbff22fba9654c54cc3b4e9bec2`

**实现入口：**

- [production_graph.py](../../apps/api/src/accesspilot/agent/production_graph.py) — `_await_requester_confirmation` 产生 interrupt，`_rehydrate_resume_snapshot` 重读权威事实，`_apply_confirmation_cas` 最多确认一次。
- [turn_execution.py](../../apps/api/src/accesspilot/agent/turn_execution.py) — `finalize_interrupt` 原子投影 pending/Cursor/head/terminal，`begin_resume` 种入 exact head，`finalize_resume_outcome` 原子关闭恢复轮。
- [json_orchestrator.py](../../apps/api/src/accesspilot/agent/json_orchestrator.py) — `_run_resume` 处理确认、换题、编辑后再 interrupt 和 crash takeover。

**测试：**

- [test_t34_graph_interrupt.py](../../apps/api/tests/agent/test_t34_graph_interrupt.py) — `test_resume_confirm_applies_confirmation_cas_exactly_once_in_one_call` 断言一次 Command resume 与 CAS 一次。
- [test_t34_postgres.py](../../apps/api/tests/db/test_t34_postgres.py) — `test_begin_resume_validates_exact_head_and_seeds_before_first_graph_call` 验证图调用前已持久化 exact head。
- [test_t38_json_langgraph.py](../../apps/api/tests/api/test_t38_json_langgraph.py) — `test_json_app_a_interrupts_and_app_b_confirms_through_rehydrate` 从真实 JSON API 跨 App 恢复。

**T41 运行证据：** [T41 证据包](accesspilot-v1.3-t41-acceptance-2026-08-20.md) §4 记录 Flow 2 进入 pending、停止 API、用原 DB/checkpoint 重启，之后通过键盘确认恢复为 ready-to-submit；该 restart 路径为 `1/1 = 100%`。

**限制：** 这里的 interrupt 仅是申请人对草稿的明确确认，不将经理、数据负责人审批或 IAM 开通交给图/模型；本地 1/1 不是生产故障切换 SLA。

<a id="cl-lg-04"></a>

## CL-LG-04 — grounded pgvector RAG

**有限 Claim：** 只有 generic policy 问答进入 `retrieve_policy_pgvector → grade_policy_evidence → compose_*`，并保留 grounded / insufficient / unavailable 三态与政策引用；目录、自审政策和权限解析都有不走 RAG 的反证。

**证据 revision：** `9e757fd433f10fbff22fba9654c54cc3b4e9bec2`

**实现入口：**

- [production_graph.py](../../apps/api/src/accesspilot/agent/production_graph.py) — `_retrieve_policy_pgvector` 调用检索并发出 retrieval 轨迹，`_grade_policy_evidence` 分三态，`_compose_grounded_answer` 只用脱敏证据。
- [policies.py](../../apps/api/src/accesspilot/tools/policies.py) — `PolicyService` 的 `query` 将底层搜索结果映射为带引用的类型化回答。
- [rag/policies.py](../../apps/api/src/accesspilot/rag/policies.py) — `search_policies` 执行 pgvector 相似度查询并返回稳定政策参考。

**测试：**

- [test_t31_readonly_graph.py](../../apps/api/tests/agent/test_t31_readonly_graph.py) — `test_generic_policy_search_alone_calls_pgvector_and_preserves_typed_state` 证明只有 generic search 进入 pgvector。
- [test_t36_trace_graph.py](../../apps/api/tests/agent/test_t36_trace_graph.py) — `test_policy_path_emits_retrieval_events_and_never_tool_events` 证明 RAG 事件来自真实 retrieval 节点。
- [test_policy_retrieval.py](../../apps/api/tests/rag/test_policy_retrieval.py) — `test_pgvector_returns_four_stable_policy_references` 锁定数据库检索引用。

**T41 运行证据：** [T41 证据包](accesspilot-v1.3-t41-acceptance-2026-08-20.md) §4 记录真实浏览器政策问答路由 `policy`，节点 `retrieve_policy_pgvector`，检索 POL-004/003/005/008，回答引用 POL-004/003。

**限制：** 浏览器运行使用 deterministic embedding 的无密钥本地路径，未测 `policy_recall_at_k`、真实 Provider 质量或 token latency；没有混合检索、重排或 query rewrite。

<a id="cl-lg-05"></a>

## CL-LG-05 — 只读真实轨迹

**有限 Claim：** node / route / model / retrieval / tool / draft / interrupt / terminal 事件从真实执行边界产生，通过 execution fence 、稳定身份和白名单公共投影进入前端；最近三轮只展示已持久化事件，切换 Tab 不发起请求或副作用。

**证据 revision：** `9e757fd433f10fbff22fba9654c54cc3b4e9bec2`

**实现入口：**

- [trace.py](../../apps/api/src/accesspilot/agent/trace.py) — `GraphTraceRecorder` 在每次 emit 前重验 execution/fence，并以稳定 event/step identity 去重。
- [events.py](../../apps/api/src/accesspilot/events.py) — `list_recent_turns` 产生脱敏的公共最近轮投影，`validate_event_payload` 限定事件 Schema 和禁用内容。
- [AgentTrajectory.tsx](../../apps/web/src/AgentTrajectory.tsx) — `AgentTrajectory` 按真实 `turn.started.orchestrator` 标记引擎，展示最近三轮的只读轨迹。
- [App.tsx](../../apps/web/src/App.tsx) — `App` 使对话与轨迹 panel 同时保持挂载，Tab 只改变可见性和键盘焦点，不触发请求、resume、确认或工具执行。

**测试：**

- [test_t36_trace_graph.py](../../apps/api/tests/agent/test_t36_trace_graph.py) — `test_read_only_path_writes_real_node_and_tool_events_in_order` 断言真实节点/工具事件顺序。
- [test_t36_postgres.py](../../apps/api/tests/db/test_t36_postgres.py) — `test_checkpoint_replay_emits_single_event_set_and_fenced_finalize_keys` 用真实 PostgreSQL 验证重放去重和 fenced terminal。
- [AgentTrajectory.test.tsx](../../apps/web/src/AgentTrajectory.test.tsx) — `it` 用题名“selects the latest three turns by each group maximum DB id and sorts events by id”与“ignores unknown event contents and projects safe details from a per-type whitelist”锁定选轮、安全投影和只读边界。
- [App.test.tsx](../../apps/web/src/App.test.tsx) — Vitest 用例 `switches tabs with roving keyboard focus and keeps the live conversation mounted without side effects` 直接断言 roving 键盘焦点、对话挂载保活与零 append/副作用。

**T41 运行证据：** [T41 证据包](accesspilot-v1.3-t41-acceptance-2026-08-20.md) §4 记录 7 个非 running turn 的 terminal 完整率 `7/7`、4 个 side-effect step `4/4` completed、三视口和键盘 Tab 路径；[轨迹截图](assets/accesspilot-v1.3-t41/flow2-trajectory-1440.png)展示真实 Flow 2 badge 和节点。

**限制：** 轨迹是脱敏执行事实的只读投影，不是模型思维链、checkpoint/debug/value stream 或可执行操作台；本版本也没有 LangSmith/OpenTelemetry 可观测平台。

<a id="cl-lg-06"></a>

## CL-LG-06 — 六个故障恢复

**有限 Claim：** 对 Provider 返回后、只读工具返回后、草稿 CAS 提交后、interrupt saver 保存后、非确认 resume 消费后、确认 CAS 提交后六个精确窗口执行 crash → takeover/reconcile，验证同一逻辑输入和本地 quota/draft/step/head/terminal 最多一次。

**证据 revision：** `9e757fd433f10fbff22fba9654c54cc3b4e9bec2`

**实现入口：**

- [fault_injection.py](../../apps/api/src/accesspilot/agent/fault_injection.py) — `FaultPoint` 列出六点，`hit_fault` 仅在进程内测试 scope 单次触发，默认关闭。
- [turn_execution.py](../../apps/api/src/accesspilot/agent/turn_execution.py) — `takeover` 复用原 run/input 身份并只递增 attempt/fence，`finalize_graph_turn_with_event` 原子提升 head 和写终态。
- [step_operations.py](../../apps/api/src/accesspilot/agent/step_operations.py) — `AgentStepOperationService` 以稳定 operation 和 execution fence 去重 quota、草稿、Cursor 与确认写入。

**测试：**

- [test_t37_fault_injection.py](../../apps/api/tests/agent/test_t37_fault_injection.py) — `test_fault_points_are_default_off_and_scoped_single_shot` 对六个 enum 参数化验证默认关闭和单次触发。
- [test_t37_recovery.py](../../apps/api/tests/agent/test_t37_recovery.py) — `test_new_input_crash_restarts_from_same_safe_input_without_duplicate_local_writes` 覆盖 Provider/草稿/interrupt 三点。
- [test_t37_recovery.py](../../apps/api/tests/agent/test_t37_recovery.py) — `test_tool_completion_crash_allows_at_least_once_tool_but_one_local_trace_and_terminal` 覆盖工具返回窗口。
- [test_t37_recovery.py](../../apps/api/tests/agent/test_t37_recovery.py) — `test_resume_crash_reuses_original_resume_turn_and_finishes_once` 覆盖非确认消费和确认 CAS 后两点。

**T41 运行证据：** [T41 证据包](accesspilot-v1.3-t41-acceptance-2026-08-20.md) §3 的 `868 passed, 0 skipped` 全 API 门禁在该 revision 重跑 T37 六点套件；六点设计与实现细节见 [T37 验收证据](accesspilot-v1.3-t37-acceptance-2026-08-20.md)。T41 另将浏览器 restart 1/1 和 rollback 1/1 合并为 `2/2 = 100%`。

**限制：** 六点 fault hook 无 API/UI/settings 入口，六点套件的 saver 为跨图实例共享 InMemorySaver，并由 T34/T36 真实 PostgreSQL 测试补证。Provider 与只读工具执行均为 at-least-once；`AFTER_TOOL_COMPLETION_BEFORE_EVENT` 恢复可重复工具调用，Provider 成功返回但 completion 未落盘时也可重调/重复计费。只有本地 quota/草稿/step/head/轨迹/terminal 最多一次；本地幂等不能外推到上述两类外部调用。

## 使用门禁

- 正式简历不在本 Ticket 修改列表中；仓库工作树审计也不允许 `.doc` / `.docx` / `.pdf` 或 resume/CV/简历路径进入 T42 变更。
- 使用 Claim 前，候选人需能从公开 API 入口追到生产节点、业务事实库与 checkpoint 分工、对应测试和 T41 限制。
- 候选人完成一次无稿 Demo、讲清 Provider at-least-once 和 Mock 边界后，才可由用户另行决定是否改正式简历。
