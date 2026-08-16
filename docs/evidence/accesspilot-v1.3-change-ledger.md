# AccessPilot v1.2 → v1.3 改造与简历价值账本

**用途：** 每完成一张 Ticket，同步记录技术改变、解决的问题、验证证据与可面试价值。
**v1.2 基线：** 顺序执行的 `ConversationService` + 业务 Cursor；仅政策问答使用 pgvector RAG；当前生产入口未由 LangGraph 编排。
**v1.3 目标：** 真实 LangGraph Agent Loop + PostgreSQL 持久恢复 + HITL interrupt/resume + 只读真实轨迹。
**当前总状态：** `In progress`；T26–T27 `Verified`，T28 为下一张。正式简历仍未修改，也不得将后续未完成的主链、HITL 或轨迹能力当作已实现成果。

## 状态口径

- `Planned`：只有 Spec/Ticket，不得宣称已实现。
- `Implemented`：代码与定向测试已落地，但还没有完成独立验收。
- `Verified`：同一 revision 的测试、运行与独立验收都有可回溯证据，才能生成简历候选表述。

## 滚动改造记录

| Ticket | 技术改变 | 解决的问题 | 简历 / 面试价值 | 证据链接 | 状态 / 限制 |
|---|---|---|---|---|---|
| T26 | 锁定 LangGraph / PostgresSaver / psycopg 矩阵；用真实 PostgreSQL 实测修正为每 run 独立 checkpoint thread 和 candidate→accepted 两阶段 head | 提前暴露 root namespace、半写入 interrupt、精确 head 恢复与跨平台依赖漂移风险 | 可讲“先做兼容性 Spike，用运行证据修正架构合同，再工程化接入”，而不是只堆框架名 | [T26 验收证据](accesspilot-v1.3-t26-acceptance-2026-08-17.md)、[双评审决策](../reviews/accesspilot-v1.3-t26-acceptance-decisions-2026-08-17.md)、[Probe](../../scripts/t26_langgraph_compat.py)、[测试](../../apps/api/tests/agent/test_t26_langgraph_compat.py)、[R-29/R-30 裁决](../reviews/accesspilot-v1.3-spec-review-decisions-2026-08-16.md#2-议题清单) | `Verified`；T26-only 快照中 PostgreSQL probe/9 项测试、Python 3.11 新环境、Python 3.12 Docker、后端 451 项/旧前端 95 项与全量评测均通过；Claude Code + DeepSeek 均无 P0/P1；生产 saver/fence 与主链仍属后续 Ticket |
| T27 | 抽象 `ConversationOrchestrator`，让 JSON/SSE 只依赖可注入边界；用 engine-owned success finalizer 保持 terminal→Cursor→emit 顺序，并冻结 Legacy 黄金样本 | 在不改变对外行为的前提下建立可替换内核，避免接口层继续理解 Legacy Cursor 状态机 | 可讲 Strangler 迁移、依赖注入、契约测试、独立验收发现边界泄漏后的闭环修复与一键回滚 | [T27 验收证据](accesspilot-v1.3-t27-acceptance-2026-08-17.md)、[实现](../../apps/api/src/accesspilot/conversation.py)、[黄金测试](../../apps/api/tests/conversation/test_t27_orchestrator.py) | `Verified`；定向 27 项、相关 151 项、API 467 项通过，另有 1 项 T26 专用数据库 probe 因未配置变量跳过；独立验收无 P0/P1；默认仍 100% Legacy，生产 LangGraph 尚未接入 |
| T28 | 增加 thread、execution、pending、fence、幂等与事件身份事实 | 为可恢复执行、去重和引擎粘性建立可查询的数据不变量 | 可深入讲幂等键、状态机约束、迁移和历史数据兼容 | [T28 计划](../tickets/accesspilot-langgraph-agent-loop-v1.3.md#t28--应用-schema运行事实与确定性身份) | `Planned`；数据库尚未迁移 |
| T29 | 接入官方 PostgreSQL checkpointer，分离 migration/runtime 账号与连接周期 | 避免请求期 DDL、连接泄漏和 stale checkpoint 恢复 | 可讲解持久化 Agent 的运行责任、最小权限和生命周期 | [T29 计划](../tickets/accesspilot-langgraph-agent-loop-v1.3.md#t29--postgresql-checkpointer-初始化账号与运行生命周期) | `Planned`；不得宣称已可跨进程恢复 |
| T30 | 建立类型化 GraphState、Runtime Context 与真实多节点拓扑 | 防止将凭证、授权事实或不必要内容写入 checkpoint | 可讲解 Agent State 与业务权威源的边界、类型化图设计 | [T30 计划](../tickets/accesspilot-langgraph-agent-loop-v1.3.md#t30--安全-graphstateruntime-context-与生产拓扑骨架) | `Planned`；现有 LangGraph 仅为测试 Demo |
| T31 | 将确定性路由、只读工具和政策 pgvector RAG 迁入图节点 | 让真实执行路径可观察，同时防止把目录查询伪称为 RAG | 可讲解路由优先级、RAG 三态和确定性/模型边界 | [T31 计划](../tickets/accesspilot-langgraph-agent-loop-v1.3.md#t31--确定性路由只读工具与政策-rag-图分支) | `Planned`；当前只有原有政策 RAG |
| T32 | 将申请解析、权限解析、草稿 CAS 与步骤幂等迁入图 | 防止重试时重复扣 quota、覆盖新草稿或重复提交本地副作用 | 可讲解 CAS、operation ledger 与 at-least-once 执行下的本地 exactly-once 效果 | [T32 计划](../tickets/accesspilot-langgraph-agent-loop-v1.3.md#t32--申请收集模型解析草稿-cas-与步骤幂等) | `Planned`；尚未验证重放幂等 |
| T33 | 建立 turn lease、fence、advisory lock 与精确 checkpoint head 接管 | 防止同 thread 并发双写、旧 executor 续写和崩溃后丢输入 | 可深入讲分布式 lease/fencing、并发一致性和故障接管 | [T33 计划](../tickets/accesspilot-langgraph-agent-loop-v1.3.md#t33--turn-execution-leasefence-与崩溃接管内核) | `Planned`；尚无崩溃恢复证据 |
| T34 | 用 LangGraph interrupt/resume 实现持久的申请人确认 | 关闭 checkpoint、pending、Cursor 和 terminal 之间的丢消息窗口 | 可讲解 Human-in-the-loop、跨进程恢复与非 2PC 一致性取舍 | [T34 计划](../tickets/accesspilot-langgraph-agent-loop-v1.3.md#t34--申请人确认-interruptresume-与原子投影) | `Planned`；当前确认仍依赖业务 Cursor |
| T35 | 实现 Workspace 粘性引擎、pending/checkpoint 对账与 Legacy 降级桥 | 防止 JSON/SSE 将同一 Workspace 分流到不同引擎，保证可控回滚 | 可讲解粘性切流、双向对账门禁和不破坏降级 | [T35 计划](../tickets/accesspilot-langgraph-agent-loop-v1.3.md#t35--workspace-sticky-enginepending-对账与-legacy-降级桥) | `Planned`；切流前仍必须 Legacy |
| T36 | 从真实 node/model/RAG/tool 边界产生脱敏、可去重轨迹事件 | 替代事后根据 `tool.summary` 合成轨迹，且不泄露 prompt/凭证/隐藏推理 | 可讲解 Agent observability 事件合同、脱敏与重放去重 | [T36 计划](../tickets/accesspilot-langgraph-agent-loop-v1.3.md#t36--真实安全可去重的轨迹事件后端) | `Planned`；当前 UI 只是 Legacy 事件原型 |
| T37 | 增加六个仅测试可用的 fault hook 与崩溃恢复验证 | 证明 checkpoint/应用事务非原子窗口不丢输入、不重复本地副作用 | 可讲解故障注入、恢复不变量和 Provider at-least-once 边界 | [T37 计划](../tickets/accesspilot-langgraph-agent-loop-v1.3.md#t37--六个崩溃边界的幂等恢复) | `Planned`；不得宣称生产高可用 |
| T38 | 在隔离注入下让真实 JSON 入口调用生产图 | 验证图不只在单测中运行，且不提前创建半切流 Workspace | 可讲解分层集成门禁和 API 合同兼容 | [T38 计划](../tickets/accesspilot-langgraph-agent-loop-v1.3.md#t38--json-生产图隔离入口门禁) | `Planned`；完成也不等于已 canary |
| T39 | 完成顶部「对话 / 轨迹」只读切换与最近三轮展示 | 让用户理解一次 Agent Loop，不赋予恢复、重放或执行权限 | 可讲解“可解释性产品化”、可访问性与只读安全边界 | [T39 计划](../tickets/accesspilot-langgraph-agent-loop-v1.3.md#t39--对话--轨迹只读-ui-与最近三轮) | `Planned`；现有前端原型不算真实轨迹证据 |
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

## 固定参考

- [v1.3 Canonical Spec](../specs/accesspilot-langgraph-agent-loop-v1.3.md)
- [v1.3 T26–T42 Tickets](../tickets/accesspilot-langgraph-agent-loop-v1.3.md)
- [v1.3 双评审裁决](../reviews/accesspilot-v1.3-spec-review-decisions-2026-08-16.md)
- [v1.2 Claim Ledger](accesspilot-v1.2-claim-ledger.md)
