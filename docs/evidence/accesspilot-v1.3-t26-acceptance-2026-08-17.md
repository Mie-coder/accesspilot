# AccessPilot v1.3 T26 验收证据

**Ticket：** T26 — LangGraph 依赖兼容性 Spike 与版本锁定
**基线 revision：** `83a9293`
**日期：** 2026-08-16–17
**状态：** `Verified`；Claude Code + DeepSeek 独立验收均无 P0/P1，已在本 revision 本地提交

## 1. T26 提交边界

实现/验证文件：

- `.env.example`
- `Dockerfile.api`
- `pyproject.toml`
- `constraints/python.lock`
- `scripts/verify-local.sh`
- `scripts/t26_langgraph_compat.py`
- `scripts/verify-t26-langgraph.sh`
- `apps/api/tests/agent/test_t26_langgraph_compat.py`

T26 计划、评审与证据文件：

- `docs/specs/accesspilot-langgraph-agent-loop-v1.3.md`
- `docs/tickets/accesspilot-langgraph-agent-loop-v1.3.md`
- `docs/reviews/accesspilot-v1.3-langgraph-spec-review-package-2026-08-16.md`
- `docs/reviews/accesspilot-v1.3-spec-claude-review-2026-08-16.md`
- `docs/reviews/accesspilot-v1.3-spec-deepseek-review-2026-08-16.md`
- `docs/reviews/accesspilot-v1.3-spec-review-decisions-2026-08-16.md`
- `docs/reviews/accesspilot-v1.3-t26-acceptance-review-package-2026-08-16.md`
- T26 验收评审结果/决策记录
- `docs/evidence/accesspilot-v1.3-change-ledger.md`
- 本文件

明确排除：用户原有 `.gitignore`、`README.md`、`.codebase-map/`、`scripts/start-local.sh`，以及未提交的 `AgentTrajectory`/`App`/样式/前端测试原型。

## 2. 可复现验证入口

数据库变量必须指向一次性隔离测试库；本文不保存凭据：

```sh
export ACCESSPILOT_T26_DATABASE_URL='postgresql+psycopg://<test-user>:<test-password>@127.0.0.1:<test-port>/<test-db>'
export ACCESSPILOT_DATABASE_URL="$ACCESSPILOT_T26_DATABASE_URL"
export ACCESSPILOT_TEST_DATABASE_URL="$ACCESSPILOT_T26_DATABASE_URL"
export LANGGRAPH_STRICT_MSGPACK=true

./scripts/verify-t26-langgraph.sh
./scripts/verify-local.sh
.venv/bin/python -m ruff check scripts/t26_langgraph_compat.py apps/api/tests/agent/test_t26_langgraph_compat.py
.venv/bin/python -m pip check
git diff --check
```

全新 Python 3.11 环境：

```sh
python3.11 -m venv <fresh-venv>
<fresh-venv>/bin/python -m pip install --constraint constraints/python.lock '.[dev]'
T26_PYTHON_BIN=<fresh-venv>/bin/python ./scripts/verify-t26-langgraph.sh
<fresh-venv>/bin/python -m pip check
```

Docker 解析：

```sh
docker build -f Dockerfile.api -t accesspilot-api:t26 .
docker run --rm --entrypoint python accesspilot-api:t26 <lock-comparison-script>
```

`lock-comparison-script` 对镜像中 `importlib.metadata.distributions()` 与 `/app/constraints/python.lock` 做 canonical-name/version 对比，只忽略项目包本身、`pip` 和 `setuptools`。

## 3. TDD 与定向结果

- 红灯：旧 `.venv` 首次执行 T26 测试得到 `1 failed`；失败精确指向 `langgraph 0.6.11 != 1.2.11`、`langgraph-checkpoint 3.0.1 != 4.2.0`、缺少 `langgraph-checkpoint-postgres` 和 `psycopg-pool`。
- 绿灯：隔离真实 PostgreSQL probe 输出 `status=passed`，随后 T26 测试 `9 passed`。
- probe 已验证：strict msgpack、URL 安全转换、同步 PostgresSaver、连接池参数、自定义 schema/search path、`setup()` 两次幂等、root namespace fail-fast、per-run thread 隔离、exact old head 不选 latest、missing/wrong-run fail-closed、跨进程 interrupt resume、`put → put_writes` 顺序和 fenced candidate promotion。
- probe 使用 UUID 临时 schema，在 `finally` 中 `DROP SCHEMA ... CASCADE`；不触碰 demo/业务数据。

## 4. 环境一致性

- 当前 Python 3.11 `.venv`：已安装包相对 lock 的 `installed_unlocked=[]`、`version_mismatches={}`，`pip check` 为 `No broken requirements found`。
- 全新 Python 3.11 虚拟环境：按 constraints 安装成功，真实 PostgreSQL probe 与 `9 passed`，`pip check` 通过。
- Python 3.12.14 Linux Docker：`LANGGRAPH_STRICT_MSGPACK=true`，`installed_unlocked=[]`、`version_mismatches={}`。
- 五个候选核心包在三环境中精确一致；平台条件依赖按同一 lock 解析。`greenlet==3.5.5` 是 Docker/Linux 必需的条件依赖，macOS 未安装它不属于版本漂移。

## 5. T26-only 基线归因

从 `83a9293` 导出临时快照，只覆盖第 1 节的 T26 实现/验证文件，不包含当前未提交轨迹 UI 原型。结果：

- API pytest：`451 passed`；
- Ruff：`All checks passed!`；
- MyPy：`Success: no issues found in 44 source files`；
- Alembic：`No new upgrade operations detected.`；
- v1.2 前端 Vitest：`18 passed` test files，`95 passed` tests；
- ESLint、两份 TypeScript no-emit 检查和 Vite production build：通过；
- 固定评测：`8 passed`；
- T17–02至 T24–01 所有产品评测场景：全部通过，脱敏报告保存于临时目录。

当前混合工作树也通过 API `451 passed` 与前端 `100 passed`，但后者包含未提交轨迹 UI 原型，不用于 T26 归因。

## 6. 限制

- T26 只是候选矩阵与恢复语义 Spike；生产 saver adapter、execution/pending ledger、fence 和真实 Agent Loop 属于后续 Ticket。
- probe 中的 `t26_head_acceptance` 是临时模拟表，不是生产实现。
- 此证据支持“兼容性 Spike 与版本锁定”，不支持“AccessPilot 主链已接入 LangGraph”、“生产级高可用”或“已部署”。
