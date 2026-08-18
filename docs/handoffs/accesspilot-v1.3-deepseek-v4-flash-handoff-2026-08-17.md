# AccessPilot v1.3 → DeepSeek V4 Flash 开发交接包

**日期：** 2026-08-17  
**仓库：** `/Users/mie/my-chatgpt/简历/accesspilot`  
**分支：** `feature/accesspilot-mvp`  
**当前 HEAD：** `7ad8a011a74f1358e97a602d12ba1e6a53cd36cd`（T31）  
**执行方式：** 严格串行，一次只实现一张 Ticket；通过独立验收后只做本地提交  
**禁止：** push、merge、deploy、修改正式简历、删除 Legacy/checkpoint、夹带用户已有脏文件

## 1. 给 DeepSeek 的直接指令

你接手的是 AccessPilot v1.3 的真实 LangGraph Agent Loop 改造。先完整阅读本交接包以及 Canonical Spec/Tickets，不要根据框架常识猜合同，不要为了测试通过而缩小验收范围。

执行顺序：

1. 先完成 T32 收口：更新状态与证据、只暂存指定文件、创建本地提交。
2. 再从 T33 开始，严格按 `T33 → T34 → … → T42` 串行推进。
3. 每张 Ticket 先写失败测试，再做最小实现；跑定向测试和相关回归。
4. 独立验收通过前只能写 `In progress`，不得写 `Verified`，不得提前写简历成果。
5. 每张 Ticket 本地提交后停止，输出验收包，等待下一步确认。
6. 发现 Spec/AC、权威源或副作用语义冲突时立即停止并回流 Spec，不得通过改弱 Ticket 验收口径绕过。

### 1.1 每张 Ticket 必须附带的防 P0/P1 调整指令

以下内容是项目级强制门禁，不是建议。每次把一张新 Ticket 交给 DeepSeek 时，将本段原样附在任务末尾：

```text
你是实现者，不是自己的最终验收者。执行本 Ticket 时必须遵守：

1. 开工前冻结验收清单：逐条列出 AC、全部正常/异常/竞态场景、预期真实路径、预期调用参数、预期副作用和明确不应发生的副作用。实现期间不得自行减少场景、缩小分母、改弱 AC 或把特殊场景移出统计口径。
2. 如果发现现有 Spec/Ticket 不可实现或互相冲突，立即停止并报告；不得边改验收定义边宣布通过。
3. 先保存可信红灯：测试必须因目标能力缺失而失败。没有红灯证据，不得开始实现；环境/密码/数据库不可用造成的失败不算红灯。
4. 测试必须证明机制，不只证明最终结果：
   - LangGraph 分支采集真实 compiled stream 并逐节点比较 expected path；
   - 模型/工具断言准确调用次数、顺序和完整安全参数；
   - 数据写入断言 transaction、revision、operation、fence、lease、Cursor、terminal 和正式业务表；
   - 错误分支断言“没有发生什么”，例如无二次模型调用、无 RAG、无 pending、无重复 quota/revision/event。
5. 所有冻结场景必须进入显式 manifest。矩阵外的 race、numeric、resume、断线或异常场景仍计入总场景数，不能只测 outcome 后从 path 分母中删掉。
6. 每个关键断言做一次反事实检查：如果把 CORRECTION_PROMPT 改回 None、删掉一个节点、改读 implicit latest、去掉 fence 校验或重复写一次，本测试是否一定失败？如果不会，测试不合格。
7. 并发/恢复/数据库合同不能用 mock 自证。需要 PostgreSQL、双连接、跨 App 或跨进程的 AC，必须使用真实隔离 PostgreSQL，并断言精确持久事实。
8. P0 安全边界：禁止 checkpoint/event/ledger 出现 prompt、Provider 原始输出、SQL、向量、Cookie、Token、CSRF、Key、隐藏推理；禁止客户端提供 thread/run/head/fence/engine 权威值；禁止 implicit latest 恢复。
9. 本地 exactly-once 与 Provider at-least-once 必须分开描述。quota/operation completed 不能伪装成 Provider 已成功返回。
10. 独立 reviewer 明确给出 P0=0、P1=0 前，状态只能是 In progress；不得写 Verified、不得创建正式简历 Claim、不得提前开始下一张 Ticket。
11. 测试数字只能写本轮真实命令输出。历史结果必须标成历史，skip 必须列出原因；不得用计划值、目标值或旧 revision 数字冒充本轮实测。
12. 工作树按文件 allowlist 暂存。任何用户已有脏文件、未验证 UI 原型或后续 Ticket 文件都不得夹带。

交付前输出一张映射表：AC → 红灯测试 → 实现入口 → 绿灯证据 → 负面断言 → 未实现边界。任一格为空，Ticket 不得提交。
```

### 1.2 T32 两轮问题转化出的验收模式

| T32 暴露的问题 | 后续强制预防规则 |
|---|---|
| 只比较最终 outcome，节点走错也可能全绿 | 每个冻结场景同时保存真实路径与结果；路径必须来自 compiled stream，不得来自常量自报 |
| 模型调用两次，却没证明第二次传了纠正提示 | 对模型/工具保存调用记录，断言次数、顺序和精确参数；同时锁死“不应重试”的异常 |
| 12 个矩阵场景补了路径，但 race/numeric 被遗漏 | 开工前建立唯一场景 manifest；所有专项用例也必须进入总分母和证据表 |
| 通过把“14 个”改成“12 个”消除 P1 | 验收口径冻结后只有回流 Spec 才能改；实现者不得在修复 P1 时编辑分母 |
| 独立复核前提前写 Verified/P0-P2=0 | 实现者只能写 Implemented/In progress；Verified 由独立 reviewer 结论触发 |
| 环境连接失败产生大量红灯，容易误判代码 | 先区分环境失败与行为失败；只有命中目标断言的失败才是 TDD 红灯 |
| 测试数字与不同轮次证据混用 | 每轮记录命令、revision、时间和结果；未重跑的全量测试明确标为历史证据 |

DeepSeek 每次修复评审问题时，只允许修改实现/测试来满足已冻结 AC；若确实需要改变 AC，必须停止并提交 Spec 修订建议，不能在同一轮自行批准。

## 2. 当前真实状态

| Ticket | 状态 | 本地提交 |
|---|---|---|
| T26 | Verified | `90667e4` |
| T27 | Verified | `db2ebf2` |
| T28 | Verified | `0553cad` |
| T29 | Verified | `76676a7` |
| T30 | Verified | `9df3a00` |
| T31 | Verified | `7ad8a01` |
| T32 | 独立复核已 PASS，尚未改为 Verified/提交 | 未提交工作区 |
| T33–T42 | 尚未开始 | 无 |

T32 最新独立复核：

- P0=0、P1=0；首轮与二轮发现的路径/纠正参数问题均已闭合。
- 14 个正常场景连续两轮都验证 compiled-stream 路径与 Legacy outcome：`28/28`。
- 第二次模型调用锁死为 `[None, CORRECTION_PROMPT]`；HTTP/超时不做纠正重试。
- T32 定向：`52 passed`。
- Agent 相关回归：`221 passed, 1 skipped`；skip 是 T26 destructive-isolated PostgreSQL probe 的环境门禁。
- Ruff、MyPy、`git diff --check`：通过。
- 本轮最后修复只改测试/证据，未重跑完整 API；现有证据记录此前完整 API 为 `646 passed, 3 skipped`，不得把它说成本轮重新实测。

仍需保留的非阻断风险：

- T32 写路径现有锁序是 `execution → step → workspace`。T33 不得引入与其反向的 `workspace → execution` 等待链；必须用双连接和短 `lock_timeout` 证明无死锁。
- draft/numeric/finalizer 已共享事务模板，但并发与故障注入证据仍可在 T33/T37 加强。
- concurrent logout 的授权生效时点尚需在 T33/T38 明确，不能静默猜为请求开始或提交时刻。
- Provider 允许 at-least-once；`quota_consumed` 只代表本地额度事实，不代表 Provider 已成功返回。

## 3. 当前能力边界（禁止夸大）

- 生产 JSON/SSE 默认仍走 Legacy `ConversationService`；LangGraph 尚未成为产品主入口。
- T31 已把确定性路由、只读工具和政策 pgvector RAG 三态迁入真实多节点图。
- T32 已把申请解析、权限解析、草稿 CAS、合法 numeric Cursor 与本地步骤幂等迁入图。
- 尚无完整 execution lease/heartbeat/takeover、真实 interrupt/resume、六点崩溃恢复、LangGraph 新事件后端或真实 flow 2 canary。
- 当前未提交的 `AgentTrajectory` 是 Legacy 事件原型，不是 T39 完成证据。
- 不存在 ReAct、Multi-Agent 业务主链、真实 SSO/IAM、SLA、Token 成本平台或完整 OpenTelemetry 证据。

## 4. 立即执行：T32 收口与本地提交

先把以下文档从 `In progress（待复核）` 更新为 `Verified`，写清本轮独立证据：T32 `52 passed`、Agent `221 passed, 1 skipped`、Ruff/MyPy/diff-check 通过；不要把本轮未重跑的完整 API 写成“本轮独立实测”。当前停点改为 T33。

T32 应纳入本地提交的文件只有：

- `apps/api/src/accesspilot/agent/production_graph.py`
- `apps/api/src/accesspilot/agent/step_operations.py`
- `apps/api/src/accesspilot/conversation.py`
- `apps/api/tests/agent/test_t30_graph_state.py`
- `apps/api/tests/agent/test_t31_readonly_graph.py`
- `apps/api/tests/agent/test_t32_request_graph.py`
- `apps/api/tests/agent/test_t32_step_operations.py`
- `docs/specs/accesspilot-langgraph-agent-loop-v1.3.md`
- `docs/tickets/accesspilot-langgraph-agent-loop-v1.3.md`
- `docs/evidence/accesspilot-v1.3-change-ledger.md`
- `docs/evidence/accesspilot-v1.3-t32-acceptance-2026-08-17.md`
- 本交接文件可单独保留或随 T32 文档提交，但不得替代验收证据。

严禁暂存这些用户既有内容：

- `.gitignore`
- `README.md`
- `.codebase-map/`
- `scripts/start-local.sh`
- `apps/web/src/App.tsx`
- `apps/web/src/App.test.tsx`
- `apps/web/src/styles.css`
- `apps/web/src/AgentTrajectory.tsx`
- `apps/web/src/AgentTrajectory.test.tsx`

提交前再次用 `git diff --cached --name-only` 核对范围。本地提交即可，不 push。

## 5. 剩余 10 张 Ticket

| Ticket | 目标 | 关键风险/停点 |
|---|---|---|
| T33 | Turn execution lease、fence、advisory lock、精确 checkpoint head、崩溃接管 | stale owner、反序死锁、implicit latest、把客户端重试当接管 |
| T34 | 申请人确认 interrupt/resume 与 pending/Cursor/terminal 原子投影 | Saver 与应用事务非 2PC、resume 输入提交后崩溃、重复确认 |
| T35 | Workspace sticky engine、pending/checkpoint 对账、Legacy 降级桥 | JSON/SSE 分裂引擎、错误 tombstone、孤立 accepted head |
| T36 | 真实 node/model/RAG/tool 脱敏轨迹事件后端 | 泄露 prompt、Provider 输出、SQL、向量、凭证或隐藏推理 |
| T37 | 六个崩溃边界的幂等恢复 | 输入丢失、quota/revision/event/terminal 重复、测试 hook 泄露 |
| T38 | JSON API 隔离入口真正调用生产图 | 把隔离测试误当真实 canary、并发双进图 |
| T39 | 顶部「对话 / 轨迹」只读 UI，最近三轮 | 前端猜节点、误做控制台、复用 Legacy 原型冒充真实事件 |
| T40 | SSE LangGraph 门禁、传输合同、sticky flow 2 canary | 断线提前释放 lease、debug stream 泄露、endpoint 分裂 |
| T41 | 全量质量、浏览器、恢复、回滚演练 | 调整分母、复用旧指标、跳过 v1.2 安全回归 |
| T42 | Claim Ledger、限制披露、简历证据包 | 把 Mock/框架接入夸大成生产规模；不得直接修改正式简历 |

权威验收标准以 Tickets 文件为准，不以本表替代。

## 6. T33 开工重点

T33 是高风险并发/恢复 Ticket。开工前必须重读 Spec §6.2、§7 和 T33 全部三条 AC，并先形成红灯测试。

必须守住：

1. begin-input 原子分配 `graph_run/input_seq/turn/fence`，写 execution、`turn.started`、清洗后的 `message.user`；第二条 running 输入在产生用户事实前返回稳定 409。
2. graph invoke 全程受 thread advisory lock 保护；heartbeat 和所有应用写都校验 execution/fence/lease。
3. 精确 checkpoint 坐标是 `checkpoint_thread_id + checkpoint_ns("") + checkpoint_id`；禁止读 implicit latest。
4. candidate 只有图停止并验证精确状态后才能提升 accepted head。
5. takeover 复用原 `graph_run/input_seq/input_turn_id/input_event_id`，只递增 attempt/fence；不得把新 HTTP body 当恢复输入。
6. execution 或其关联 pending/run 曾接纳 head 时，只能从精确 accepted head 恢复；三者从未接纳 head 才能从安全 `input_event_id` 重建。
7. 新输入遇恢复中 execution 固定返回 `TURN_RECOVERY_IN_PROGRESS`。
8. T33 不实现 T34 interrupt 业务语义，不接 JSON/SSE 生产入口，不生成 T36 新轨迹事件。

必须增加至少两类反证：

- 两连接 stale-owner/takeover，证明旧 owner 在新 fence 后的业务写/head CAS 全失败。
- 与 T32 写路径同时运行的锁序测试，短 `lock_timeout` 下无反序死锁。

## 7. 每张 Ticket 的固定循环

1. 核对 Ticket 依赖和当前 HEAD/脏文件。
2. 写失败测试并保存首个可信红灯。
3. 做最小实现，不提前实现后续 Ticket。
4. 跑本 Ticket 定向测试。
5. 跑受影响模块的相关回归、Ruff、MyPy、`git diff --check`。
6. 只有关键节点才跑全量：建议 T34、T37、T40、T41；普通 Ticket 不重复全量以节省时间与 Token。
7. 交给独立 reviewer 只读验收；P0/P1 未清零不得标 `Verified`。
8. 验收通过后更新 Spec/Tickets/change ledger/evidence，并只做一个本地提交。
9. 输出提交 hash、文件清单、测试数字、未完成边界；停止等待下一张。

## 8. 本机测试环境

不要打印 `.env` 内容或任何密码。测试时可在 shell 内加载现有变量并构造测试库 URL：

```bash
set -a
source .env
set +a
export ACCESSPILOT_TEST_DATABASE_URL="postgresql+psycopg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:55432/accesspilot_test"
export LANGGRAPH_STRICT_MSGPACK=true
```

T32 快速门禁示例：

```bash
.venv/bin/pytest \
  apps/api/tests/agent/test_t32_step_operations.py \
  apps/api/tests/agent/test_t32_request_graph.py -q

.venv/bin/pytest apps/api/tests/agent -q

.venv/bin/ruff check apps/api/src apps/api/tests
.venv/bin/mypy apps/api/src
git diff --check
```

真实 PostgreSQL destructive-isolated probe 必须使用其专用环境变量和 disposable DB；不得指向 demo/业务数据库。不要运行 bootstrap 作为普通测试前置，因为它可能写目录/向量并在有 Provider key 时发起外部调用。

## 9. 发生以下情况必须停止

- 需要改变 Canonical Spec、权威源、副作用语义、恢复身份或 AC。
- 测试只能通过降低场景数、删除断言、改小分母或把目标值写成实测值。
- 需要覆盖用户未提交的 UI/README/.gitignore/start-local 变更。
- 需要 push、merge、deploy、删除 Legacy/checkpoint 或修改正式简历。
- 需要打印/传递 `.env`、凭证、Cookie、CSRF、Provider key 或原始 prompt/output。
- 无法用真实 PostgreSQL 证明并发、checkpoint 或恢复语义。

停止时只报告：阻塞证据、影响的 AC、最小 Spec/Ticket 修正建议，不猜实现。

## 10. 每张 Ticket 的回报格式

```text
Ticket: Txx
Status: Implemented / Verified / Blocked
AC1: PASS/FAIL + 证据
AC2: PASS/FAIL + 证据
AC3: PASS/FAIL + 证据
Tests: 定向 / 相关 / 全量（如有）
Files: 精确文件清单
Commit: 本地 hash 或“未提交”
P0/P1/P2: 数量与边界
Not implemented: 后续能力与禁止夸大的内容
Next stop: 下一张 Ticket；不自动开始
```

## 11. 权威文件

- `docs/specs/accesspilot-langgraph-agent-loop-v1.3.md`
- `docs/tickets/accesspilot-langgraph-agent-loop-v1.3.md`
- `docs/reviews/accesspilot-v1.3-spec-review-decisions-2026-08-16.md`
- `docs/evidence/accesspilot-v1.3-change-ledger.md`
- `docs/evidence/accesspilot-v1.3-t32-acceptance-2026-08-17.md`
- `apps/api/src/accesspilot/agent/production_graph.py`
- `apps/api/src/accesspilot/agent/checkpoint.py`
- `apps/api/src/accesspilot/agent/state.py`
- `apps/api/src/accesspilot/agent/step_operations.py`

先执行 T32 收口，不要直接跳到 T33。
