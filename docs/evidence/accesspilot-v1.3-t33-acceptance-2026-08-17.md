# AccessPilot v1.3 T33 验收证据（待独立复核）

**Ticket：** T33 — Turn Execution Lease、Fence 与崩溃接管内核
**实现基线 revision：** `7ad8a01`（T32 基线）；T33 本地提交见 git log
**日期：** 2026-08-17
**状态：** `In progress`（已实现，待独立复核）；尚未标记 `Verified`

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

- `FencedGraphTurnRunner` 在 graph invoke 全程持有 `agent_thread_id` 稳定派生的 PostgreSQL session advisory lock，并用后台心跳线程按 `execution_id + fence + status=running` CAS 续租。
- `TurnExecutionService.heartbeat()` 拒绝过期 lease 和 stale fence。
- T32 `AgentStepOperationService` 的每个写事务继续校验 execution/workspace fence；`AcceptedCheckpointHeadStore.promote()` 本次新增校验 Workspace 当前 `lease_fence`，stale owner 无法提升 accepted head。
- 证据：
  - `test_advisory_lock_is_exclusive_per_agent_thread`
  - `test_runner_holds_advisory_lock_during_graph_invoke`
  - `test_heartbeat_renews_lease_and_rejects_stale_fence`
  - `apps/api/tests/db/test_t33_postgres.py::test_takeover_reuses_identity_increments_fence_and_stale_step_write_fails`（stale fence 的 `reserve_model_attempt` 固定 `StepExecutionRejected`，零 quota 写入）
  - `apps/api/tests/db/test_t29_postgres.py` 真实 PostgreSQL 通过（head CAS 的既有精确/孤儿反证）。

### AC3 — 过期接管复用原逻辑输入，按 accepted head 或安全 input fact 恢复

- `TurnExecutionService.takeover()` 在 execution → workspace 锁序下：
  - 复用原 `graph_run_id/input_seq/input_turn_id/input_event_id`；
  - 仅递增 `attempt` 与 `lease_fence`；
  - 有 accepted head（execution 或链接 pending）返回 `source="checkpoint"` 与精确 `checkpoint_thread_id + checkpoint_ns + checkpoint_id`；
  - 从未接纳 head 返回 `source="input_event"`，允许从安全 `input_event_id` 重建。
- 证据：
  - `test_takeover_reuses_identity_increments_fence_and_stale_step_write_fails`
  - `test_takeover_with_accepted_head_plans_exact_checkpoint_recovery`
  - `apps/api/tests/db/test_t33_postgres.py::test_t33_and_t32_write_paths_share_lock_order_no_deadlock`（短 `lock_timeout` 下与 T32 写路径同锁序，无反向死锁）

## 2. 测试结果（本轮真实命令输出）

- T33 定向：`11 passed`
  - `apps/api/tests/agent/test_t33_turn_execution.py`：7 项
  - `apps/api/tests/db/test_t33_postgres.py`（真实隔离 disposable PostgreSQL）：4 项
- Agent 相关回归：`228 passed, 1 skipped`
  - skip 为 T26 destructive-isolated PostgreSQL probe 的环境门禁。
- 相关真实 PG 回归：`apps/api/tests/db/test_t29_postgres.py` 通过。
- Ruff：`All checks passed!`
- MyPy：`Success: no issues found in 52 source files`
- `git diff --check`：待提交前实跑（见提交记录）。

## 3. 实现文件

- `apps/api/src/accesspilot/agent/turn_execution.py`（新增）
- `apps/api/src/accesspilot/agent/turn_runner.py`（新增）
- `apps/api/src/accesspilot/agent/checkpoint.py`（`AcceptedCheckpointHeadStore.promote` 增加 Workspace fence 校验）
- `apps/api/tests/agent/test_t33_turn_execution.py`（新增）
- `apps/api/tests/db/test_t33_postgres.py`（新增）

## 4. 限制

- 未实现 T34 interrupt/resume 业务语义；`await_requester_confirmation` 仍不产生真实 pending。
- 未接入生产 JSON/SSE 入口；T33 runner 是可复用内核，T38/T40 才做入口门禁与 canary。
- 未生成 T36 脱敏轨迹事件。
- 未做六点崩溃恢复故障注入（T37）。
- 本文件不是独立验收结论；`Verified` 必须由独立 reviewer 给出 P0=0、P1=0 后标记。
