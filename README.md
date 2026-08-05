# AccessPilot

AccessPilot 是一个完全使用虚构数据的企业系统访问申请 Agent。它演示员工如何通过多轮对话提交权限申请，并经过风险审查、人工审批和实际权限开通。

## 项目目标

- 多轮对话补全结构化申请草稿；
- 使用 Structured Output 和 Tool Calling；
- 通过状态机管理确认、审批、开通和错误恢复；
- 使用主 Agent 与只读风险审查 Agent 协作；
- 使用 RAG 检索政策条款并返回稳定的政策编号；
- 区分“审批通过”和“权限已经开通”；
- 记录审计事件，并通过幂等键安全重试。

所有员工、系统、权限、审批和政策内容都是原创虚构数据，与任何真实企业内部数据无关。

## 当前进度

- T01：DeepSeek 严格结构化提取、确认提交守卫、正式申请与审计事务已完成；
- T02：百炼/离线 512 维向量、pgvector Top 4 和只读风险审查已完成；
- T03：直属经理到数据所有者的串行审批、越权/乱序/重复守卫与审计已完成；
- T04：幂等 IAM 开通、失败重试、超时未知查询恢复和唯一授权已完成；
- T05：安全事件白名单、SSE `Last-Event-ID` 回放、对话入口和 Workspace 模型配额已完成；
- T06：assistant-ui `LocalRuntime + ChatModelAdapter`、申请草稿卡、明确确认、正式提交与刷新恢复已完成；
- T07：按当前演示身份过滤的审批收件箱、政策/审批/开通/审计详情、故障恢复与只读回放已完成；
- T08：12 条固定评测、容器化配置和本地最终验收已完成，等待用户最终验收。

当前角色选择器和 `actor_id` 只用于虚构 Demo 流程，不是生产级身份认证或授权边界；真实系统必须由可信登录态在服务端确定操作者身份。

## 目录结构

```text
apps/api/src/accesspilot/    Python API 与领域代码
apps/api/tests/              后端 pytest 测试
apps/web/                    React + Vite 前端
docs/                        产品计划、架构流程和 ADR
CONTEXT.md                   项目术语表
```

## 本地开发

需要 Python 3.11+、Node.js 22+、pnpm 11，以及启用了 pgvector 的 PostgreSQL。先创建只保存在本机的配置：

```bash
cp .env.example .env
```

`.env` 已被 Git 忽略。没有配置 DeepSeek/百炼 Key 时，后端会明确使用确定性离线适配器；它适合本地流程和测试，但不代表真实模型质量。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

alembic upgrade head
python -m accesspilot.bootstrap
uvicorn accesspilot.main:app --host 127.0.0.1 --port 8000
```

`accesspilot.bootstrap` 会幂等写入全部虚构目录，并用当前配置的百炼适配器或离线适配器重建政策向量。另开终端运行检查：

```bash
source .venv/bin/activate

pytest apps/api/tests -v
ruff check apps/api/src apps/api/tests
mypy apps/api/src
alembic check
```

集成测试需要本地 PostgreSQL/pgvector；DeepSeek 与百炼测试默认使用假客户端或确定性离线适配器，不消耗真实额度。真实密钥只能放在本地 `.env`，不得提交到 Git：

```dotenv
DEEPSEEK_API_KEY=你的本地密钥
DASHSCOPE_API_KEY=你的本地密钥
```

需要手动验证真实适配器时运行：

```bash
python -m accesspilot.smoke dashscope-embedding
python -m accesspilot.smoke deepseek-risk
```

Smoke 输出只包含安全业务结果或向量维度，不打印密钥和完整向量。

前端使用 Node.js 20.19+ 与 pnpm：

```bash
pnpm install
pnpm --filter @accesspilot/web test
pnpm --filter @accesspilot/web lint
pnpm --filter @accesspilot/web build
pnpm --filter @accesspilot/web exec vite --host 127.0.0.1 --port 5173
```

开发服务器会把 `/api` 和 `/health` 代理到 `http://127.0.0.1:8000`。浏览器只持有 HttpOnly Workspace cookie；DeepSeek 与百炼密钥始终留在 FastAPI 的本地 `.env`。

## 固定评测与统一检查

12 条固定场景覆盖黄金路径、缺项、未确认、政策失败、审批乱序、驳回、IAM 超时恢复、重复幂等调用、SSE 重连、配额、Workspace 隔离和模型结构错误：

```bash
./scripts/run-evals.sh
./scripts/verify-local.sh
```

统一脚本运行后端全量测试、Ruff、MyPy、Alembic 漂移检查、前端测试、ESLint、TypeScript/生产构建和固定评测。Docker 配置及 1440px/390px 真实浏览器检查仍需单独执行并保留证据。

## 本地容器运行

请先在 `.env` 中替换 `POSTGRES_PASSWORD`，并让 `ACCESSPILOT_COMPOSE_DATABASE_URL` 使用同一密码。若密码含 `@`、`:`、`/` 等 URL 保留字符，需要在连接串中做百分号编码；Compose 不会直接拼接原始密码。Docker Compose 会按顺序启动 pgvector、运行 Alembic、写入目录与政策向量、启动 API，再由非 root Nginx 提供前端和 SSE 反向代理：

```bash
docker compose config --quiet
docker compose build
docker compose up -d

curl --fail http://127.0.0.1:8080/health
curl --fail http://127.0.0.1:8080/ready
docker compose ps
```

浏览器访问 `http://127.0.0.1:8080`。普通停止使用 `docker compose down`，它会保留 PostgreSQL 具名卷；只有明确要删除全部本地 Demo 数据时才使用 `docker compose down -v`。

当前仓库只提供可部署的本地 Compose 包，没有执行推送或部署。真正放到公网前，必须在外层终止 TLS、设置 `ACCESSPILOT_WORKSPACE_COOKIE_SECURE=true`、关闭数据库公开端口、使用密钥管理服务，并用服务端可信登录态替换演示 `actor_id`。

### 腾讯云部署前说明（本轮未执行）

- 服务器只开放必要的 SSH、80 和 443；Compose 中数据库端口继续绑定 `127.0.0.1`，不得加入公网安全组。
- 通过服务器的密钥管理或受控文件传输写入 `.env`，不要把真实 Key、数据库密码或服务器地址提交到 Git。
- 由宿主机 Nginx/Caddy 在 TLS 后代理到 `127.0.0.1:8080`，并保留本仓库针对 `/api/events` 的禁缓冲和长连接配置。
- TLS 生效后设置 `ACCESSPILOT_WORKSPACE_COOKIE_SECURE=true`，再验证 `/health`、`/ready`、SSE 重连和完整黄金路径。
- 上线前补充 PostgreSQL 卷备份、日志采集、监控告警和回滚方案，并再次获得用户明确部署授权。

## 已知限制

- 全部人员、权限、政策和 IAM 都是原创虚构数据；IAM 是幂等模拟器，不连接真实企业系统。
- 角色选择器和客户端 `actor_id` 只用于 Demo，不是 SSO、身份认证或生产授权边界。
- Workspace cookie、调用配额和数据隔离面向单机演示，不等于生产级多租户、防滥用或限流体系。
- 离线提取、向量和风险审查是透明的确定性回退，不等同于 DeepSeek/百炼真实效果。
- `/health` 只表示进程存活，`/ready` 才检查数据库；两者都不会调用外部模型或 IAM。
- 当前前端主包约 524 KB，Vite 会提示后续可按页面拆包；不影响 MVP 功能，但不应视为最终性能优化结果。
- 当前未配置备份、集中日志、监控告警、任务队列、高可用或线上部署。

## 架构文档

- [访问申请与权限开通流程](docs/architecture/access-flow.md)
- [数据库 ER 图](docs/architecture/data-model-er.md)
- [项目实现计划](docs/plans/accesspilot-mvp.md)
- [术语表](CONTEXT.md)
- [架构决策记录](docs/adr/)
