# AccessPilot v1.3 T34 验收证据包（Implemented，待独立验收）

**Ticket：** T34 — 申请人确认 Interrupt/Resume 与原子投影
**实现基线 revision：** `1a493eb`（T34 服务原语）；本包为图接入与恢复语义实现
**日期：** 2026-08-18
**状态：** `Implemented`（实现与定向测试已落地；尚未完成独立只读验收，未标记 `Verified`）

## 1. 范围

在 `1a493eb` 已实现的服务原语（`agent.input.required` 事件合同、
`finalize_interrupt` / `begin_resume` / `confirm_draft`）之上，完成：

1. 生产图真实 `interrupt()` / 单次 `Command(resume)` 接线（`production_graph.py`）；
2. `rehydrate_resume_snapshot` / `apply_confirmation_cas` 真实实现；
3. resume 终态的原子关闭（`finalize_resume_outcome`）与再次 interrupt 的
   pending 原子替换（`finalize_interrupt(previous_pending_input_id=...)`）；
4. fenced saver 对 resume 派生 head 的 writes 放行（`checkpoint.py`）；
5. 受影响回归更新：T32 parity 的 await 边界断言、T30 拓扑断言。

## 2. 三条 AC 的证据

### AC1 — 安全、可序列化的 confirmation interrupt，节点零副作用

- 图级测试 `apps/api/tests/agent/test_t34_graph_interrupt.py`：
  `test_complete_draft_stops_at_confirmation_interrupt_with_safe_payload`
  - invoke 停在 `ConfirmationInterruptRaised`（`__interrupt__` 检测）；
  - payload 含 `kind=confirmation`、`pending_input_id`、`draft_revision`、
    摘要与 `allowed_decisions`，`json.dumps(payload)` 通过；
  - interrupt 节点零写：Request/Approval/Grant/Pending/step 计数不变，
    事件数不变，`model_calls == 1`（仅上游 T32 parse），quota 无额外消耗，
    `draft.confirmed` 保持 False；
- 真实 PG `apps/api/tests/db/test_t34_postgres.py` 的 atomic 测试断言
  interrupt 后（调用层 finalize 前）pending=0、事件=2（仅
  `turn.started`+`message.user`）。

### AC2 — 原子投影与 checkpoint-only 崩溃补全

- `test_interrupt_finalize_is_atomic_pending_cursor_events_terminal_and_lease`
  （真实 PG）：单事务提交 accepted head 提升 + pending(active) +
  `confirmation` Cursor + `agent.input.required → business.status →
  message.completed` 事件链 + execution `waiting_input`/lease 释放；
  事件顺序按 DB id 精确断言；stale owner（lease 过期）finalize 为
  零写（pending=0、事件数不变），无 terminal-only/Cursor-only 窗口；
- `test_checkpoint_only_crash_takeover_completes_original_turn`（真实 PG）：
  candidate 已落盘但 finalize 未提交（"崩溃"）→ App B takeover 复用原
  `graph_run_id/input_seq/input_turn_id`，仅 `attempt/fence +1`，从安全
  input fact 重建（从未接纳 head），重新跑到 interrupt 后补全原 turn，
  原 turn 只有一个 `message.completed` terminal。

### AC3 — resume 原子事务、精确 head 种入、单次 Command(resume)、确认最多一次

- `test_begin_resume_validates_exact_head_and_seeds_before_first_graph_call`
  （真实 PG）：缺 head fail-closed（零新事实）；成功后 pending→`resuming`、
  新 execution + `message.user` 原子创建、精确三元 head 种入；
  "resume 事务已提交、图未调用"崩溃 → App B takeover `source="checkpoint"`
  且从种入的精确 head 继续；
- `test_resume_confirm_full_flow_applies_cas_once_and_closes_atomically`
  （真实 PG）：App A interrupt → App B 一次 `Command(resume)` 经 rehydrate
  进入 `apply_confirmation_cas` → END；`finalize_resume_outcome` 单事务
  关闭 pending(resolved)/Cursor(consumed)/唯一 terminal/execution completed；
  `draft.confirmed=True`、revision 恰 +1、`apply_confirmation` step 恰 1 条、
  正式 Request 0 条；
- `test_resume_field_edit_reinterrupts_and_replaces_pending_atomically`
  （真实 PG）：改字段在同一 resume 内重路由 → 再次 interrupt →
  `finalize_interrupt(previous_pending_input_id=...)` 同事务 re-arm 同一
  pending 行（新 revision/head），无双 active、消息不丢；
- `test_resume_wrong_auth_session_fails_closed_with_zero_writes`（真实 PG）：
  错 auth 的 resume 零新事实，pending 保持 active；
- `test_concurrent_resume_second_request_409_without_new_facts`（真实 PG）：
  双线程并发 resume 恰一个成功，另一个 409 族
  （`TurnInProgressError`/`TurnRecoveryInProgressError`/`TurnLockUnavailableError`）；
- 图级（InMemorySaver）路径断言：confirm 路径
  `await_requester_confirmation → rehydrate_resume_snapshot →
  apply_confirmation_cas → ready_to_submit → finalize_public_outcome`；
  换题/拒绝路径 `... → rehydrate → route_intent → ...`（同一 resume 内），
  `apply_confirmation_cas` 不出现；确认判定复用
  `_explicit_confirmation_from_text`（仅 True 才确认）。

## 3. 可信红灯测试

`apps/api/tests/agent/test_t34_graph_interrupt.py` 5 项在实现前全部红灯：
失败原因确认为"图停在普通返回、不调用 `interrupt()`"（
`GraphOutput(... business_status='awaiting_confirmation' ...)` 而非
`ConfirmationInterruptRaised`），随后实现转绿。

## 4. 实现入口与真实图路径

- 实现入口：
  - `apps/api/src/accesspilot/agent/production_graph.py`：
    `_await_requester_confirmation`（interrupt + `Command(update, goto)`）、
    `_rehydrate_resume_snapshot`、`_apply_confirmation_cas`、
    `_confirmation_conflict_update`、`_route_confirmation_result`、
    `ConfirmationInterruptRaised`、`ProductionGraph.invoke/stream/get_state`
    resume 支持、拓扑（await 无静态出边、`apply_confirmation_cas` 条件边）；
  - `apps/api/src/accesspilot/agent/turn_execution.py`：
    `finalize_resume_outcome`、`finalize_interrupt(previous_pending_input_id)`；
  - `apps/api/src/accesspilot/agent/checkpoint.py`：
    `FencedSaverInvocation.put_writes` 对 resume 派生 head 的放行说明
    （writes 不能成为 accepted head；promote 只认 verified candidate）；
- 真实图路径（compiled-stream 断言）：
  - 首次：`hydrate → route_intent → parse_request_patch →
    [resolve_entitlement] → merge_candidate → persist_draft_cas →
    validate_draft → await_requester_confirmation`（停在 interrupt）；
  - confirm：`await_requester_confirmation → rehydrate_resume_snapshot →
    apply_confirmation_cas → ready_to_submit → finalize_public_outcome`；
  - 非确认：`... → rehydrate_resume_snapshot → route_intent → ...`。

## 5. 本轮实测结果

- T34 定向：`12 passed`
  - `apps/api/tests/agent/test_t34_graph_interrupt.py`：5 项
  - `apps/api/tests/db/test_t34_postgres.py`（真实隔离 disposable
    PostgreSQL + 真实 PostgresSaver）：7 项
- 受影响回归：`apps/api/tests/agent/test_t32_request_graph.py` 27 项、
  `apps/api/tests/agent/test_t30_graph_state.py` 57 项（await 边界与
  拓扑断言按 T34 合同更新）、`test_t33_*` 全部通过；
- 完整 API 测试：`685 passed, 1 skipped, 1 deselected`
  - skip：T26 destructive-isolated PostgreSQL probe 环境门控；
  - deselected：`test_t30_postgres.py::test_t30_real_postgres_strict_checkpoint_scan_and_malicious_zero_write`
    为基线（`1a493eb` stash 验证）即失败的 T31 遗留测试——其 runtime
    context 缺 T31 引入的真实 `WorkspaceService` 绑定，与 T34 无关，
    不夹带修复（P2）。
- Ruff：`All checks passed!`
- MyPy：`Success: no issues found in 53 source files`
- `git diff --check`：通过

## 6. 修改与新增文件

- 修改：
  - `apps/api/src/accesspilot/agent/production_graph.py`
  - `apps/api/src/accesspilot/agent/turn_execution.py`
  - `apps/api/src/accesspilot/agent/checkpoint.py`
  - `apps/api/tests/agent/test_t32_request_graph.py`（await 边界合同更新）
  - `apps/api/tests/agent/test_t30_graph_state.py`（拓扑断言合同更新）
- 新增：
  - `apps/api/tests/agent/test_t34_graph_interrupt.py`
  - `apps/api/tests/db/test_t34_postgres.py`
  - 本证据包

## 7. P0/P1/P2

- P0：0；P1：0。
- P2：
  1. `test_t30_postgres.py` 基线遗留失败（T31 引入的 context 合同漂移，
     与 T34 无关），已在完整回归中 deselected 并披露；
  2. 图级测试使用 InMemorySaver（语义快速验证），真实 PostgreSQL 证明
     全部在 `tests/db/test_t34_postgres.py`；
  3. `current_input_seq` 由调用层经 runtime context 传入（图不自增，
     符合 Spec §6.3.7"input_seq 来自已提交 execution record"）。

## 8. 未实现边界（诚实披露）

- 未接入生产 JSON/SSE 入口（T38/T40）；interrupt 尚不可被真实 API resume。
- 未做六点故障注入（T37）；本轮真实 PG 覆盖了其中两个窗口
  （checkpoint-only 崩溃、resume 事务已提交图未调用崩溃）。
- 未做 T35 粘性引擎/对账/降级。
- 未标记 `Verified`：需要独立只读 reviewer 复核（P0/P1=0）后才能更新。
- 正式简历未修改。

## 9. 本地提交

实现、测试与证据文档已整理；提交信息（中文 Conventional Commits）：
`feat(agent): 完成 T34 申请人确认中断恢复`
（提交前已核对 `git diff --cached --name-only` 仅含 T34 授权文件，
不夹带用户脏文件。）
