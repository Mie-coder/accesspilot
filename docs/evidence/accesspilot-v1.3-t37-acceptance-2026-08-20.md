# AccessPilot v1.3 T37 验收证据包（Verified）

**Ticket：** T37 — 六个崩溃边界的幂等恢复
**实现基线：** `934cb0e`（T36）；T37 已本地提交 `e1e47cc`
**日期：** 2026-08-20
**状态：** `Verified`；首轮独立验收 P0=0、P1=1、P2=0，回修后定向复核 P0/P1/P2=0

## 1. 范围与边界

本 Ticket 在生产图的六个精确非原子窗口增加进程内测试 fault hook，并验证 crash → restart → takeover/reconcile 后的输入恢复、应用级幂等、fence 与 accepted checkpoint head 合同。

- hook 默认关闭、单次触发，只能由测试进程显式打开；没有接入 settings、依赖注入、Graph Runtime Context、API 或 UI；
- 测试使用生产图、真实 PostgreSQL 应用事务与 advisory lock；六点套件的 checkpoint saver 是跨图实例共享的 `InMemorySaver`；
- 真实 PostgresSaver 的 interrupt/resume、精确 head 与事件去重由 T34/T36 disposable PostgreSQL 回归补证；
- JSON/SSE 产品入口仍保持 Legacy，T37 不构成生产高可用或生产切流 Claim。

## 2. 三条验收标准

### AC1 — 六个默认关闭、仅测试可用的精确 fault hook

`apps/api/src/accesspilot/agent/fault_injection.py` 定义六个 `FaultPoint`、默认空的 `ContextVar`、单次消费的 `hit_fault()` 和显式测试 scope。六点分别位于：

1. Provider 成功返回后、model completion/checkpoint 前；
2. 只读工具返回后、`tool.completed` 前；
3. 草稿 CAS 提交后、checkpoint 前；
4. interrupt 已由 saver 保存后、调用层 finalize 前；
5. 非确认 resume 已消费后、rehydrate/重新路由前；
6. 确认 CAS 提交后、terminal 前。

独立验收全仓检索未发现任何 API、UI、settings 或 runtime payload 可注入入口；hook 合同测试 7 项通过。

### AC2 — 六点恢复保持同一逻辑输入与本地事实最多一次

`apps/api/tests/agent/test_t37_recovery.py` 对六点逐一执行 crash → lease 过期 → takeover → graph restart/reconcile，并验证：

- `graph_run_id/input_seq/input_turn_id/input_event_id` 保持不变，只有 takeover 将 `attempt/lease_fence` 各递增 1；
- 恢复从持久化安全 `message.user` 事实重建，不依赖旧进程内输入；
- quota、草稿 revision、步骤 operation、accepted head、轨迹语义身份和每 turn terminal 不重复、不回退；
- Request、Approval、Grant 始终为 0；
- 未接纳的 interrupt candidate 不被选为恢复 head，旧 owner 的 step 写与 accepted-head CAS 被 fence 拒绝。

### AC3 — 外部 at-least-once 与 checkpoint/head 失败闭合

- Provider 已成功返回后触发 crash，恢复允许 Provider 第 2 次调用，本地 quota 仍为 1；只读工具同样允许调用两次，但只产生一组稳定轨迹；
- 确认 CAS 后崩溃通过稳定 `pending_input_id` operation 读取已完成事实，并同时校验当前 execution/fence/session/workspace、同一 graph run、`pending revision + 1`、权威 revision 与 `draft.confirmed=true`；
- interrupt/resume 和普通 END 的 graph finalizer 都先在同一 fenced 事务中提升 verified candidate，再写 terminal、更新 status 并释放 lease；promotion 失败整体回滚，保留 `running`、lease 和空 terminal。

## 3. 独立验收发现与回修

首轮独立验收发现 1 项可达 P1：普通只读工具 END 路径虽然验证了 candidate，却使用不接收 candidate 的旧 `complete_turn_with_event`，可能在 accepted-head promotion 失败时仍写成功 terminal 并释放 lease。

最小修复保留旧 finalizer 给无 checkpoint candidate 的既有调用方，新增 graph-only `finalize_graph_turn_with_event`，强制接收 `VerifiedCheckpointCandidate`。收紧后的单测先因新合同不存在得到 `1 failed`；实现后转为 `1 passed`，并明确反证 promotion 失败时 execution 仍为 `running`、lease 保留、terminal 为空、accepted head 不变，随后成功 finalization 才提升 head、写唯一 terminal 并释放 lease。

定向复核确认原 P1 关闭，P0/P1/P2=0；没有重新开启已关闭的其他审计范围。

## 4. 实测证据

主实现阶段：

- T37 定向：`13 passed`；
- T30–T36 生产图/步骤操作受影响回归：`166 passed`；
- T34/T36 disposable PostgreSQL 回归：`16 passed`；
- Ruff、MyPy（3 个受影响源码）、`git diff --check`：通过。

P1 回修阶段：

- 可信红灯/转绿：`1 failed` → `1 passed`；
- T37 定向：`13 passed`；
- T33/T34/T36 Agent 回归：`36 passed`；
- T33/T34/T36 disposable PostgreSQL 回归：`25 passed`；
- Ruff、MyPy（4 个受影响源码）、`git diff --check`：通过。

独立定向复核：

- 原失败场景：`1 passed`；
- T37 定向：`13 passed`；
- 旧 T33 finalizer 回归：`14 passed`；
- Ruff、MyPy、`git diff --check`：通过；
- skip/blocker：0。

首次运行中的 localhost 沙箱限制、Docker 未启动、默认测试 URL 缺密码，以及遗漏 `LANGGRAPH_STRICT_MSGPACK=true` 均发生在测试 fixture/环境门禁处；补齐仓库规定环境后已实测通过，未把这些阻断记成代码测试失败或通过。未运行完整 API 全量测试，符合当前 Ticket 的定向与受影响回归边界。

## 5. 修改文件

- `apps/api/src/accesspilot/agent/fault_injection.py`
- `apps/api/src/accesspilot/agent/production_graph.py`
- `apps/api/src/accesspilot/agent/step_operations.py`
- `apps/api/src/accesspilot/agent/turn_execution.py`
- `apps/api/tests/agent/test_t37_fault_injection.py`
- `apps/api/tests/agent/test_t37_recovery.py`

## 6. 当前停点

T37 已 `Verified` 并按用户授权本地提交 `e1e47cc`。当前按已确认的 Ticket 顺序进入 T38；未授权推送、合并、部署或修改正式简历。
