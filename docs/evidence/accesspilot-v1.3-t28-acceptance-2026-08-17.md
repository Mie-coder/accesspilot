# AccessPilot v1.3 T28 验收证据

**Ticket：** T28 — 应用 Schema、运行事实与确定性身份  
**实现基线 revision：** `db2ebf2`  
**日期：** 2026-08-17  
**状态：** `Verified`；独立只读验收 AC1–AC3 全部 PASS，P0/P1/P2 均为 0，等待本 Ticket 本地提交

## 1. 已验证改变

- Workspace 新增服务端内部 `agent_thread_id`、`flow_version` 和单调 `lease_fence`；存量逐行回填，新建由服务端/数据库生成，API 不接受也不返回这些字段。
- 新增 turn execution、pending input 和 step execution 三类应用事实；使用 PostgreSQL CHECK、partial unique、复合外键与 trigger 固化 live 状态、租约/终态、checkpoint 坐标、跨 Workspace 引用和 retirement tombstone 不变量。
- `workspace_events.event_key` 保持 nullable；历史事件不回填，只对同 Workspace 的非空 key 建 partial unique。
- 新增版本化 canonical JSON + SHA-256 身份工具，稳定生成 operation、confirmation、step、event 与 tool identity；execution attempt/fence/retry 不进入身份。

## 2. 验收结果

| 验收标准 | 结果 | 核心证据 |
|---|---|---|
| Workspace 回填、服务端生成、不可变/单调及客户端隔离 | PASS | 两条存量行 distinct UUID；真实 trigger/Store/API 注入与响应反证 |
| execution/pending/step/event 数据不变量 | PASS | 真实 PostgreSQL status/root-ns/partial unique/跨租户 FK/tombstone/event NULL 语义反证 |
| canonical identity 与可逆迁移 | PASS | 硬编码黄金向量；fresh/no-drift；含数据 0009→0010→0009→0010；checkpoint sentinel 指纹不变 |

验证数字：

- T28 定向：`23 passed`；
- 旧事件、Workspace、Auth 与 Chat 相关回归：`58 passed`；
- 完整 API：`491 passed`；
- Ruff、MyPy（45 个 source files）、Alembic no-drift 与 `git diff --check`：通过；
- 独立测试创建的临时数据库已清理，零残留。

完整 API 首次从受限沙箱运行时因 localhost 连接被操作系统拒绝而产生批量基础设施假失败；将隔离测试数据库迁至 0010 后，在获准访问本机测试数据库的同一代码 revision 重跑为 `491 passed`。

## 3. 迁移与隔离证据

- Fresh 数据库可升级到 head 并通过 `alembic check`。
- 含两条 Workspace、历史 Workspace events 与既有 Auth/Chat 事实的数据库完成 `0009 → 0010 → 0009 → 0010`，旧事实保持一致。
- 非默认 checkpoint sentinel schema/table/row 在 upgrade、downgrade 和 no-drift 各阶段 fingerprint 不变；AccessPilot Alembic 未接管、删除或降级 checkpoint 对象。
- `event_key` 的多个 NULL、同 Workspace 重复非空、跨 Workspace 相同非空 key 均在真实 PostgreSQL 验证。

## 4. 简历与面试价值

候选表述：

> 为可恢复 Agent Loop 设计应用侧运行事实层，通过 Alembic 迁移建立 Workspace thread/flow/fence、execution/pending/step ledger 与确定性 event/operation identity；使用 PostgreSQL CHECK、partial unique、复合外键和不可变 trigger 固化并发与恢复不变量，并完成含历史数据的 0009→0010→0009→0010 可逆迁移、checkpoint Schema 隔离和 491 项 API 回归。

可展开讲解：为什么 checkpoint 不能成为业务权威源；为什么稳定身份不包含 attempt/fence；为什么 partial unique 比普通 unique 更适合“同 Workspace 最多一个 live run”；如何用 sentinel 证明应用迁移没有误碰框架表。

## 5. 限制

- T28 只建立应用自有事实与身份工具；尚未实现 T29 的官方 PostgresSaver 初始化、账号、连接池、readiness 或 fenced saver adapter。
- 默认入口仍为 Legacy；没有生产 LangGraph invoke、interrupt/resume、跨进程恢复或真实轨迹证据。
- 完整迁移测试要求隔离 PostgreSQL 测试角色具备创建临时数据库权限；不应在 demo/业务数据库执行。
- 本文件只提供简历候选内容；正式简历仍需用户确认后才能修改。
