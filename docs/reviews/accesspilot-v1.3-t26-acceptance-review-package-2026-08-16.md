# AccessPilot v1.3 T26 独立验收评审包

**评审类型：** `final-acceptance`（仅 T26 Ticket）
**日期：** 2026-08-16
**基线：** AccessPilot v1.2，开始 revision `83a9293`
**当前状态：** 评审已完成；Claude Code + DeepSeek 均无 P0/P1，T26 `Verified`
**证据归档：** `docs/evidence/accesspilot-v1.3-t26-acceptance-2026-08-17.md`
**决策记录：** `docs/reviews/accesspilot-v1.3-t26-acceptance-decisions-2026-08-17.md`

## 1. 目标与非目标

T26 只证明并锁定 LangGraph/PostgresSaver/psycopg 候选矩阵，用隔离真实 PostgreSQL 验证后续生产接入所依赖的恢复语义。

T26 **不**把 LangGraph 接入生产 JSON/SSE 主链，不实现生产 saver/fence/execution ledger，不实现 HITL 业务确认，也不修改正式简历。

## 2. 二元验收标准

1. 全新 Python 环境、本地环境和 Docker 解析同一组精确版本，且有可复现约束文件。
2. strict msgpack、同步 PostgresSaver、自定义 schema/search path、连接池、幂等 setup、root namespace fail-fast、per-run checkpoint thread、exact-head fail-closed、interrupt 跨进程 resume 和 candidate→accepted fence 前置条件全部通过。
3. 只包含 T26 改动的快照中，v1.2 后端/前端/评测基线全绿。

## 3. 实现与合同修正

- 精确锁定 `langgraph==1.2.11`、`langgraph-checkpoint==4.2.0`、`langgraph-checkpoint-postgres==3.1.2`、`psycopg==3.3.4`、`psycopg-pool==3.3.1`；
- 新增 `constraints/python.lock`，本地和 Docker 都使用该约束；Docker 实测发现并补锁 Linux-only `greenlet==3.5.5`；
- 强制在导入 LangGraph 前设置 `LANGGRAPH_STRICT_MSGPACK=true`；
- 新增隔离 probe 与测试：`scripts/t26_langgraph_compat.py`、`scripts/verify-t26-langgraph.sh`、`apps/api/tests/agent/test_t26_langgraph_compat.py`；
- 实测发现 root `checkpoint_ns` 必须为 `""`，因此改为每 graph run 独立 `checkpoint_thread_id`；
- 实测发现 dynamic interrupt 持久顺序为 `put(head) → put_writes(__interrupt__)`，因此改为 candidate 精确状态校验后再 fenced 提升 accepted head。

## 4. 已有证据

- TDD 红灯：旧环境精确失败，显示 LangGraph/checkpoint 版本不符且缺少 checkpoint-postgres/pool；
- 真实 PostgreSQL：独立 schema 的 probe 输出 `status=passed`，T26 测试 `9 passed`；临时 schema 在 `finally` 中删除；
- 全新 Python 3.11 虚拟环境：按 lock 安装、`pip check`、真实 PostgreSQL probe 与 9 项测试通过；
- Docker Python 3.12.14：`LANGGRAPH_STRICT_MSGPACK=true`，已安装包相对 lock 的 `installed_unlocked=[]`、`version_mismatches={}`；
- T26-only 临时快照：后端 `451 passed`，Ruff/MyPy/Alembic no-drift 通过；v1.2 前端 `95 passed`，ESLint/TypeScript/Vite build 通过；固定评测 8 项与 T17–T24 产品评测全部通过；
- 当前混合工作树（含用户未提交轨迹 UI 原型）也通过后端 `451 passed` 和前端 `100 passed`，但该数据不用于 T26-only 归因；
- `ruff check` 覆盖 T26 probe/test，`git diff --check` 和当前 `.venv` 的 lock 一致性/`pip check` 通过。

## 5. 已知限制

- probe 里的 fence 表是隔离模拟，生产 execution/pending ledger 和 saver adapter 属于 T28/T29/T33；
- T26 证明“候选矩阵可用”，不证明“AccessPilot 主链已迁移”或“生产高可用”；
- 正式简历保持未修改；只在变更账本中保留可验证的候选面试表述。

## 6. 聚焦评审问题

T26 的 3 条验收标准是否均有足够、可复现的证据？实现、测试、Spec/Ticket 合同或变更账本中，是否仍有阻止把 T26 标记为 `Verified` 并做本地提交的 P0/P1？

请按 `问题 → 证据或原因 → 影响 → 最小改进建议` 输出，并区分 P0/P1/P2/P3。评审只读，不修改文件。
