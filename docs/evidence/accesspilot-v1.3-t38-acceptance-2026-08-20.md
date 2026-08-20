# AccessPilot v1.3 T38 验收证据包（Verified）

**Ticket：** T38 — JSON 生产图隔离入口门禁
**实现基线：** `e1e47cc`（T37）；T38 已本地提交 `0aa953c`
**日期：** 2026-08-20
**状态：** `Verified`；首轮独立验收 P0=0、P1=1、P2=0，回修后定向复验 P0/P1/P2=0

## 1. 范围与边界

本 Ticket 只让测试显式注入的真实 JSON API 入口调用生产 `CompiledStateGraph`，用于验证生产图、应用事实和官方 PostgresSaver 的跨 runtime 合同。

- 默认 `create_app` 继续选择 Legacy orchestrator；客户端 payload、settings、Workspace flow 或 UI 都不能选择该测试适配器；
- SSE `prepare()` 固定失败闭合，T40 前不建立 JSON 已切、SSE 未切的产品状态；
- 测试不创建持久 `flow_version=2` Workspace，mixed canary 仍为 0；
- T38 不构成真实 canary、生产切流、UI 完成或完整产品验收 Claim。

## 2. 三条验收标准

### AC1 — 真实 JSON 路由进入生产图且默认仍 Legacy

`LangGraphConversationOrchestrator` 复用 `TurnExecutionService`、`FencedGraphTurnRunner`、`FencedPostgresSaverAdapter` 和 `build_production_graph`，没有复制生产节点状态机。真实 `POST /api/chat/messages` 断言完整 compiled node path；七个只读黄金场景的规范化 DTO 与 Legacy 逐字段一致。

客户端注入 identity、engine、flow 或 thread 坐标会得到安全失败且不创建 execution；模型 quota 用稳定 429 与唯一 recoverable terminal 保持 JSON 合同。默认应用仍为 Legacy，SSE 方法固定 503 失败闭合。

### AC2 — App A/B 跨 runtime 恢复与消息不丢

隔离测试为每组场景创建、迁移和销毁专用 PostgreSQL 数据库，并使用官方 `PostgresSaver`：

- App A 通过真实 HTTP 运行至确认 interrupt，关闭 runtime 后由全新 App B、同 thread/run 完成 confirm 或非确认重路由；
- `AFTER_CONFIRMATION_CAS_BEFORE_TERMINAL` 和 `AFTER_NON_CONFIRM_RESUME_CONSUMED` 两个恢复窗口在 lease 过期后由 App B 接管原 HTTP turn；
- quota 耗尽的非确认输入在 `Command(resume)` 前做纯路由/quota 预检，429 不创建 execution、不消费 exact head，pending 保持 active，后续确认可正常恢复；
- 恢复保持原 `graph_run_id/input_seq/input_turn_id`，接管只递增 attempt/fence，安全用户消息不丢。

### AC3 — 并发、身份与唯一终态失败闭合

同 Workspace 并发 invoke/resume 只有一个请求进入图；错身份、revision conflict 和缺失 exact pending head 均失败闭合。每个已接受 HTTP turn 恰有一个 terminal，真实 PostgreSQL 场景在数据库销毁前断言 `flow_version=2` Workspace 数为 0。

`begin_input` 在 Workspace 行锁内、任何 fence/execution/event/message 写入前复核 `active/resuming` pending。迟到 new-input 遇到已发布 pending 时稳定返回 409 且零副作用，客户端重试后由 orchestrator 进入同 run resume。

## 3. 独立验收发现与回修

首轮独立验收发现 1 项可达 P1：请求 B 可在无锁读取到“无 execution/pending”后暂停；请求 A 随后通过真实 HTTP 发布 active pending；B 再调用 `begin_input` 时，旧实现只在 Workspace 行锁内复核 running execution，没有复核 live pending，因此会错误创建第二个 running turn。该 turn 在发布第二个 pending 时返回 503，lease 接管后仍不能闭合，违反 AC2/AC3。

回修先加入精确调度测试并得到可信 `1 failed`，再把 live-pending 守卫放入 `begin_input` 已持有的 Workspace 行锁事务，并置于所有写入之前。修复后：

- A interrupt 返回 200；迟到 B 返回 409；
- B 前后的 execution、event、`message.user` 数量完全不变；A pending 保持 active；
- B 重试返回 200、复用同一 graph run，pending 最终 resolved；
- 两个已接受 HTTP turn 的 terminal 数量均为 1。

独立验收只复核该未关闭问题及其受影响回归，确认 P0/P1/P2=0。

## 4. 实测证据

主实现阶段：

- T38 定向：`22 passed`，其中官方 PostgresSaver + 新 runtime App A/B：`4 passed`；
- T27 黄金合同、Auth/Workspace JSON、engine binding：`65 passed`；
- T33–T37 Agent 回归：`77 passed`；
- T33–T36 真实 PostgreSQL 回归：`33 passed`；
- Ruff（全部 API src/tests）、MyPy（58 个源码文件）、`git diff --check`：通过。

P1 回修阶段：

- 精确调度红灯/转绿：`1 failed` → `1 passed`；
- T38 定向：`23 passed`；
- T33/T35/T37 TurnExecution、rollback、recovery 受影响回归：`48 passed`；
- Ruff、MyPy（58 个源码文件）、`git diff --check`：通过。

独立定向复验：

- 精确真实 HTTP 调度：通过，409 零副作用且重试同 run resume；
- T38 定向：`23 passed in 6.94s`，含真实 PostgresSaver App A/B 4 项；
- Ruff、MyPy（58 个源码文件）、`git diff --check`：通过；
- skip/blocker：0。

所有 LangGraph 测试均设置 `LANGGRAPH_STRICT_MSGPACK=true`。首次独立运行中的 localhost 沙箱限制、未加载数据库凭证及 Compose 内部主机名只发生在连接/fixture 阶段；按仓库 `.env` 安全加载并使用本机映射端口后无 skip/blocker，未把环境阻断记成代码测试失败或通过。未运行完整 API 全量测试，符合当前 Ticket 的定向与受影响回归边界。

## 5. 修改文件

- `apps/api/src/accesspilot/agent/json_orchestrator.py`
- `apps/api/src/accesspilot/agent/turn_execution.py`
- `apps/api/src/accesspilot/conversation.py`
- `apps/api/src/accesspilot/main.py`
- `apps/api/tests/api/test_t38_json_langgraph.py`
- 本证据包与 v1.3 Spec、Ticket、Change Ledger 当前状态记录

## 6. 当前停点

T38 已 `Verified` 并按用户授权本地提交 `0aa953c`。当前按已确认的 Ticket 顺序进入 T39；未授权推送、合并、部署或修改正式简历。
