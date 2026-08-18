# AccessPilot v1.3 T33 验收证据（Verified）

**Ticket：** T33 — Turn Execution Lease、Fence 与崩溃接管内核
**实现基线 revision：** `7ad8a01`（T32 基线）；T33 本地提交见 git log
**日期：** 2026-08-17
**状态：** `Verified`；独立复核 P0=0、P1=0，验收通过

## 1. AC 覆盖与证据

### AC1 — begin-input 原子分配并拒绝第二条 running input

- `TurnExecutionService.begin_input()` 在单个 Workspace 行锁事务内：
  - 生成/复用 `graph_run_id`，分配 `input_seq`、`input_turn_id`、`lease_fence`；
  - 原子写入 `turn.started`、清洗后 `message.user` 与 `AgentTurnExecutionRecord(status=running)`；
  - 第二条 running input 在写入任何用户事实前稳定抛出 `TurnInProgressError`（lease 未过期）或 `TurnRecoveryInProgressError`（lease 已过期）。
- 证据：
  - `apps/api/tests/agent/test_t33_turn_execution.py::test_begin_input_atomically_creates_execution_started_and_user_event`
  - `test_second_running_input_fails_before_new_facts`
  - `test_expired_running_input_reports_recovery_in_progress`
  - `apps/api/tests/db/test_t33_postgres.py::test_concurrent_begin_input_second_conflict_without_new_facts`（双线程真实 PostgreSQL，唯一成功 + 唯一 409，事件数不变）

### AC2 — graph 运行持有 advisory lock，心跳 CAS 续租，stale owner 写失败

- `AdvisoryLockHandle` 是不可伪造的锁所有权：只能由 `TurnExecutionService.advisory_lock()` 创建，并携带实际持有锁的 Session。
- `FencedGraphTurnRunner.run()` 要求调用方传入该 handle；在 graph invoke 前先做一次 `heartbeat()` 预检，因此 expired/stale handle 会在 `graph.invoke` 前失败、graph 调用次数为 0。
- graph invoke 全程在锁范围内，后台心跳按 `execution_id + fence + status=running` CAS 续租。
- `takeover`、`complete_turn` 和可选的 head promotion 都使用同一 lock-holding Session，锁连续覆盖 takeover → graph run → head promotion/finalize。
- T32 `AgentStepOperationService` 的每个写事务继续校验 execution/workspace fence；`AcceptedCheckpointHeadStore.promote()` 增加 Workspace 当前 `lease_fence` 与未过期 lease 条件。
- 证据：
  - `test_advisory_lock_is_exclusive_per_agent_thread`
  - `test_runner_holds_advisory_lock_during_graph_invoke`
  - `test_runner_expired_handle_fails_before_graph_invoke`
  - `test_heartbeat_renews_lease_and_rejects_stale_fence`
  - `apps/api/tests/db/test_t33_postgres.py::test_takeover_reuses_identity_increments_fence_and_stale_step_write_fails`
  - `test_stale_owner_head_and_terminal_zero_write_after_takeover`
  - `apps/api/tests/db/test_t29_postgres.py` 真实 PostgreSQL 通过（head CAS 的既有精确/孤儿反证）。

### AC3 — 过期接管复用原逻辑输入，按 accepted head 或安全 input fact 恢复

- `TurnExecutionService.takeover()` 要求 advisory-lock handle，并在 execution → workspace 锁序下：
  - 复用原 `graph_run_id/input_seq/input_turn_id/input_event_id`；
  - 仅递增 `attempt` 与 `lease_fence`；
  - 当前 execution、关联 pending 或同 `graph_run_id` 历史 execution 任一已有 accepted head 时，`source="checkpoint"` 且使用精确 `checkpoint_thread_id + checkpoint_ns + checkpoint_id`，绝不回退 `input_event`；
  - 只有三者从未接纳 head 才返回 `source="input_event"`；
  - 提供真实 saver 时，若选择 checkpoint 但精确 head 不存在，则 fail-closed，不递增 fence/attempt。
- 证据：
  - `test_takeover_reuses_identity_increments_fence_and_stale_step_write_fails`
  - `test_takeover_with_accepted_head_plans_exact_checkpoint_recovery`
  - `test_takeover_historical_accepted_head_forbids_input_event_fallback`
  - `test_takeover_missing_accepted_head_fails_closed_with_real_saver`
  - `test_real_checkpoint_exact_end_head_and_no_implicit_latest`
  - `test_t33_and_t32_write_paths_share_lock_order_no_deadlock`

## 2. 独立复核问题修复记录

第一轮修复：
1. expired handle 现在在 `graph.invoke` 前由 runner 预检 `heartbeat()` 拒绝，`_CountingGraph.calls == 0`。
2. takeover 改为必须携带不可伪造的 `AdvisoryLockHandle`；锁 handle 持有实际 Session，takeover/complete/head promotion 共用同一锁会话。
3. takeover 会检查当前 execution、关联 pending 和同 `graph_run_id` 历史 execution 的 accepted head，任一存在即禁止回退 `input_event`。
4. `AcceptedCheckpointHeadStore.promote()` 增加 `lease_expires_at > now` 条件。
5. 新增真实 PostgresSaver + 小型 StateGraph 测试：accepted exact head、END head、missing head fail-closed、禁止 implicit latest。
6. 新增 stale owner 的 step/head/terminal 三类零写入反证。

第二轮 P1 修复：
1. 锁句柄现在带 active 状态与目标 `agent_thread_id`，`runner.run()` 在任何图调用前执行 `lock.require_thread(handle.agent_thread_id)`；错误 Workspace 锁无法运行目标图。
2. advisory lock 使用专用且保持 check-out 的 connection，支持多个应用事务：`takeover()` 提交后再进入 runner，runner heartbeat 能读到新 fence；`promote()`/finalize 各自提交后锁才释放。
3. 删除 `AdvisoryLockHandle` 可公开取得的 `_create/_token` 绕过路径；context 退出后 handle 永久失效。
4. 新增 `complete_turn_with_event()`：terminal 事件创建与 execution terminalize 在同一个 fenced 事务；stale owner 场景断言事件数不增加。
5. 新增真实组合测试 `test_takeover_runner_promote_finalize_combined_real_checkpoint`：takeover → 精确 accepted interrupt head 恢复 invoke → promote → finalize 全程由服务方法提交，测试不手动 `commit()`。

第三轮 P1 修复（terminal finalizer 合同）：
1. `complete_turn_with_event()` 删除可调用方指定的 `status`，状态由 `event_type` 唯一派生：`message.completed → completed`、`error.recoverable → recoverable_error`、`turn.interrupted → interrupted`。
2. terminal payload 的 `turn_id` 必须等于 `handle.input_turn_id`，不匹配时事件和 execution 都零写入。
3. 删除旧的公开 `complete_turn(terminal_event_id=...)` 逃生口；生产只保留原子 `complete_turn_with_event()`。
4. 新增测试：三种 event/status 映射、错误 turn_id 零写入、旧入口不可用。

## 3. 测试结果（本轮真实命令输出）

- T33 定向：`23 passed`
  - `apps/api/tests/agent/test_t33_turn_execution.py`：14 项
  - `apps/api/tests/db/test_t33_postgres.py`（真实隔离 disposable PostgreSQL）：9 项
- Agent 相关回归：`235 passed, 1 skipped`
  - skip 为 T26 destructive-isolated PostgreSQL probe 的环境门禁。
- 相关真实 PG 回归：`apps/api/tests/db/test_t29_postgres.py` 通过。
- Ruff：`All checks passed!`
- MyPy：`Success: no issues found in 53 source files`
- `git diff --check`：通过。

## 3.5 独立复核结论

- 独立复核：P0=0、P1=0。
- AC1/AC2/AC3 均通过；`takeover → exact resume → promote → finalize` 真实组合路径已跑通。
- 状态：`Verified`。

## 4. 实现文件

- `apps/api/src/accesspilot/agent/advisory_lock.py`（新增）
- `apps/api/src/accesspilot/agent/turn_execution.py`（新增）
- `apps/api/src/accesspilot/agent/turn_runner.py`（新增）
- `apps/api/src/accesspilot/agent/checkpoint.py`（`AcceptedCheckpointHeadStore.promote` 增加 Workspace fence 与未过期 lease 校验）
- `apps/api/tests/agent/test_t33_turn_execution.py`（新增）
- `apps/api/tests/db/test_t33_postgres.py`（新增）

## 5. 限制

- 未实现 T34 interrupt/resume 业务语义；`await_requester_confirmation` 仍不产生真实 pending。
- 未接入生产 JSON/SSE 入口；T33 runner 是可复用内核，T38/T40 才做入口门禁与 canary。
- 未生成 T36 脱敏轨迹事件。
- 未做六点崩溃恢复故障注入（T37）。
- 本文件已由独立复核确认 P0=0、P1=0，状态为 `Verified`；正式简历仍需用户另行确认后才修改。
