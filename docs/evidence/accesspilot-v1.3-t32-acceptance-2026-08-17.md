# AccessPilot v1.3 T32 验收证据（Verified）

**Ticket：** T32 — 申请收集、模型解析、草稿 CAS 与步骤幂等
**实现基线 revision：** `7ad8a01`；T32 本地提交见 git log（本文件随 T32 提交）
**日期：** 2026-08-17
**状态：** `Verified`；首轮 2 项 P1 与二轮 1 项 P1 均已闭环；二轮独立复核 P0=0、P1=0，验收通过，已本地提交

## 0. 验收迭代记录

- **首轮独立验收：** P0=0、P1=2（①第二次模型调用缺少 `CORRECTION_PROMPT` 断言；②12 个矩阵场景只验证 outcome 未验证真实节点路径）。已闭环：矩阵每轮新增调用序列断言与 `expected_path` 路径断言。
- **二轮只读复核：** P0=0、P1=1。纠正重试已锁死为 `[None, CORRECTION_PROMPT]`，12 个矩阵场景已验证路径；但 revision race 与合法 numeric 两个场景仍只验证结果，且原冻结口径为 14 个正常场景，不能缩成 12 个。
- **本轮修复：** 两个场景各在两轮 fresh fixture 中采集 compiled stream 节点序列并逐节点断言，14 个正常场景×2 轮全部含路径断言（path 28/28）。
- **最终独立复核：** P0=0、P1=0；二轮指出的 revision race 与合法 numeric 路径断言已确认闭合；本轮定向 `52 passed`、Agent 相关回归 `221 passed, 1 skipped`、Ruff/MyPy/`git diff --check` 通过。完整 API `646 passed, 3 skipped` 为历史证据，不是本轮重新实测。

## 1. 已验证改变

- 真实实现 `parse_request_patch → resolve_entitlement → merge_candidate → persist_draft_cas → validate_draft → ask_missing_field / await_requester_confirmation` 的申请收集主链，与 `_handle_numeric_followup` 的合法 numeric duration Cursor 消费。
- 模型两次 attempt 各使用稳定 `operation_id`；quota 消费与 ledger reserve/complete 同一事务；第二次 attempt 仅在 primary malformed 后发生，并携带 `CORRECTION_PROMPT`。
- 普通草稿 CAS、numeric duration Cursor 与 justification Cursor 各使用独立 `step_key` 的稳定 operation；重放先查 completed operation，不重复扣 quota、不重复递增 revision。
- 缺项/下一个 Cursor 只在成功终态 finalizer 中激活，且 finalizer 走与节点相同的 fenced service：单事务内 `_lock_execution → _load_step → _lock_workspace` 顺序加锁，写 Cursor 五列 + `activate_missing_cursor` completed 事实。
- `completed_step` 锁顺序统一为 execution→step→workspace，与其余写方法一致；`_lock_execution` 上方记录 canonical 锁顺序，T33 接管路径必须沿用。

## 2. 模型纠正参数证据（首轮 P1-1，已闭环）

- 12 个矩阵场景每一轮都断言模型调用序列：`graph_model.calls == legacy_model.calls == len(expected_corrections)` 且 `graph_model.corrections == legacy_model.corrections == expected_corrections`。
- 期望序列按场景推导：primary 调用传 `None`；只有 primary malformed 后的第二次调用才传 `CORRECTION_PROMPT`；HTTP/超时失败不触发第二次调用。
- `malformed_then_retry_success` 与 `malformed_twice` 场景因此被锁定为 `calls == 2` 且 `corrections == [None, CORRECTION_PROMPT]`。

## 3. 14 个正常场景的节点路径证据（首轮 P1-2 + 二轮 P1 修复）

- **12 个矩阵场景**：每个在两轮中先用 fresh-identical fixture 采集真实 compiled-stream 节点序列（`stream_mode="updates"`），与场景内嵌 `expected_path` 逐节点断言：path `24/24` 一致。覆盖四条真实链路：缺项 `ask_missing_field`；完整草稿 `await_requester_confirmation`（含/不含 `resolve_entitlement` 两种）；recoverable 闭合（含/不含 `resolve_entitlement` 两种）。
- **revision race**：两轮中独立 fresh fixture 用同样的并发 `save_draft_cas` 回调制造 race，采集 compiled stream 并锁死 `hydrate→route_intent→parse_request_patch→resolve_entitlement→merge_candidate→persist_draft_cas→validate_draft→compose_recoverable_answer→finalize_public_outcome`，证明冲突确实发生在 persist CAS 节点并走 recoverable 闭合，而不是结果巧合。
- **合法 numeric**：两轮中独立 fresh fixture（duration_days Cursor 预置）采集 compiled stream 并锁死 `hydrate_authoritative_snapshot→route_intent→handle_numeric_followup→finalize_public_outcome`，证明合法期限确实由 numeric 节点消费 Cursor 并写 draft。
- 14 个正常场景合计 path `28/28`；同一测试继续逐字段比较 normalized outcome、phase、quota 与 Cursor 投影与 Legacy（矩阵 `24/24`，race 与 numeric 各 2 轮），另 2 个 quota 异常场景×2 轮失败行为（`ModelQuotaExceededError`、模型调用次数、quota/ledger 事实）与 Legacy 一致。

## 4. 事务、幂等与 fenced finalizer 证据

- 重放幂等：同一 operation 只产生一行 ledger；二次激活返回 `replayed=True`，Cursor `issued_at` 不变；draft/ledger 冲突整体回滚零写入。
- stale fence / 过期租约：`StepExecutionRejected`，Cursor 五列与 ledger 均零写入（step 层与 finalizer 层各验证）。
- 他方会话 Cursor 与 revision 冲突：`DraftRevisionConflictError`，零写入。
- finalizer 重放幂等：两次 prepare/finalize 只激活一次 Cursor（一行 `activate_missing_cursor` 事实）。
- finalizer 遇 stale execution fence：拒绝且 Cursor 不激活，无 ledger 行。
- 完整草稿在 await 边界安全结束：不创建 pending、不激活 confirmation Cursor；图不创建 Request/Approval/Grant。

## 5. 复跑结果（本轮修复后）

- T32 定向：`52 passed`（基线 47 + 新增 5）；
- Agent 相关回归：`221 passed, 1 skipped`；
- Ruff、MyPy（50 个 source files）：通过；
- `git diff --check`：通过；
- 独立只读验收：首轮 P1=2（已闭环）、二轮 P1=1（已补齐）；最终独立复核 P0=0、P1=0。
- 历史完整 API（非本轮重跑）：此前记录为 `646 passed, 3 skipped`；三项 skip 为 T26/T29/T30 各自 destructive-isolated PostgreSQL 专项证明，不是 T32 失败。本轮未重跑，不得写成“本轮独立实测”。

## 6. 简历与面试价值（已启用）

候选表述：

> 将 AccessPilot 的申请收集主链（模型解析、权限解析、草稿 CAS、字段校验）迁入真实 LangGraph 节点，以稳定 operation ledger 实现 quota/草稿/Cursor 的应用级幂等，并让第二次模型调用携带纠正提示、14 个正常场景连续两轮 path+outcome 28/28 与 Legacy 一致；补充 fenced finalizer 事务（Cursor 五列与 ledger 同事务、重放同一行、stale fence/过期租约零写入），此前完整 API 646 项回归通过（本轮未重跑）。

可展开讲解：为什么 at-least-once 执行下本地副作用要 exactly-once；CAS 与 revision 冲突如何回滚；为什么 finalizer 也必须复核 execution fence 而不是只调 WorkspaceService；canonical 锁顺序为什么是 execution→step→workspace。

## 7. 限制

- T32 只校验夹具预建的 `running` execution，不创建、续租、接管或释放 execution；lease 心跳、advisory lock 与崩溃接管由 T33 验证。
- `await_requester_confirmation` 仍不 interrupt；真实 pending/resume 与确认 CAS 由 T34 验证。
- 图仍未接入生产 JSON/SSE；T38/T40 才做入口门禁与 canary。
- 本文件只提供简历候选内容；正式简历仍需用户确认后才能修改。
