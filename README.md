# AccessPilot

AccessPilot 是一个完全使用虚构数据的企业系统访问申请 Agent。它演示员工如何通过多轮对话提交权限申请，并经过风险审查、人工审批和实际权限开通。

## 当前版本与进度

- **稳定基线：MVP v1.0** — T01–T08 已完成本地实现和验收；没有推送、部署或接入真实企业系统。
- **本地完成版本：v1.1** — T09–T17 已完成本地实现与验证；没有推送、合并、部署或接入真实企业系统。
- **当前本地版本：v1.2 精简版 T20 已完成并独立验收** — 四账号 AuthSession 之上已建立共享正式 Case 与资源级 SQL ACL；申请人重登、当前/已决定审批人和已批准 Case 管理员可按关系重读，草稿、聊天、事件和 Cursor 仍保持私有。T20 攻击矩阵 14/14，API 396 passed，前端 61 passed，当前产品固定评测 72/72。T21–T25 尚未开始；没有推送、合并或部署。
- **当前能力：** 产品工作台只接受 `accesspilot_session → AuthSession → EmployeeRecord` 身份链；旧 Workspace/Demo 入口返回 404，旧 Workspace cookie 单独访问返回 401。登录页明确标注“作品集 Mock 登录，非真实身份认证”，不实现密码、注册、OIDC 或真实 SSO。业务写请求要求精确 Origin 与内存 CSRF，Session 刷新轮换 CSRF，退出只吊销 Session、不删除 Workspace。其余能力包括多意图路由、只读工具白名单、确定性权限名称解析、8 条基本政策主题问答、基于本轮证据的 `grounded`/`insufficient_evidence`/`retrieval_unavailable` 三态政策回答、自审批禁止规则，以及当前轮增量 SSE、事件回放、取消、断线重连和刷新恢复。产品工作台还提供类型化业务卡片：权限按 `eligible`、`owned`、`pending`、`expiring_soon`、`expired` 五态展示；权限名称解析按 `matched`、`ambiguous`、`no_match` 三态展示候选和重新校验结果；政策按三态展示证据、提示和下一步；申请页按事实展示草稿、提交、风险审查、两级审批、开通与恢复时间线，断线时在业务页面显示可恢复的重连状态。
- **后续演进：** provider token 延迟、政策召回率和生产 SLA 尚未测量；它们不属于本地 deterministic_offline 单样本结论。

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
- T08：12 条固定评测、容器化配置和本地最终验收已完成。
- T09：Workspace 后端演示身份、身份迁移、刷新恢复和申请/审批越权守卫已完成；
- T10：按后端身份查询可自助申请权限与当前有效 `AccessGrant` 已完成；
- T11：7 类意图路由、只读工具执行器、咨询/草稿隔离、基础敏感信息拒绝与脱敏已完成。
- T12：历史产品身份与 Demo 控制面分层、Workspace/Cookie 隔离、预算边界和三档视口验收已完成；T19 已关闭旧 Demo 控制台和匿名入口；
- T13：自然语言权限名称经当前身份目录确定性解析，歧义与未知权限 fail-closed，业务字段变化使确认失效，已完成并本地验证；
- T14：当前轮增量 SSE、Workspace 回放后长连接、稳定游标与关联 ID、取消/唯一终态、刷新恢复和 Nginx 禁缓冲已完成并本地验证；
- T15：政策问答与提示词攻击防护、8 条基本政策主题、三态证据回答、自审批规则（`POL-006`）和复合攻击安全回归已完成并本地验证；模型输出、草稿、事件和错误正文均通过敏感信息边界检查。
- T16：权限、政策与申请状态业务卡片已完成；权限五态、名称解析三态、政策证据三态、申请生命周期时间线、加载/空/错误/安全拒绝和断线重连状态均已接入产品导航并本地验证。
- T17：v1.1 历史基线在 `c683d84` 冻结为 10 个场景、30/30；后端全量 339 passed（1 warning）、前端 14 files/48 passed、Ruff、MyPy（41 sources）、Alembic（无漂移/head 0006）、TypeScript app+node、ESLint 和正式构建均通过。脱敏单样本观测来自本机 direct-ASGI `deterministic_offline`：首事件 7.666417 ms、首个非空 `message.delta` 35.480167 ms、完成 39.017833 ms、计费模型调用 1 次；Workspace 重连回放 2 条、重复 0 条；复合攻击 4/4 阻断。上述延迟不是 provider token、p50/p95 或生产 SLA，不代表真实模型质量。
- T18：ConversationCursor、纯数字 `111` 期限上下文、失效/消费与 draft revision/CAS 已完成并独立验收；固定 T18 评测 11/11。
- T19：四账号 Mock Login、AuthSession/Principal、Origin/CSRF、Cookie、身份注入、旧入口关闭、Cursor session 绑定和 0008 迁移已完成；固定 T19-01 23/23；独立 verifier 结论 PASS（P0=0、P1=0）。
- T20：共享正式 Case、requester/approver/admin 资源级 SQL ACL、跨 Session 重读与私有 Workspace 边界已完成；固定 T20-01 14/14，当前产品 registry 72/72；独立 verifier 结论 PASS（P0=0、P1=0）。T21–T25 尚未开始。

登录页的四个账号只能证明会话隔离与 ACL 边界，不是真实 SSO 或生产级认证；访问者仍可选择任一虚构账号。真实系统仍必须由可信登录态确定操作者身份。

## 目录结构

```text
apps/api/                    FastAPI 后端、数据库迁移与 pytest 测试
apps/web/                    React + Vite 前端与组件测试
docs/                        产品规格、Tickets、架构、ADR 与学习资料
deploy/                      Nginx 等部署配置
scripts/                     启动、评测与本地验收脚本
compose.yaml                 前后端、PostgreSQL 的容器编排
pyproject.toml               Python 依赖与质量工具配置
package.json                 前端 Workspace 的统一命令入口
CONTEXT.md                   项目业务术语表
```

这是前后端分离的 Monorepo：React 和 FastAPI 分别位于 `apps/web` 与 `apps/api`，通过 HTTP API/SSE 通信，只是共享同一个 Git 仓库、文档和交付配置。详细职责见 [`apps/README.md`](apps/README.md) 与 [`docs/README.md`](docs/README.md)。教学记录集中在 [`docs/learning`](docs/learning/README.md)，不参与应用运行。

本地开发还会生成 `.venv/`、`node_modules/`、`postgres-data/`、测试缓存和浏览器测试缓存。这些目录已被 Git 忽略，只是依赖或本地运行数据，不属于产品源码，也不应合并进 `apps/`。

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

开发服务器会把 `/api` 和 `/health` 代理到 `http://127.0.0.1:8000`。浏览器使用 HttpOnly `accesspilot_session` cookie，CSRF 明文只保留在可读 cookie/内存中；开发 Origin 必须精确为 `http://127.0.0.1:5173`，不要混用 `localhost`。DeepSeek 与百炼密钥始终留在 FastAPI 的本地 `.env`。

## 固定评测与统一检查

T08 历史清单在 `c683d84` 有 12 条；在 T19 阶段，`eval01/05/06` 明确延期到 T20/T22（完整四角色黄金路径由 T24 恢复），旧 Workspace 隔离 `eval11` 已由 T19 的 AuthSession 隔离证据取代。当前 runner 只将其余 8 条 compatible 案例作为通过证据，不收集延期/取代项，也不以 skip 计入结果。`eval07/08` 使用预置 approved Case 与确定性 Sequenced/Counting IAM，继续真实验证 timeout→recover 和单次幂等开通，不用 404/409 替代旧语义。

v1.1 T17 历史记录为 30/30 @ `c683d84`。当前 T19 compatible/new registry 排除已被 T19 产品边界取代的 `T17-01` 和 `T17-09`，唯一计数为 active T17 24 + T18 11 + T19 23 = 58；这不是同分母历史对比：

```bash
./scripts/run-evals.sh
./scripts/verify-local.sh
```

T17 的脱敏评测摘要保存在 [`docs/evidence/accesspilot-v1.1-evaluation-2026-08-11.json`](docs/evidence/accesspilot-v1.1-evaluation-2026-08-11.json)，作品集证据和限制见 [`docs/evidence/accesspilot-v1.1-portfolio-evidence.md`](docs/evidence/accesspilot-v1.1-portfolio-evidence.md)，最终验收见 [`docs/reviews/accesspilot-t17-final-acceptance-review-2026-08-11.md`](docs/reviews/accesspilot-t17-final-acceptance-review-2026-08-11.md)。JSON 的 `source.git_revision=a45b5f7` 是 T16 基线加 T17 工作树运行来源；评测运行时不能预先写入包含自身文档修改的未来提交哈希。

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

当前仓库只提供可部署的本地 Compose 包，没有执行推送或部署。Compose 本地入口 Origin 是精确的 `http://127.0.0.1:8080`；真正放到公网前，必须在外层终止 TLS、设置 `ACCESSPILOT_AUTH_COOKIE_SECURE=true`、关闭数据库公开端口、使用密钥管理服务，并用真实可信登录态替换 Mock Login。

### 腾讯云部署前说明（本轮未执行）

- 服务器只开放必要的 SSH、80 和 443；Compose 中数据库端口继续绑定 `127.0.0.1`，不得加入公网安全组。
- 通过服务器的密钥管理或受控文件传输写入 `.env`，不要把真实 Key、数据库密码或服务器地址提交到 Git。
- 由宿主机 Nginx/Caddy 在 TLS 后代理到 `127.0.0.1:8080`，并保留本仓库针对 `/api/events` 的禁缓冲和长连接配置。
- TLS 生效后设置 `ACCESSPILOT_AUTH_COOKIE_SECURE=true`，并将 Compose 的 `ACCESSPILOT_COMPOSE_WEB_ORIGIN` 配置为唯一 HTTPS Origin，再验证 `/health`、`/ready`、SSE 重连和完整黄金路径。Vite 本地开发仍使用 `ACCESSPILOT_WEB_ORIGIN=http://127.0.0.1:5173`。
- 上线前补充 PostgreSQL 卷备份、日志采集、监控告警和回滚方案，并再次获得用户明确部署授权。

## 已知限制

- 全部人员、权限、政策和 IAM 都是原创虚构数据；IAM 是幂等模拟器，不连接真实企业系统。
- Mock Login 账号选择和客户端展示的 `employee_id` 只用于作品集演示，不是 SSO、身份认证或生产授权边界。
- 旧 Workspace cookie 不参与鉴权；Session、调用配额和数据隔离面向单机演示，不等于生产级多租户、防滥用或限流体系。
- 离线提取、向量和风险审查是透明的确定性回退，不等同于 DeepSeek/百炼真实效果。
- `/health` 只表示进程存活，`/ready` 才检查数据库；两者都不会调用外部模型或 IAM。
- 当前前端主包约 524 KB，T17 正式构建报告 561.02 KB chunk warning（P2）；不影响本地功能，但不应视为最终性能优化结果。
- T17 尚未测量 provider token latency、policy recall@k 或 production SLA；本地 deterministic_offline 单样本不能外推这些生产指标。
- 当前未配置备份、集中日志、监控告警、任务队列、高可用或线上部署。
- P2 延期边界：legacy JSON 路径以及 `set_actor`/`reset`/`exit_demo`/submit 等旧控制面仍是整行读写，极端并发下可能有最后写入者覆盖；SSE 当前轮锁已覆盖主路径。
- P2 延期边界：当前确定性适配器的流内容与规范化 `assistant_message` 一致；未来接入真实 Answer Provider 时仍需明确 canonical message 定义。
- P2 UX 限制：没有活动 Cursor 的数字澄清文案暂固定引用 `111`，不影响零副作用语义，但尚未做通用化文案。

## 架构文档

- [AccessPilot v1.1 产品功能书（历史基线）](docs/product/accesspilot-product-function-book-v1.1.md)
- [AccessPilot v1.2 精简产品说明书（T18–T20 已完成）](docs/product/accesspilot-product-function-book-v1.2.md)
- [访问申请与权限开通流程](docs/architecture/access-flow.md)
- [数据库 ER 图](docs/architecture/data-model-er.md)
- [项目实现计划](docs/plans/accesspilot-mvp.md)
- [术语表](CONTEXT.md)
- [架构决策记录](docs/adr/)
