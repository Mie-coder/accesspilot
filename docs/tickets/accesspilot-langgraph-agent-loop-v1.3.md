# AccessPilot v1.3「真实 LangGraph Agent Loop 与只读运行轨迹」Tickets

**状态：** 用户已确认串行实施；T26–T32 `Verified`，T33 `In progress`（已实现，待独立复核），T34–T42 尚未开始；未授权推送、合并、部署或修改正式简历
**更新时间：** 2026-08-17
**继承基线：** AccessPilot v1.2，`product_verified=true`；T32 实现基线 HEAD `7ad8a01`，历史证据不自动证明 v1.3
**Canonical Spec：** `docs/specs/accesspilot-langgraph-agent-loop-v1.3.md`
**双评审裁决：** `docs/reviews/accesspilot-v1.3-spec-review-decisions-2026-08-16.md`

## 0. 执行边界

- 用户确认后才按 `T26 → T27 → … → T42` 严格串行实施，一次只做一张；
- 每张实现 Ticket 先写失败测试，再做最小实现；定向验证、相关回归和独立只读验收通过后，才允许本地提交；
- T26 任一兼容条件失败必须回流 Spec，不在 Ticket 内静默换版；任何后续 Ticket 发现需要改变权威源、副作用语义或验收口径，也先停止并回流 Spec；
- T40 前默认生产/本地产品入口都保持 Legacy；T38/T40 先分别完成 JSON/SSE 隔离门禁，两者都通过后才允许真实 flow 2 Workspace 在两个入口整体进入 LangGraph；
- 当前未提交的 `AgentTrajectory` 只是 Legacy 事件原型，不算 T39 完成证据；T39 只能在真实后端事件合同完成后受控复用或重写；
- 本列表不授权推送、合并、部署、删除 Legacy/checkpoint，或修改正式简历；
- `product_verified` 只由 T41 在同一新 revision 上判定；T42 只交付证据和候选表述，不代替用户的面试掌握度。

## 1. 依赖与用户可见结果

| Ticket | 交付结果 |
|---|---|
| T26 | 证明候选 LangGraph/PostgresSaver 矩阵真的可用，再锁版 |
| T27 | 建立 Legacy/LangGraph 可替换边界，对外行为不变 |
| T28 | 建立 thread、execution、pending、幂等和事件身份数据事实 |
| T29 | 建立官方 PostgreSQL checkpointer 初始化与安全运行周期 |
| T30 | 生成类型化生产图骨架，checkpoint 不泄密 |
| T31 | 真实路由、只读工具与政策 RAG 进入图 |
| T32 | 申请字段解析、权限解析、草稿 CAS 和校验进入图 |
| T33 | 图执行具有租约、fence、精确 checkpoint head 和崩溃接管 |
| T34 | 申请人确认可持久 interrupt/resume，且 pending/Cursor/terminal 原子可见 |
| T35 | Workspace 粘性引擎、对账门禁和 Legacy 降级路径可执行 |
| T36 | 后端从真实 node/model/RAG/tool 边界产生脱敏轨迹 |
| T37 | 六个崩溃边界恢复后不丢输入、不重复本地副作用 |
| T38 | JSON 入口在隔离门禁中真正调用生产图 |
| T39 | 页面顶部可切换「对话 / 轨迹」，只读展示最近三轮 |
| T40 | SSE 合同保持，真实 flow 2 canary 的 JSON/SSE 同时走 LangGraph |
| T41 | 完整质量、浏览器、恢复和回滚验收通过 |
| T42 | 形成可追溯的 Claim Ledger、面试证据和限制披露 |

## T26 — LangGraph 依赖兼容性 Spike 与版本锁定

**状态：** `Verified`；三条验收标准均通过，Claude Code + DeepSeek 独立验收均无 P0/P1，已按 T26 文件清单本地提交

**目标：** 在不触碰生产入口和业务数据的隔离环境中，证明候选依赖矩阵满足 Spec 所需 API 后再锁定。

**Spec 映射：** §6.1、§7、§10.1；AC-11。

**验收：**

1. 全新 Python 环境、本地环境和 Docker 解析出候选矩阵的同一组精确版本，并生成可复现锁定/约束产物。
2. strict msgpack、同步 PostgresSaver、SQLAlchemy 到 psycopg URL 安全转换、自定义 schema/search path、`autocommit=True`、`dict_row`、连接池、连续两次 `setup()`、根图非空 namespace fail-fast、每 graph run 独立 `checkpoint_thread_id`、精确 `checkpoint_thread_id + checkpoint_ns + checkpoint_id` 恢复、缺失/错 run head fail-closed、interrupt 跨进程 resume 全部通过。实测还必须捕获 `put(head) → put_writes(interrupt)` 顺序，并证明只有图停止后经精确状态校验的 candidate 才能 fenced 提升为 accepted head。
3. 完整 v1.2 基线全绿；任一条失败则 T26 不通过、不得静默换版，必须回流 Spec。

**测试/运行验证：** 隔离安装与版本清单、真实 PostgreSQL setup/head/resume smoke、Docker 解析、v1.2 Pytest/Ruff/MyPy/Alembic/Vitest/ESLint/TypeScript/build 基线。

**依赖：** T25。

**主要风险：** LangGraph 1.x、checkpoint-postgres 和 psycopg 的同步 API、连接工厂、序列化或精确 head 恢复合同不兼容。

**可回滚停点：** 未通过不得进入 T27；删除隔离环境或回退锁定提交即可，业务数据和入口保持 v1.2。

## T27 — ConversationOrchestrator 兼容层与 Legacy 黄金基线

**状态：** `Verified`；三条验收标准通过，独立只读验收无 P0/P1，已本地提交 `db2ebf2`

**目标：** 先建立可替换编排边界，JSON、SSE 和业务结果仍完全走 Legacy。

**Spec 映射：** §9、§10.1；AC-02、AC-09。

**验收：**

1. JSON/SSE 入口只依赖注入的 `ConversationOrchestrator`；`LegacyConversationOrchestrator` 薄包装现有 `ConversationService`，不改其业务责任。
2. 全部规定意图的规范化 JSON outcome、SSE 顺序、Cursor 转移、异常和唯一终态与冻结黄金样本逐字段一致。
3. 默认只启用 Legacy，不创建 checkpoint、execution/pending 事实或新图事件。

**测试/运行验证：** 先冻结 JSON/SSE 黄金样本，再运行会话、Cursor、政策、工具、错误闭合、事件回放和 v1.2 全回归；实际各发起一次 JSON/SSE 请求核对。

**依赖：** T26。

**主要风险：** 适配层改变异常映射、终态顺序或 Cursor 激活时机。

**可回滚停点：** 回退适配层本地提交即恢复原入口；无数据迁移。

## T28 — 应用 Schema、运行事实与确定性身份

**状态：** `Verified`；三条验收标准通过，独立只读验收无 P0/P1/P2，已本地提交 `0553cad`

**目标：** 建立 sticky flow、逻辑输入、lease/fence、pending、步骤幂等和轨迹去重所需的应用数据事实。

**Spec 映射：** §4、§6.2、§7、§8.2；AC-05、AC-11、AC-12。

**验收：**

1. 存量 Workspace 原子回填唯一、非空、不可变 `agent_thread_id`、`flow_version=1` 和单调 fence 初值；新 Workspace 在创建事务内由服务端分配，客户端不能读写或覆盖。
2. `AgentTurnExecutionRecord`、`AgentPendingInputRecord` 和步骤执行事实满足 Spec 的唯一、partial unique、status/check 约束；完整 checkpoint 坐标包含每 run 独立 `checkpoint_thread_id`，v1.3 根图 `checkpoint_ns` 只允许空字符串；`event_key` 可空且只对非空值唯一，历史事件不回填。
3. canonical JSON + SHA-256 工具对 operation/step/event/tool 黄金向量产生稳定值，明确验证 attempt→ordinal 映射；Alembic fresh 和含数据库的 upgrade→downgrade→upgrade/no-drift 通过且不触碰 checkpoint schema。

**测试/运行验证：** 模型、Store、客户端注入、并发唯一、status check、存量回填、canonical 黄金向量与迁移链。

**依赖：** T27。

**主要风险：** 回填锁表、partial index 语义或过度宽松的 status 转移会破坏恢复不变量。

**可回滚停点：** 入口仍为 Legacy；只回退应用迁移，不删除/降级 checkpoint schema。

## T29 — PostgreSQL Checkpointer 初始化、账号与运行生命周期

**状态：** `Verified`；三条验收标准通过，独立只读验收无 P0/P1/P2，已本地提交 `76676a7`

**目标：** 将官方 checkpoint schema、migration/runtime 账号、连接池和 fenced saver 适配器的运行边界固定。

**Spec 映射：** §6.1–§6.2、§7；AC-04、AC-11。

**验收：**

1. 显式初始化命令按“应用 Alembic → 官方 checkpoint setup/migrate → readiness”执行且连续两次幂等；runtime 账号无 DDL，请求路径永不调用 `setup()`。
2. 每个 App 实例正确创建、复用、readiness 检查并关闭同步 PostgresSaver 连接池；Legacy 模式不依赖 checkpoint 可用，允许 LangGraph 时 checkpoint 不可用则 readiness 失败。
3. fenced saver 适配器只接受服务端 execution/fence 上下文，显式读写完整 `checkpoint_thread_id + checkpoint_ns + checkpoint_id`，并把 `put` 返回值只作为调用内 candidate；只有图停止、精确状态验证通过后才在 fenced finalize 事务提升 accepted head。失效 fence 无法提升 head，半写入和 stale checkpoint 都是孤儿且不会被恢复。

**测试/运行验证：** migration/runtime 两账号权限、生命周期、App 重启、连接池、schema/search path、head CAS 与 no-drift 隔离测试。

**依赖：** T28。

**主要风险：** 请求时意外 DDL、连接泄漏、隐式 latest 读取或 stale owner 篡改 head。

**可回滚停点：** 保持 Legacy 并停用 checkpointer runtime；保留 checkpoint schema，不做破坏性清理。

## T30 — 安全 GraphState、Runtime Context 与生产拓扑骨架

**状态：** `Verified`；三条验收标准通过，独立只读验收无 P0/P1/P2，已本地提交 `9df3a00`

**目标：** 固定可持久 State 边界和完整生产拓扑，但暂不接入 API。

**Spec 映射：** §4–§5、§6.2；AC-01、AC-03。

**验收：**

1. 输入/内部/输出 State 使用严格类型和字段白名单；服务、Principal、当前 turn/fence、Cookie、CSRF、Token、Key 只存在 runtime context。
2. 生产 `CompiledStateGraph` 包含 Spec 全部节点、条件边和 `rehydrate_resume_snapshot`，拓扑/代表路径快照与规定一致，旧 `ConversationService` 不是单一图节点。
3. 实际 strict-msgpack checkpoint bytes/values 递归扫描通过，`draft_patch/safe_tool_result` 额外字段和所有禁用内容被拒绝。

**测试/运行验证：** State 构造/恶意字段、拓扑快照、strict msgpack、真实 PostgresSaver 序列化扫描和无副作用 stub invoke。

**依赖：** T29。

**主要风险：** checkpoint 留存过量输入，或把派生 State 误当授权/草稿权威源。

**可回滚停点：** 生产入口未注入图；回退本 Ticket 不触碰业务表和 checkpoint schema。

## T31 — 确定性路由、只读工具与政策 RAG 图分支

**状态：** `Verified`；三条验收标准通过，独立只读验收无 P0/P1/P2

**目标：** 先迁移无业务写入的路由、数字 Cursor 只读/零写子集、只读工具和政策问答分支；合法且未超限的 duration 消费留给 T32。

**Spec 映射：** §5.1–§5.2、§7；AC-01、AC-02、AC-06。

**验收：**

1. hydrate、help/security/unknown、numeric cursor 只读/零写子集、read-only 和 policy 分支执行规定真实节点序列，并生成与 Legacy 相同的规范化结果。
2. 固定只读 parity 场景 route/outcome 100%，连续两次完整运行零差异；shadow 不调模型、不写草稿。
3. 只有政策问答调用 pgvector 并保留 grounded/insufficient/unavailable 三态；目录、自审批政策和权限解析不走、不显示 RAG。

**测试/运行验证：** 图路径、Router 优先级、数字 Cursor 只读/零写子集、工具白名单、RAG 三态/反证和错误闭合；合法且未超限的 duration 明确 defer T32；直接 invoke 政策、权限解析和安全场景。

**依赖：** T30。

**主要风险：** 拆节点时改变 Router 优先级，或把非 RAG 查询伪装成检索。

**可回滚停点：** 图仍未进入 API；Legacy 黄金样本继续是唯一生产结果。

## T32 — 申请收集、模型解析、草稿 CAS 与步骤幂等

**状态：** `Verified`；预审的 execution/fence 与 interrupt/finalizer 两项 P1 边界已冻结；首轮验收的纠正参数/节点路径两项 P1 已闭环，二轮只读复核 P1=1（revision race 与合法 numeric 两个场景缺 compiled-stream 路径断言）已补齐；二轮独立复核 P0=0、P1=0，已本地提交

**目标：** 完成申请字段解析、权限目录解析、草稿写入和校验节点，让本地副作用可重放。

**Spec 映射：** §5.2、§7；AC-02、AC-05、AC-10。

**验收：**

1. `parse_request_patch → resolve_entitlement → merge → persist_draft_cas → validate` 的缺项、成功、解析失败、quota 耗尽和 revision conflict 与 Legacy 黄金样本一致。
2. 模型两次 attempt、普通草稿 CAS 与合法 numeric duration Cursor 各使用稳定 `operation_id`；ledger reserve/complete 必须与对应 quota/CAS 在同一事务。T32 节点只校验测试夹具预建的 `running` execution、actor 与 fence，不创建、续租、接管或释放 execution；这些生命周期与 stale-owner 全链由 T33 验证。
3. T31–T32 已实现路径（至 `validate_draft/await_requester_confirmation` 边界，不包含尚未实现的 resume/确认语义）的 parity matrix 连续两次 100%：14 个正常场景（12 个矩阵场景 + revision race + 合法 numeric）每轮都先断言真实 compiled-stream 节点路径（path 28/28）再逐字段比较 outcome/phase/quota/Cursor 投影（28/28），另 2 个 quota 异常场景×2 失败行为一致；完整草稿在 await 边界安全结束，不创建 pending/confirmation Cursor，缺项或 numeric 的下一 Cursor 只在成功终态 finalizer 后激活。图不创建 Request、Approval 或 Grant，身份/确认字段和非白名单工具名在副作用前被拒绝。真实 interrupt/pending 由 T34 补全，全意图矩阵由 T41 汇总。

**测试/运行验证：** 14 个正常场景×2 的 path+outcome parity（12 个矩阵场景内嵌 `expected_path`；revision race 两轮锁死 `_PARITY_PATH_RECOVERABLE_RESOLVED`；合法 numeric 两轮锁死 `hydrate→route→handle_numeric_followup→finalize`，均断言 compiled stream 节点序列）、两个 quota 异常×2、纠正参数断言（`calls==2` 且 `corrections==[None, CORRECTION_PROMPT]`，HTTP/超时失败不重试）、字段覆盖、canonical code 仍走资格解析、身份注入、CAS 冲突、重复/并发 operation、合法 numeric 重放先查 completed operation、终态前后 Cursor 时机、fenced finalizer 重放与 stale fence 零写入；直接 invoke 不完整/完整/冲突申请收集路径。夹具负责预建 execution，T32 不把它写成 T33 崩溃恢复证据。

**依赖：** T31。

**主要风险：** 幂等键过粗会吞掉下一条合法输入，过细会重复扣 quota 或增加 revision。

**可回滚停点：** API 仍走 Legacy；步骤表可保留空或只有测试事实。

## T33 — Turn Execution Lease、Fence 与崩溃接管内核

**状态：** `In progress`（已实现，待独立复核）；T33 定向 23 项、Agent 235 项、隔离 PG 9 项通过；Ruff/MyPy/`git diff --check` 通过；尚未标记 `Verified`

**目标：** 在打开 interrupt/API canary 前，证明同 thread 串行、accepted checkpoint head 单调、stale owner 无法写入，且恢复不伪造新用户输入。

**Spec 映射：** §6.2、§7；AC-04、AC-05。

**验收：**

1. begin-input 事务一次分配 `graph_run/input_seq/turn/fence`，原子写 execution、`turn.started` 和清洗后 `message.user`；同 Workspace 第二条 running input 在产生用户事实前稳定 409。
2. graph 运行持有 thread advisory lock，心跳以 execution/fence CAS 续租；所有应用写与 candidate→accepted head 提升均检查同一 fence，stale owner 固定失败，其半写入/孤儿 checkpoint 不能被 implicit latest 恢复。
3. lease 过期且 App B 取得 lock 后，原行 `attempt/fence +1`，`graph_run/checkpoint_thread_id/input_seq/input_turn_id/input_event_id` 不变；如 execution 或其链接的 pending/run 曾有 accepted head，只能从事务中种入/后续 CAS 的精确 `checkpoint_thread_id + checkpoint_id + namespace` 继续，绝不得从输入重启；只有三者从未接纳过任何 head 时才能从安全 `input_event_id` 重建。终态仍归属原 turn，新输入在恢复中固定 `TURN_RECOVERY_IN_PROGRESS`。

**测试/运行验证：** 并发 begin、heartbeat CAS、advisory lock、head 单调、stale 业务/事件/terminal 写、过期接管、无 checkpoint/有 checkpoint/END 恢复。

**依赖：** T32。

**主要风险：** 把客户端重试误当崩溃接管，或让旧 executor 在 lease 释放后继续写 checkpoint/业务事实。

**可回滚停点：** 入口仍 Legacy；可停用 recovery runtime，保留空 execution/pending/checkpoint 数据。

## T34 — 申请人确认 Interrupt/Resume 与原子投影

**状态：** 尚未开始

**目标：** 在图内完成唯一 P0 业务 interrupt，关闭 terminal/Cursor 窗口，并保证 resume 中的新输入不丢失。

**Spec 映射：** §5.1–§6.3、§7；AC-04、AC-05。

**验收：**

1. 字段完整时生成安全、JSON 可序列化 confirmation interrupt；interrupt 节点自身不写业务表、不扣 quota、不调模型/工具、不创建正式申请。
2. accepted interrupt head 之后，pending 投影、confirmation Cursor、`agent.input.required → business.status → message.completed`、execution terminal 和 lease 释放在同一应用事务提交；checkpoint-only 崩溃由 App B 补全原 turn，不会出现 terminal-only/Cursor-only。
3. 接受 resume 的应用事务先验证 active pending 的精确 interrupt head，再原子建立新 execution/`message.user`、把该 `checkpoint_thread_id + checkpoint_ns + accepted_checkpoint_id` 种入 execution，并把旧 pending 标为 `resuming`；“该事务已提交、图还未调用”时崩溃必须从种入 head 恢复。图只调一次 `Command(resume)`，只有现有确认函数返回 True 才进确认 CAS，其他输入经 rehydrate 在同一调用重新路由。最终 confirm/重路由/失败都在同一 fenced 应用事务中关闭或替换 pending/Cursor、终止 execution 并写唯一 terminal；Principal、Session、pending、Cursor、revision 任一不匹配安全闭合，确认写最多一次。

**测试/运行验证：** 真实 PostgreSQL interrupt、checkpoint-only 崩溃、“resume 输入事务已提交但图还未调用”崩溃、App A/B、confirm、修改字段、换题、拒绝、无法分类、错身份/会话和 revision conflict；断言恢复使用种入的精确 head 且 rehydrate 必经。

**依赖：** T33。

**主要风险：** Saver 与应用表不是 2PC；必须允许 checkpoint-only 可恢复窗口，但绝不允许终态先可见。

**可回滚停点：** interrupt 尚未开放到生产入口；checkpoint/pending 可保留但不会被 API resume。

## T35 — Workspace Sticky Engine、Pending 对账与 Legacy 降级桥

**状态：** 尚未开始

**目标：** 在切流前建立不可被 endpoint 分裂的 Workspace 引擎绑定、发布对账和无业务反向同步的降级路径。

**Spec 映射：** §9、§10；AC-12。

**验收：**

1. `legacy/mixed/langgraph` 只决定新 Workspace 的 server-side flow 分配；flow 1/2 分别粘性绑定 Legacy/LangGraph，JSON/SSE 始终同引擎，客户端覆盖和非法 canary 配置失败。
2. 解析顺序固定为 running execution → active/resuming pending → Workspace flow；不一致固定 409 `ENGINE_BINDING_CONFLICT`，跨 JSON/SSE 的 pending/resume 仍进原引擎，不修改另一引擎状态。
3. 节点删除/重命名/重排门禁只在“live 应用 pending=0、unfinished execution=0、对应 live accepted checkpoint task=0”且全部 retained pending task 都能分类时通过；回滚中只有对账一致的 confirmation 能原子降为 flow 1 Cursor，并为保留的旧 head 写不可变 retirement tombstone。Tombstone head 明确不再 resume、不计入 live 零值；未知 kind、未 tombstone 的孤立 accepted head、投影/task 不一致或不可读均阻断。

**测试/运行验证：** 全配置/分配矩阵、稳定 hash、客户端注入、跨入口 pending、绑定冲突、双向对账、未知 kind、三类降级和 dry run。

**依赖：** T34。

**主要风险：** 同 Workspace 被 JSON/SSE 分流，或根据不完整投影臆测 checkpoint 状态。

**可回滚停点：** 默认仍 `legacy`；不一致状态宁可阻断，不删 checkpoint 或改业务数据。

## T36 — 真实、安全、可去重的轨迹事件后端

**状态：** 尚未开始

**目标：** 从真实图节点、模型、RAG、工具和状态边界持久化可给用户阅读的运行事实。

**Spec 映射：** §8；AC-03、AC-06、AC-07、AC-08。

**验收：**

1. 每种新事件使用独立 `extra=forbid` Schema，v1.2 terminal/draft 合同完整继承；禁用 key/敏感值扫描在落库前执行，确定性 event/step/tool key 使重放不产生第二组事件。
2. node/model/retrieval/tool started/completed 只在真实执行边界写入且顺序正确；LangGraph 模式不再从 `tool.summary` 事后合成 started，只有 `search_policies` 产生 pgvector retrieval 事件。
3. 公开投影只返回对应事件 Schema 的安全字段，按全局 DB event ID 选择/排序最近三轮；未知/Legacy 安全降级，不读 raw checkpoint/debug stream。

**测试/运行验证：** 逐事件 Schema、Legacy 终态回归、顺序、唯一键、attempt/ordinal、RAG 反证、敏感字段、历史 null key、最近三轮和未知事件。

**依赖：** T35。

**主要风险：** 为增强可观测性而泄露 prompt、Provider 输出、SQL、向量、凭证或隐藏推理。

**可回滚停点：** API 仍未切流；Legacy 事件合同保留，新事件可被旧前端忽略。

## T37 — 六个崩溃边界的幂等恢复

**状态：** 尚未开始

**目标：** 在任何真实 flow 2 canary 前，证明 checkpoint 和应用事务非原子窗口不会丢输入或重复本地副作用。

**Spec 映射：** §7、§12.1；AC-04、AC-05、AC-07。

**验收：**

1. 默认关闭、仅测试可注入且不进入公开 API/UI 的 fault hook 能精确触发 Spec 六个崩溃点。
2. 六个场景 crash→restart→reconcile/resume 后，run/input/turn 不变、attempt/fence 只在接管递增，安全输入不丢，quota、草稿 revision、accepted head、轨迹和终态均最多一次，Request/Approval/Grant 数量不变。
3. Provider 已返回但完成事实未持久化时允许外部调用 at-least-once，但本地 quota 最多一次；stale owner 的业务写/head CAS 被拒绝，checkpoint 失败不提前释放 lease 或伪造终态。

**测试/运行验证：** 每个崩溃点查 operation facts、execution/pending、Cursor、checkpoint head、草稿、事件、终态和正式业务表；保留可复现故障证据。

**依赖：** T36。

**主要风险：** 测试 hook 泄露成产品故障控制面，或把 checkpoint 成功误当业务副作用已提交。

**可回滚停点：** fault hook 生产默认关闭；入口继续 Legacy，不清理持久 checkpoint。

## T38 — JSON 生产图隔离入口门禁

**状态：** 尚未开始

**目标：** 让真实 JSON API 适配器在隔离测试中调用生产图，但不创建“JSON 已切/SSE 未切”的持久 Workspace。

**Spec 映射：** §6.3、§9、§10.1；AC-01、AC-02、AC-04。

**验收：**

1. 测试显式注入 LangGraph orchestrator 时，真实 JSON 路由进入生产 `CompiledStateGraph`，规范化 DTO、错误和 Legacy 黄金样本一致；默认运行仍 Legacy。
2. App A 在确认处写原子 pending/Cursor/terminal 后退出；App B 以后续新 input turn、同 thread/run 经 rehydrate 完成 confirm 或非确认重路由，且恢复性崩溃不丢消息。
3. 同 Workspace 并发 invoke/resume 只一个进图；错身份/revision/pending 失败闭合，每个 HTTP turn 只一个终态，测试结束不留真实 flow 2 Workspace。

**测试/运行验证：** JSON API 路径断言、resolved engine、真实 App A/B、confirm/非 confirm/并发/崩溃恢复，以及默认 Legacy 反证。

**依赖：** T37。

**主要风险：** 把隔离入口验证误当真实 canary，或在 SSE 尚未就绪时为用户创建 flow 2 pending。

**可回滚停点：** 移除测试注入即可；默认配置、Workspace flow 和产品入口仍是 Legacy。

## T39 — 「对话 / 轨迹」只读 UI 与最近三轮

**状态：** 尚未开始

**目标：** 用真实事件呈现可访问、不可操作的 Agent Loop 轨迹。

**Spec 映射：** §3、§8.3；AC-08。

**验收：**

1. 顶部「对话 / 轨迹」Tab 可由键盘切换，切换不发起 API、不 resume、不执行工具、不提交申请，不影响当前对话状态。
2. 最近三轮按 DB event ID 展示真实引擎、节点、模型、RAG、工具、State、HITL 和终态，最新轮默认展开；Legacy/Unknown 不伪装为 LangGraph。
3. 空、加载、运行中、可恢复错误、完成和未知事件稳定渲染；详情不显示 Spec 禁止内容，也没有恢复/重放/编辑/提交按钮。

**测试/运行验证：** Vitest/Testing Library 覆盖 Tab、三轮排序、引擎、全状态、未知事件和零副作用；ESLint、TypeScript、build；1440×900、1024×768、390×844 与键盘 smoke。

**依赖：** T38。

**主要风险：** 前端根据 intent 猜测未发生节点，或把只读轨迹误做成开发者控制台。

**可回滚停点：** 可隐藏轨迹 Tab 并保留安全事件；对话和 JSON 门禁不受 UI 回退影响。

## T40 — SSE LangGraph 门禁、传输合同与 Sticky Flow 2 Canary

**状态：** 尚未开始

**目标：** 先证明 SSE 适配器不破坏成熟传输合同，再首次为真实新 Workspace 整体绑定 LangGraph。

**Spec 映射：** §9、§10.1；AC-01、AC-09、AC-12。

**验收：**

1. 隔离注入下真实 SSE 入口调用生产图；首帧、v1 envelope、连续 seq、`turn_id:seq`、delta 不落库、completed 先持久、唯一终态、取消、敏感跨 chunk、错误闭合、Last-Event-ID 和背景流去重全部通过，不透传 LangGraph debug/value stream。
2. SSE 断开只在 graph worker 确认停止后写 interrupted/释放 lease；后台仍可能运行时 execution 保持 running 并由恢复合同闭合；同一输入 JSON/SSE 规范化 outcome、interrupt terminal、Cursor 和业务事实一致。
3. JSON 与 SSE 隔离门禁都通过后，`mixed` 才以服务端稳定 cohort 为新 Workspace 写 flow 2；该 Workspace 两入口同时走 LangGraph，通过 JSON→SSE 与 SSE→JSON 跨入口 pending/resume，flow 1 仍 Legacy，resolved engine/持久事件/UI 徽标一致。

**测试/运行验证：** 新增 LangGraph SSE 失败测试后跑完整流式/重连/Nginx 回归；实际完成申请收集→interrupt→跨入口 resume、断线重连和受控 flow 2 Workspace canary。

**依赖：** T39。

**主要风险：** 框架流直接暴露浏览器，或为一个 Workspace 创建 endpoint 分裂引擎。

**可回滚停点：** 停收新 Workspace/turn，排空 execution，用 T35 对账/映射降为 flow 1；不删 checkpoint 或业务数据。

## T41 — v1.3 全量评测、浏览器验收与回滚演练

**状态：** 尚未开始

**目标：** 在同一新 revision 上证明 AC-01–AC-13 全部成立，并完成 sticky flow 与完整 Legacy 回滚。

**Spec 映射：** §10–§12；AC-01–AC-13。

**验收：**

1. Pytest、Ruff、MyPy、Alembic、Vitest、ESLint、TypeScript、Vite build、固定评测与 v1.2 ACL/Packet/审批/开通攻击矩阵全部通过。
2. 同一 revision 完成重启确认、grounded RAG、只读权限解析、安全探测、可恢复错误、四角色 Case 闭环、最近三轮轨迹、三视口和键盘运行验收。
3. 完成“停收 → 排空 → pending/checkpoint 双向对账 → flow 2 降级 → legacy 重启”演练；旧/新 Workspace、草稿、事件、Cursor、正式 Case 和 Grant 继续可读，任一 AC 失败则不设 `product_verified=true`。

**测试/运行验证：** 固定 parity 连续两次零差异；记录真实场景数、路径一致率、恢复成功率、事件完整率、泄漏数和可观测性能数据，不复用历史 `425/87/101`。

**依赖：** T40。

**主要风险：** 通过调整评测分母、跳过旧安全回归或用目标值代替实测结果制造全绿。

**可回滚停点：** 任何 AC 失败都保持/回到 Legacy，不推送、部署、删除 Legacy 或 checkpoint。

## T42 — Claim Ledger、限制披露与简历证据包

**状态：** 尚未开始

**目标：** 把已验证的 LangGraph 能力转化为可追溯证据，但不直接修改正式简历。

**Spec 映射：** §11 AC-14、§12.3、§13；AC-14。

**验收：**

1. LangGraph 真实主链、PostgreSQL checkpoint、interrupt/resume、RAG 路径、只读轨迹和六个故障恢复 Claim 都链接到同一 T41 revision 的代码入口、测试、运行证据和限制。
2. Product Manifest/评测证据只记录 T41 实测指标；Provider at-least-once、Mock 身份、无真实 SSO/IAM、无 ReAct/Multi-Agent/SLA/Token 成本/可观测平台等边界明确披露。
3. 形成可核验的简历候选表述、Agent Loop 讲解图和 Demo 证据，但正式简历保持未修改；只有用户另行确认后才允许采用。

**测试/运行验证：** 文档链接、revision、指标来源、Claim→证据反向抽查和边界审计；独立 reviewer 只读复核 AC-14。

**依赖：** T41。

**主要风险：** 把框架接入、Mock 场景或未测指标包装成生产规模、自主 Agent 或本人已掌握。

**可回滚停点：** 本 Ticket 只改证据文档；证据缺口回流对应实现 Ticket/T41，不降低 Claim 门槛。

## 2. 覆盖与当前停点

- T26–T40 分层建立 AC-01–AC-12；T41 在同一 revision 汇总判定 AC-01–AC-13；T42 单独完成 AC-14；
- 数据库、checkpoint、Graph State、JSON、SSE、UI 和证据按依赖串行，不并发修改共享权威源；
- **当前停点：T33 待独立复核；复核通过后进入 T34。** 仍不推送、合并、部署或修改正式简历。
