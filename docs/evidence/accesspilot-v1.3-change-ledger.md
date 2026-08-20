# AccessPilot v1.2 → v1.3 改造与简历价值账本

**用途：** 每完成一张 Ticket，同步记录技术改变、解决的问题、验证证据与可面试价值。
**v1.2 基线：** 顺序执行的 `ConversationService` + 业务 Cursor；仅政策问答使用 pgvector RAG；当前生产入口未由 LangGraph 编排。
**v1.3 目标：** 真实 LangGraph Agent Loop + PostgreSQL 持久恢复 + HITL interrupt/resume + 只读真实轨迹。
**当前总状态：** `In progress`；T26–T39 `Verified`，T40–T42 `Planned`。用户已授权 T39 仅本地提交；正式简历仍未修改，也不得将后续未完成的 SSE/真实 canary 或完整产品验收能力当作已实现成果。

## 状态口径

- `Planned`：只有 Spec/Ticket，不得宣称已实现。
- `Implemented`：代码与定向测试已落地，但还没有完成独立验收。
- `Verified`：同一 revision 的测试、运行与独立验收都有可回溯证据，才能生成简历候选表述。

## 滚动改造记录

| Ticket | 技术改变 | 解决的问题 | 简历 / 面试价值 | 证据链接 | 状态 / 限制 |
|---|---|---|---|---|---|
| T26 | 锁定 LangGraph / PostgresSaver / psycopg 矩阵；用真实 PostgreSQL 实测修正为每 run 独立 checkpoint thread 和 candidate→accepted 两阶段 head | 提前暴露 root namespace、半写入 interrupt、精确 head 恢复与跨平台依赖漂移风险 | 可讲“先做兼容性 Spike，用运行证据修正架构合同，再工程化接入”，而不是只堆框架名 | [T26 验收证据](accesspilot-v1.3-t26-acceptance-2026-08-17.md)、[双评审决策](../reviews/accesspilot-v1.3-t26-acceptance-decisions-2026-08-17.md)、[Probe](../../scripts/t26_langgraph_compat.py)、[测试](../../apps/api/tests/agent/test_t26_langgraph_compat.py)、[R-29/R-30 裁决](../reviews/accesspilot-v1.3-spec-review-decisions-2026-08-16.md#2-议题清单) | `Verified`；T26-only 快照中 PostgreSQL probe/9 项测试、Python 3.11 新环境、Python 3.12 Docker、后端 451 项/旧前端 95 项与全量评测均通过；Claude Code + DeepSeek 均无 P0/P1；生产 saver/fence 与主链仍属后续 Ticket |
| T27 | 抽象 `ConversationOrchestrator`，让 JSON/SSE 只依赖可注入边界；用 engine-owned success finalizer 保持 terminal→Cursor→emit 顺序，并冻结 Legacy 黄金样本 | 在不改变对外行为的前提下建立可替换内核，避免接口层继续理解 Legacy Cursor 状态机 | 可讲 Strangler 迁移、依赖注入、契约测试、独立验收发现边界泄漏后的闭环修复与一键回滚 | [T27 验收证据](accesspilot-v1.3-t27-acceptance-2026-08-17.md)、[实现](../../apps/api/src/accesspilot/conversation.py)、[黄金测试](../../apps/api/tests/conversation/test_t27_orchestrator.py) | `Verified`；定向 27 项、相关 151 项、API 467 项通过，另有 1 项 T26 专用数据库 probe 因未配置变量跳过；独立验收无 P0/P1；默认仍 100% Legacy，生产 LangGraph 尚未接入 |
| T28 | 通过 0010 迁移增加 Workspace thread/flow/fence、execution/pending/step facts 与 nullable event key；用 DB CHECK/partial unique/复合 FK/trigger 固化状态不变量，并实现 canonical JSON + SHA-256 稳定身份 | 为后续恢复、粘性切流和 checkpoint 重放提供应用权威事实；防止跨 Workspace 引用、并发双 running、tombstone 复活与重复事件 | 可深入讲数据库状态机、幂等身份、含数据可逆迁移、真实 PostgreSQL 约束反证和 checkpoint Schema 隔离 | [T28 验收证据](accesspilot-v1.3-t28-acceptance-2026-08-17.md)、[迁移](../../apps/api/migrations/versions/20260817_0010_add_agent_runtime_facts.py)、[身份工具](../../apps/api/src/accesspilot/agent/identity.py)、[真实 PG 测试](../../apps/api/tests/db/test_t28_postgres.py) | `Verified`；定向 23 项、相关 58 项、完整 API 491 项通过；fresh 与含数据 0009→0010→0009→0010/no-drift 通过；默认仍为 Legacy，T29 saver 生命周期与生产图尚未实现 |
| T29 | 接入官方同步 PostgresSaver 生命周期；拆分显式初始化与普通 API 启动、migration/runtime 账号、独立 schema 与连接池；增加 exact-locator fenced adapter 和 candidate→accepted CAS primitive | 消除请求期 DDL、Legacy 对 checkpoint 的隐式依赖、连接泄漏、implicit latest 与 stale/half-write checkpoint 被错误恢复的风险 | 可讲最小权限、readiness/资源生命周期、官方 Saver 适配、精确 head 与独立评审发现版本语义泄漏后的闭环 | [T29 验收证据](accesspilot-v1.3-t29-acceptance-2026-08-17.md)、[运行适配器](../../apps/api/src/accesspilot/agent/checkpoint.py)、[初始化入口](../../apps/api/src/accesspilot/checkpoint_init.py)、[真实 PG 测试](../../apps/api/tests/db/test_t29_postgres.py) | `Verified`；单元 12 项、相关 44 项、真实 PG 1 项、完整 API 502 项通过（普通全量另有 T26/T29 两项专用 PG 测试按环境门控跳过）；默认仍 Legacy，T33 lease/advisory/takeover 与生产图尚未实现 |
| T30 | 建立 26 字段严格 GraphState、Runtime Context 和 22 节点生产拓扑骨架；增加 invoke 前输入与节点返回前 update 校验，并让 strict allowlist 传播到真实 PostgresSaver I/O | 防止额外输入或错误节点更新先进入 checkpoint；隔离服务、Principal、Token/Cookie/CSRF/API key；锁定后续业务节点的安全骨架 | 可讲类型化 Agent State、runtime/checkpoint 边界、LangGraph 持久化时机陷阱、真实 bytes/decoded 双重泄密扫描与独立评审闭环 | [T30 验收证据](accesspilot-v1.3-t30-acceptance-2026-08-17.md)、[生产图](../../apps/api/src/accesspilot/agent/production_graph.py)、[严格 State](../../apps/api/src/accesspilot/agent/state.py)、[真实 PG 测试](../../apps/api/tests/db/test_t30_postgres.py) | `Verified`；T30 57 项、相关 69 项、Agent 134 项、完整 API 559 项通过；T29/T30 真实 PG 各 1 项通过；节点仍为无副作用 stub，T31/T32/T34/API 接线未实现 |
| T31 | 将 hydrate、确定性路由、只读工具、numeric 只读子集和政策 pgvector RAG 三态迁入真实图节点；下游 compose 只依赖可持久化的脱敏 State，不依赖进程内 scratch | 让只读 Agent Loop 真正按节点运行，并以 fresh-runtime 节点级重放验证下游状态依赖；明确 generic policy search 与目录/自审批/权限解析的非 RAG 边界 | 可讲节点级迁移、Legacy parity、shadow route-only、RAG 三态、节点间状态完整性与避免“什么都叫 RAG” | [T31 验收证据](accesspilot-v1.3-t31-acceptance-2026-08-17.md)、[生产图](../../apps/api/src/accesspilot/agent/production_graph.py)、[两轮 parity 测试](../../apps/api/tests/agent/test_t31_readonly_graph.py) | `Verified`；14 场景×2 的 route/outcome 28/28 均 100% 且零差异，fresh-runtime 节点级反证 6 项通过；T31 35 项、相关 262 项、完整 API 594 项通过；尚未验证 PostgreSQL checkpoint resume 或进程重启，合法 duration 写入、模型/草稿 CAS、interrupt 与 API 接线未实现 |
| T32 | 将申请解析、权限解析、草稿 CAS 与步骤幂等迁入图；quota/CAS/Cursor 各用稳定 operation 的同一事务 ledger，重放先查 completed；缺项下一 Cursor 由 fenced finalizer 同事务写入 | 防止重试时重复扣 quota、覆盖新草稿或重复提交本地副作用；防止失效 executor 自提交 Cursor 投影 | 可讲解 CAS、operation ledger、at-least-once 执行下的本地 exactly-once、canonical 锁顺序与 fenced finalizer | [T32 验收证据](accesspilot-v1.3-t32-acceptance-2026-08-17.md)、[步骤操作](../../apps/api/src/accesspilot/agent/step_operations.py)、[生产图](../../apps/api/src/accesspilot/agent/production_graph.py)、[两轮 parity 测试](../../apps/api/tests/agent/test_t32_request_graph.py) | `Verified`；14 个正常场景×2 的 path+outcome 28/28 断言已齐（含 revision race 与合法 numeric），T32 52 项、Agent 221 项通过（完整 API 646 项为历史证据）；二轮独立复核 P0=0、P1=0；execution 生命周期/接管（T33）与 interrupt/resume（T34）未实现 |
| T33 | 建立 turn lease、fence、advisory lock 与精确 checkpoint head 接管 | 防止同 thread 并发双写、旧 executor 续写和崩溃后丢输入 | 可深入讲分布式 lease/fencing、并发一致性和故障接管 | [T33 验收证据](accesspilot-v1.3-t33-acceptance-2026-08-17.md)、[执行服务](../../apps/api/src/accesspilot/agent/turn_execution.py)、[图 runner](../../apps/api/src/accesspilot/agent/turn_runner.py)、[隔离 PG 测试](../../apps/api/tests/db/test_t33_postgres.py) | `Verified`；T33 定向 23 项、Agent 235 项、隔离 PG 9 项通过；Ruff/MyPy/diff-check 通过；独立复核 P0=0、P1=0；T34 interrupt/resume 未实现 |
| T34 | 用 LangGraph interrupt/resume 实现持久的申请人确认 | 关闭 checkpoint、pending、Cursor 和 terminal 之间的丢消息窗口 | 可讲解 Human-in-the-loop、跨进程恢复与非 2PC 一致性取舍 | [T34 验收证据](../evidence/accesspilot-v1.3-t34-acceptance-2026-08-18.md)、[图测试](../../apps/api/tests/agent/test_t34_graph_interrupt.py)、[真实 PG 测试](../../apps/api/tests/db/test_t34_postgres.py)、[服务测试](../../apps/api/tests/agent/test_t34_service_primitives.py) | `Verified`；生产图真实 interrupt()/单次 Command(resume) 接线、rehydrate/确认 CAS、`finalize_resume_outcome` 与 pending 原子替换已落地；定向/回归测试与独立验收均已闭环，P0/P1=0 |
| T35 | 实现 Workspace 粘性引擎、pending/checkpoint 对账与 Legacy 降级桥 | 防止 JSON/SSE 将同一 Workspace 分流到不同引擎，保证可控回滚 | 可讲解粘性切流、双向对账门禁、不破坏降级与 rollback/resume 并发原子性 | [T35 实现](../tickets/accesspilot-langgraph-agent-loop-v1.3.md#t35--workspace-sticky-enginepending-对账与-legacy-降级桥)、[粘性绑定测试](../../apps/api/tests/agent/test_t35_engine_binding.py)、[对账/降级测试](../../apps/api/tests/agent/test_t35_rollback.py)、[真实 PG 证明](../../apps/api/tests/db/test_t35_postgres.py) | `Verified`；第 2 轮独立验收关闭 R01–R05，P0/P1=0；Codex 实测 T35 定向 64 项（含 8 项隔离真实 PG）与受影响回归 168 项全部通过且无跳过，Ruff/MyPy/diff-check 通过；Pi 报告完整 API 756 项通过、3 项历史环境门控跳过；提交 `8ef9e36` 已推送到 `origin/accesspilot-v1.3`；默认入口仍 Legacy，mixed canary 强制 0 |
| T36 | 从真实 node/model/RAG/tool 边界产生脱敏、可去重轨迹事件，并在所有轨迹写入前校验 execution fence | 替代事后根据 `tool.summary` 合成轨迹，防止重放产生第二组事件、失效 executor 续写或 prompt/凭证/隐藏推理泄露 | 可讲解 Agent observability 事件合同、脱敏、稳定事件身份与 fenced 写入 | [轨迹实现](../../apps/api/src/accesspilot/agent/trace.py)、[图边界测试](../../apps/api/tests/agent/test_t36_trace_graph.py)、[事件测试](../../apps/api/tests/events/test_t36_trace.py)、[真实 PG 测试](../../apps/api/tests/db/test_t36_postgres.py) | `Verified`；第 2 轮独立验收关闭 R01 fence 校验缺口，P0/P1=0；Codex 实测 T36 定向 43 项、T28/T30–T35 及事件/对话受影响回归 266 项通过，Ruff/MyPy（56 个源码文件）/diff-check 通过；API 仍未切流，UI 尚未完成 T39 |
| T37 | 增加六个进程内、默认关闭、单次触发的 fault hook；用稳定 operation、lease/fence、candidate→accepted head 与 graph-only fenced finalizer 恢复同一逻辑输入 | 证明 checkpoint/应用事务非原子窗口中安全输入不丢、本地 quota/草稿/轨迹/终态最多一次，并让 Provider/只读工具的 at-least-once 边界保持诚实 | 可讲解故障注入、应用级幂等、stale owner 拒绝和独立验收发现普通 END promotion 缺口后的闭环 | [T37 验收证据](accesspilot-v1.3-t37-acceptance-2026-08-20.md)、[fault hook](../../apps/api/src/accesspilot/agent/fault_injection.py)、[恢复测试](../../apps/api/tests/agent/test_t37_recovery.py) | `Verified`；本地提交 `e1e47cc`，独立复核 P0/P1/P2=0。六点套件使用真实 PostgreSQL 应用事实与共享 InMemorySaver，真实 PostgresSaver 由 T34/T36 disposable PostgreSQL 回归补证；JSON/SSE 仍未切流，不得宣称生产高可用 |
| T38 | 以服务端显式注入把真实 JSON 路由接到生产 CompiledStateGraph；复用 TurnExecution/fenced runner/官方 PostgresSaver，并保持默认 Legacy 与 SSE 失败闭合 | 验证图不只在节点单测中运行；证明 App A/B 跨进程 confirm/非确认与崩溃接管；以 Workspace 锁内 live-pending 守卫关闭迟到 new-input 竞态 | 可讲解分层集成门禁、Strangler 入口兼容、跨 runtime 恢复、并发 TOCTOU 复现与原子失败闭合 | [T38 验收证据](accesspilot-v1.3-t38-acceptance-2026-08-20.md)、[JSON 图适配器](../../apps/api/src/accesspilot/agent/json_orchestrator.py)、[真实 API/PG 测试](../../apps/api/tests/api/test_t38_json_langgraph.py) | `Verified`；本地提交 `0aa953c`，独立复验 P0/P1/P2=0，T38 23 项（含官方 PostgresSaver App A/B 4 项）通过；仅隔离注入，默认仍 Legacy、SSE 未接、mixed canary 仍为 0，不等于真实 canary |
| T39 | 将现有 Legacy 轨迹原型升级为按真实 T36 事件渲染的只读最近三轮；增加逐轮 engine、状态分类、安全详情白名单与可访问 Tab | 防止前端按 intent 伪造节点、Legacy 冒充 LangGraph、未知/敏感 payload 泄露，并保证切换视图不卸载对话或触发副作用 | 可讲解“可解释性产品化”、事件驱动 UI、可访问 Tab、前端最小披露与三尺寸验证 | [T39 验收证据](accesspilot-v1.3-t39-acceptance-2026-08-20.md)、[轨迹组件](../../apps/web/src/AgentTrajectory.tsx)、[组件测试](../../apps/web/src/AgentTrajectory.test.tsx) | `Verified`；独立验收 P0/P1/P2=0/0/1，完整 Web 103 项与三尺寸/键盘/零请求检查通过；T40 前真实浏览器仍诚实显示 Legacy，LangGraph 全事件由组件合同测试证明；reduced-motion 箭头 transition P2 不阻塞 |
| T40 | 保持 SSE 重连/唯一终态合同，再将真实 flow 2 Workspace 整体 canary | 防止断线后后台重复执行或 JSON/SSE 引擎分裂 | 可讲解流式传输可靠性、渐进式发布和跨入口一致性 | [T40 计划](../tickets/accesspilot-langgraph-agent-loop-v1.3.md#t40--sse-langgraph-门禁传输合同与-sticky-flow-2-canary) | `Planned`；未切流、未部署 |
| T41 | 对同一 revision 执行全量评测、浏览器验收和回滚演练 | 防止复用 v1.2 数字或只用单元测证明 v1.3 产品成立 | 可用实测指标和回滚演练支撑工程结果，而不是框架名词堆叠 | [T41 计划](../tickets/accesspilot-langgraph-agent-loop-v1.3.md#t41--v13-全量评测浏览器验收与回滚演练) | `Planned`；`product_verified` 不得沿用 v1.2 |
| T42 | 将实现、测试、指标、限制与简历 Claim 反向链接 | 防止把 Mock、AI 协助或未测能力包装成生产经历 | 生成可核验的简历候选表述与 Agent Loop 面试讲解材料 | [T42 计划](../tickets/accesspilot-langgraph-agent-loop-v1.3.md#t42--claim-ledger限制披露与简历证据包) | `Planned`；不直接修改正式简历 |

## 每张 Ticket 的追记模板

```markdown
### YYYY-MM-DD · Txx · Planned → Implemented / Verified
- 改变：v1.2 是什么 → v1.3 改成什么
- 价值：解决的可复现问题；可用什么面试故事讲解
- 证据：revision / 代码 / 测试 / 运行记录 / 独立验收
- 限制：未验证、Mock、外部 Provider 或生产环境边界
- 简历候选表述：仅在 Verified 后填写；正式简历仍需用户确认
```

## 已发生的决策变更

### 2026-08-16 · T26 · 根图 checkpoint 隔离方式

- 改变：原计划为每个 graph run 伪造非空 `checkpoint_ns`；候选 LangGraph 1.2.11 实测后改为每 run 独立 `checkpoint_thread_id`，namespace 只保存 saver 返回值。
- 解决：根图会把传入 namespace 归一为 `""`，`get_state()` 却把非空 namespace 当作子图路径；旧设计会导致恢复查询失败。
- 证据：候选版本隔离环境运行显示 `stored_namespaces=['']`、自定义 namespace 读取返回 `Subgraph accesspilot not found`，而精确 checkpoint ID 在独立 checkpoint thread 上可恢复 interrupt。
- 简历价值：可作为“通过兼容性 Spike 提前发现并修正 checkpoint 隔离设计”的面试事例；仍不写成“已完成 LangGraph 生产持久化”。

### 2026-08-16 · T26 · accepted checkpoint 时机

- 改变：原计划每次 saver `put` 后立即 CAS accepted head；实测后改为调用结束、精确状态验证通过后才提升。
- 解决：dynamic interrupt 先写 head，再写 `__interrupt__`；旧设计可能将半写入 checkpoint 暴露为可恢复状态。
- 证据：候选 1.2.11 调用顺序已观测为 `put(head) → put_writes(__interrupt__)`；Spec 和 T26/T29/T33/T34 合同已回流。
- 简历价值：可讲“基于持久化顺序设计 candidate→accepted 两阶段 head，避免半写入恢复”；目前仍不得声称生产已实现。

### 2026-08-16 · T26 · interrupt 判停方式与 strict msgpack（新增实测）

- 改变：LangGraph 1.2.11 的 `invoke()` 不抛出 `Interrupt` 异常，而是把 `__interrupt__` 写进返回状态；恢复统一用 `Command(resume=...)`。v1.3 的 pending 判定与 resume 入口按此实现。
- 解决：0.6.x 时代的 `try/except Interrupt` 判停模式在 1.2.11 不再成立；若沿用会导致 pending 永不落库或重复 invoke。
- 证据：`scripts/t26_langgraph_compat.py` + `apps/api/tests/agent/test_t26_langgraph_compat.py`（9/9 通过）；实验进程在导入 LangGraph 前开启 `LANGGRAPH_STRICT_MSGPACK=true`，且真实 PostgresSaver 的 interrupt/resume 路径通过。
- 限制：T26 只验证候选框架与 saver 合同；AccessPilot 生产 GraphState 字段白名单与 checkpoint 泄密扫描属于 T30，不在此处越界声称。
- 简历价值：可作为“升级主框架版本时用兼容性探针逐项验证行为合同，而不是只改依赖版本号”的工程事例。

### 2026-08-17 · T26 · 跨平台依赖闭环

- 改变：候选矩阵不再只在开发机解析；全新 Python 3.11 环境与 Python 3.12 Linux Docker 都按同一 constraints 安装。
- 解决：Docker 实测发现 macOS 未安装、Linux 必需的 `greenlet==3.5.5`，已补入 lock；防止“本机绿、镜像漂移”。
- 证据：五个候选核心包在三个环境中精确一致；平台条件依赖按同一 lock 解析，Docker 输出 `installed_unlocked=[]` 且 `version_mismatches={}`。
- 简历价值：可讲“用清洁环境和容器双重验证消除 Agent 框架升级的平台漂移”；不把条件依赖误说成三环境包集完全相同。

## T26 Verified 简历候选表述

> 在 AccessPilot 主链迁移前设计 LangGraph 1.2 + PostgresSaver 兼容性 Spike，通过真实 PostgreSQL 跨进程 exact-head resume 实测，发现 root namespace 归一和 interrupt 半写入窗口，将恢复合同修正为 per-run checkpoint thread 与 candidate→accepted 两阶段 head；锁定 Python 3.11/3.12 与 Docker 依赖，保持 v1.2 后端 451 项、前端 95 项及产品评测全绿。

使用边界：该表述只说明“生产迁移前的兼容性与架构验证”；不能写成 AccessPilot 生产主链已使用 LangGraph。正式简历仍需用户另行确认后才修改。

## T27 Verified 简历候选表述

> 为 AccessPilot 的 LangGraph 渐进迁移建立 `ConversationOrchestrator` Strangler 边界，将 JSON/SSE 入口与 Legacy 编排解耦，并以全意图 outcome、Cursor 三态、流式终态和异常黄金测试锁定兼容合同；独立验收发现并修复接口层 Cursor 业务规则泄漏，相关 151 项与完整 API 467 项回归通过（另有 1 项 T26 专用数据库 probe 因未配置变量跳过）。

使用边界：该表述只说明“已建立可替换编排边界并冻结 Legacy 合同”；当前默认仍为 Legacy，不能写成生产主链已经运行 LangGraph。正式简历仍需用户另行确认后才修改。

## T28 Verified 简历候选表述

> 为可恢复 Agent Loop 设计应用侧运行事实层，通过 Alembic 迁移建立 Workspace thread/flow/fence、execution/pending/step ledger 与确定性 event/operation identity；使用 PostgreSQL CHECK、partial unique、复合外键和不可变 trigger 固化并发与恢复不变量，并完成含历史数据的 0009→0010→0009→0010 可逆迁移、checkpoint Schema 隔离和 491 项 API 回归。

使用边界：该表述说明“运行事实和恢复不变量的数据底座已验证”；T29 的官方 PostgresSaver 生命周期、fenced adapter、生产 LangGraph 主链和真实崩溃恢复尚未实现。正式简历仍需用户另行确认后才修改。

## T29 Verified 简历候选表述

> 为 AccessPilot 接入官方 PostgreSQL Checkpointer 运行层，拆分 migration/runtime 最小权限账号与显式初始化流程，建立每 App 连接池复用、readiness 和可关闭生命周期；通过 exact checkpoint locator、parent lineage 与 fenced CAS 将 saver head 区分为 candidate/accepted，真实验证 half-write、stale fence 和 latest orphan 均不能覆盖已接受恢复点，保持完整 API 502 项回归通过。

使用边界：该表述只说明“官方 Saver 生命周期与精确 head 适配器已验证”；T33 的 lease、advisory lock、heartbeat、接管编排以及生产图的跨进程恢复尚未实现。默认入口仍为 Legacy，正式简历仍需用户另行确认后才修改。

## T30 Verified 简历候选表述

> 为 AccessPilot 设计 26 字段严格 GraphState、Runtime Context 与 22 节点生产 LangGraph 拓扑，在 invoke 前和节点 update 返回前建立双重校验，避免 LangGraph 将非法输入/更新先写入 checkpoint；通过真实 PostgresSaver 对 JSONB、BYTEA、pending writes 与 decoded state 递归扫描，验证服务对象、身份与敏感 canary 零落盘，并保持完整 API 559 项回归通过。

使用边界：该表述只说明“安全 State、完整拓扑骨架和真实序列化边界已验证”；T30 节点仍是无副作用 stub，T31 的路由/RAG、T32 的解析/CAS、T34 的 interrupt/resume 以及生产 API 切流尚未实现。正式简历仍需用户另行确认后才修改。

## T31 Verified 简历候选表述

> 将 AccessPilot 的确定性路由、只读工具与政策 pgvector RAG 拆入真实 LangGraph 多节点链路，固定 generic search 才走 RAG、目录/自审批/权限解析不走 RAG，并以 14 个场景连续两轮验证 route/outcome 28/28 全部一致；补充 6 项 fresh-runtime 节点级反证，证明下游只依赖可持久化的脱敏 GraphState、不依赖进程内 scratch，完整 API 594 项回归通过。

使用边界：该表述只覆盖 T31 已实现的只读图分支与节点级重放，不代表 PostgreSQL checkpoint resume 或进程重启已经验证；合法期限消费、模型解析、草稿 CAS 属于 T32，崩溃接管/恢复属于 T33/T37，申请人 interrupt/resume 属于 T34，生产 JSON/SSE 仍未切入 LangGraph。正式简历仍需用户另行确认后才修改。

## T32 Verified 简历候选表述

> 将 AccessPilot 的申请收集主链（模型解析、权限解析、草稿 CAS、字段校验）迁入真实 LangGraph 节点，以稳定 operation ledger 实现 quota/草稿/Cursor 的应用级幂等，并让第二次模型调用携带纠正提示、14 个正常场景连续两轮 path+outcome 28/28 与 Legacy 一致；补充 fenced finalizer 事务（Cursor 五列与 ledger 同事务、重放同一行、stale fence/过期租约零写入）。独立验收 P0=0、P1=0，T32 定向 52 项、Agent 相关 221 项通过（完整 API 646 项为此前历史证据，本轮未重跑）。

使用边界：该表述只覆盖 T32 已实现的申请收集/草稿 CAS/步骤幂等；execution 生命周期、lease、崩溃接管属于 T33，申请人 interrupt/resume 属于 T34，真实轨迹事件与生产 JSON/SSE 切流尚未实现。正式简历仍需用户另行确认后才修改。

## T33 Verified 简历候选表述

> 为 AccessPilot 建立可恢复的 LangGraph turn 执行内核：应用自有 begin-input 原子分配 `graph_run/input_seq/turn/fence`，不可伪造的 PostgreSQL advisory-lock ownership 连续覆盖 takeover、graph run 与 finalize；lease 过期接管复用原逻辑输入并递增 attempt/fence，任何已接纳的 accepted checkpoint head（含同 graph_run 历史）都禁止回退 input_event；以真实 PostgresSaver + 小型 StateGraph 验证 exact/END/interrupt head、missing head fail-closed、禁止 implicit latest，并补齐 stale owner 的 step/head/terminal 三类零写入反证。独立复核 P0=0、P1=0，T33 定向 23 项、Agent 235 项、隔离 PG 9 项通过。

使用边界：该表述只覆盖 T33 的 lease/fence/advisory lock/崩溃接管内核；T34 申请人 interrupt/resume 业务语义、T35 粘性切流、T36 轨迹事件、T37 六点故障注入以及生产 JSON/SSE 切流尚未实现。正式简历仍需用户另行确认后才修改。

## 固定参考

- [v1.3 Canonical Spec](../specs/accesspilot-langgraph-agent-loop-v1.3.md)
- [v1.3 T26–T42 Tickets](../tickets/accesspilot-langgraph-agent-loop-v1.3.md)
- [v1.3 双评审裁决](../reviews/accesspilot-v1.3-spec-review-decisions-2026-08-16.md)
- [v1.2 Claim Ledger](accesspilot-v1.2-claim-ledger.md)
