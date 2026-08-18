# AccessPilot v1.3 T29 验收证据

**Ticket：** T29 — PostgreSQL Checkpointer 初始化、账号与运行生命周期  
**实现基线 revision：** `0553cad`  
**日期：** 2026-08-17  
**状态：** `Verified`；独立只读验收 AC1–AC3 全部 PASS，P0/P1/P2 均为 0，等待本 Ticket 本地提交

## 1. 已验证改变

- 新增唯一显式 checkpoint 初始化入口，顺序为 AccessPilot Alembic → 官方 `PostgresSaver.setup()` → readiness；普通 API 启动和请求路径不再执行 Alembic 或 saver setup。
- checkpoint 使用独立 schema 与 migration/runtime 两个实际不同的 PostgreSQL 角色；runtime 只有必要 DML/USAGE 权限，不能 CREATE/ALTER/DROP、创建数据库或使用 TEMP。
- legacy 模式不创建 checkpoint pool；mixed/langgraph 配置下 `/health` 保持存活，checkpoint 缺失、未初始化、无权限或不可达时 `/ready=503`。
- 每个 App 实例复用并关闭自己的同步连接池；多 App 互不干扰，重启后可读取既有 checkpoint。
- fenced saver 只接受服务端 execution/fence context 和完整 exact locator；root namespace 非空、错 run、错 fence、缺失 head 与 implicit latest 均 fail closed。

## 2. 精确恢复点合同

- 官方 saver `put` 返回值只保存在一次 invocation 的 candidate state，不会立即改写应用 accepted head。
- candidate 记录 immediate parent/accepted lineage；旧分支不能在 accepted head 已前进后覆盖当前恢复点。
- finalize 必须验证精确 candidate、父 head、图已停止状态，再以匹配 execution/workspace/input turn/running/fence/旧 head 的 SQL CAS 提升。
- 真实 PostgreSQL 测试证明：half-write、stale fence 和 official latest 指向的孤儿 head 均不能提升；重启后 scoped reader 只读取应用 accepted exact head。
- facade 显式委托官方 `PostgresSaver.get_next_version()`，保留其字符串 channel version 与并发语义；不通过通用 `__getattr__` 暴露 latest/list/delete 逃生口。

## 3. 验收结果

| 验收标准 | 结果 | 核心证据 |
|---|---|---|
| 初始化顺序、幂等与双账号权限 | PASS | setup×2 指纹稳定；实际 current_user/role 属性/DML/DDL/TEMP 反证；Alembic no-drift |
| App pool、readiness、Legacy 零依赖 | PASS | 双 App 互不影响、关闭/restart、setup fail-if-called、health/ready 矩阵 |
| exact fenced saver 与 candidate→accepted | PASS | 真实小 StateGraph、parent lineage、half-write/latest orphan/stale fence、官方版本委托 |

验证数字：

- T29 单元：`12 passed`；
- Legacy、JSON/SSE 与重启相关回归：`44 passed`；
- T29 真实隔离 PostgreSQL：`1 passed`，临时数据库/角色残留 `0/0`；
- 完整 API：`502 passed, 2 skipped`；两项 skip 是 T26/T29 专用真实 PG 环境门控，其中 T29 已按上一项单独通过；
- Ruff、MyPy（47 个 source files）与 `git diff --check`：通过。

## 4. 评审闭环

独立预审先发现 candidate 缺少父 accepted head 约束、通用属性透传可能绕过 exact guard，以及 half-write/latest orphan 反证不足；实现补齐 lineage、最小 facade 和真实 PG 场景。最终复验又发现 facade 未委托官方 `get_next_version()`，会静默退化成 Base saver 的整数版本；补充委托与官方字符串格式测试后，AC3 通过。

## 5. 简历与面试价值

候选表述：

> 为 AccessPilot 接入官方 PostgreSQL Checkpointer 运行层，拆分 migration/runtime 最小权限账号与显式初始化流程，建立每 App 连接池复用、readiness 和可关闭生命周期；通过 exact checkpoint locator、parent lineage 与 fenced CAS 将 saver head 区分为 candidate/accepted，真实验证 half-write、stale fence 和 latest orphan 均不能覆盖已接受恢复点，保持完整 API 502 项回归通过。

可展开讲解：为什么 Legacy 必须对 checkpoint 零依赖；为什么 `put` 不是 accepted；为什么不能读取 official latest；适配器为何必须保留官方 channel version 语义；最小权限账号怎样用真实失败反证，而不是只看配置。

## 6. 限制

- 数据库的 pgvector 扩展由平台/管理员预先安装；非 superuser migration role 只执行应用 Alembic 和 checkpoint setup，不负责安装扩展。
- T29 只交付 saver 生命周期、exact guard 与 CAS primitive；T33 才实现 lease、heartbeat、advisory lock、过期接管和所有业务写 fence。
- 默认产品入口仍为 Legacy；T30 生产图、interrupt/resume、跨进程业务恢复和真实轨迹尚未完成。
- 初始化和账号权限只在隔离 PostgreSQL 验收，尚未部署到生产环境。
- 本文件只提供简历候选内容；正式简历仍需用户确认后才能修改。
