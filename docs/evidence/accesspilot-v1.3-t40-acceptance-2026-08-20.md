# AccessPilot v1.3 T40 验收证据包（Verified）

**Ticket：** T40 — SSE LangGraph 门禁、传输合同与 Sticky Flow 2 Canary
**实现基线：** `3272a4f`（T39）；T40 已本地提交 `51ce85f`
**日期：** 2026-08-20
**状态：** `Verified`；独立验收修复轮 P0/P1/P2=0/0/1，P2 不阻塞；用户已授权仅本地提交

## 1. 范围与边界

本 Ticket 将真实生产 `CompiledStateGraph` 接入 SSE，并让 JSON/SSE 对同一 Workspace 共享服务端 sticky engine binding。实现保留 Legacy 回滚路径，不删除 checkpoint 或业务数据，不把 LangGraph debug/value/checkpoint 暴露到浏览器，也没有部署或修改既有正式 canary。

- LangGraph SSE 的 turn admission、图执行与 terminal finalization 复用 T33–T38 的 execution、lease/fence、accepted checkpoint head 与 pending/Cursor 合同；
- graph worker 未确认停止时，断线不写 `turn.interrupted`、不释放 lease；
- mixed cohort 只在新 Workspace 创建时按服务端 `agent_thread_id` 稳定分配，两种聊天入口不允许各自选择引擎；
- T41 的全量产品验收、浏览器主链和完整回滚演练不在本 Ticket 内提前声明。

## 2. 三条验收标准

### AC1 — 真实 SSE 生产图与成熟传输合同

隔离注入的 `/api/chat/messages/stream` 实际调用生产多节点图，并保持同一 HTTP/graph `turn_id`、v1 envelope、连续 `seq` 与 `turn_id:seq` 事件身份。流式 delta 不落库，terminal 由 graph engine 单一所有者在 accepted checkpoint head 提升后闭合；answer 安全失败、取消或 interrupt answer 失败不会留下伪 `message.completed`，interrupt 的 pending/head/Cursor 仍保持一致。

既有敏感跨 chunk、取消、Last-Event-ID、背景流去重、events API 与 Nginx 合同由受影响回归继续证明；浏览器出口没有 debug/value/checkpoint 原文。

### AC2 — 断线 worker 屏障、竞态与唯一终态

直接 ASGI 断线测试证明：同步 graph worker 阻塞期间，execution 维持 `running`、lease 保留且 terminal 为 0；worker 确认返回后才写 `turn.interrupted` 并释放 lease。completion/disconnect 竞态复用同一 terminal ID，最终只有一个 terminal，accepted head 非空且 Workspace fence 不回退。

独立验收首轮发现并发 admission P1：第二个 SSE 曾在第一个未过期 graph turn 运行时错误复用旧 `turn_id`，先返回 200/旧 `turn.started`，随后静默丢弃新输入。回修把 LangGraph SSE admission 移到 `StreamingResponse` 构造和首帧之前，并复用 Workspace 行锁、thread advisory lock 与 fence；live-running 请求现在固定 409 且零新增事实。

### AC3 — Sticky flow、双入口一致性与真实 PostgreSQL 恢复

`mixed` 的 0–100 合法值在 T40 后开放，缺失、越界、strict msgpack 或 checkpoint 配置不满足时仍 fail closed。新 Workspace 在创建事务内按服务端 `agent_thread_id` 分配稳定 flow；flow 1 的 JSON/SSE 均走 Legacy，flow 2 的两个入口均走 LangGraph，binding conflict 在执行前固定返回 409。

产品 `create_app`/lifespan 实际构造官方 PostgresSaver；Legacy 模式不会无谓启动 graph runtime。真实隔离 PostgreSQL 覆盖 confirm/non-confirm × JSON→SSE/SSE→JSON 四向恢复：同 run/thread、每个已接受 turn 唯一 terminal、pending resolved、Cursor cleared 且权威 draft 一致。

## 3. TDD 与独立验收

首轮可信红灯为 `2 failed`：SSE 注入尚未进入生产图，且 `mixed=100` 被旧配置门禁拒绝。实现后补齐真实 SSE、lifespan saver、双入口 sticky、断线 worker 屏障、answer error、binding conflict 与真实 PG 四向恢复。

独立验收首轮在既有测试全绿时仍构造出可达 P1：live-running 第二个 SSE 返回 200 并静默丢消息；同根因也影响 expired recovery 的不同输入。两条 direct-ASGI 测试先稳定得到 `2 failed`，原子 admission 回修后转为 `2 passed`。同一独立验收者仅复核该 P1 与直接受影响面，确认 P1 关闭并判定 PASS。

## 4. 实测证据

实现收口：

- 完整 T40：`16 passed`，0 skip，包含官方 PostgresSaver 真实隔离 PostgreSQL 的四向跨入口场景；
- P1 回修直接受影响回归：`169 passed`，0 skip；
- Ruff 全 API 源码/测试、MyPy 58 个源码文件、`git diff --check`：通过。

独立验收修复轮：

- 两条 P1 direct-ASGI：`2 passed`；
- 完整 T40：`16 passed`，0 skip，真实 PostgreSQL 四向集成全部通过；
- transport / JSON / create_app / engine-binding：`63 passed`；
- falsey 注入与 Legacy runtime：`2 passed`；
- Ruff、MyPy 58 个源码文件、`git diff --check`：通过。

## 5. 非阻塞 P2

后台 cleanup task 的 done callback 目前只移除强引用，没有主动消费或记录潜在异常；lifespan 关闭前也不等待全部 cleanup task。若断线后的 terminal finalizer 遭遇瞬时数据库/checkpointer 错误，可能出现未观察的 task exception。

execution 在该失败下仍保持可由 T33/T37 takeover 闭合的状态，因此不构成伪成功、lease 错放或业务数据丢失，未升为 P1。按修复轮范围纪律，本 Ticket 不扩大实现观察性增强；可在后续维护或 T41 集成门禁中记录运行日志行为。

## 6. 修改文件

- `apps/api/src/accesspilot/conversation.py`
- `apps/api/src/accesspilot/events.py`
- `apps/api/src/accesspilot/auth.py`
- `apps/api/src/accesspilot/config.py`
- `apps/api/src/accesspilot/main.py`
- `apps/api/src/accesspilot/agent/engine_binding.py`
- `apps/api/src/accesspilot/agent/json_orchestrator.py`
- `apps/api/src/accesspilot/agent/turn_execution.py`
- `apps/api/tests/agent/test_t35_engine_binding.py`
- `apps/api/tests/api/test_t40_sse_langgraph.py`
- 本证据包与 v1.3 Spec、Ticket、Change Ledger 当前状态记录

## 7. 当前停点

T40 已 `Verified` 并按用户授权本地提交 `51ce85f`；后续 Ticket 在独立验收通过后默认仅本地提交。当前进入 T41；未授权推送、合并、部署、修改正式简历或删除 Legacy/checkpoint 数据。
