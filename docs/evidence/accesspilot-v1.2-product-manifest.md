# AccessPilot v1.2 Product Manifest

- 验证日期：2026-08-12
- 产品代码 revision：`eec27d7`
- 运行档案：本地 PostgreSQL + FastAPI + React；四个 Mock Login 账号；模拟 IAM
- 结论：`product_verified=true`
- 面试准备：`interview_ready=false/pending_user_verification`（T25 证据产物已完成，仍需用户本人无稿演练与两道迁移题）

## 质量门禁

`./scripts/verify-local.sh` 在同一实现 revision 上通过：

- API 全量：425 passed（1 条第三方 LangGraph 弃用预警）；
- Ruff、MyPy 44 source files；
- Alembic head `20260812_0009`，`alembic check` 无漂移；
- Web：17 files / 87 tests，ESLint、TypeScript 与 production build 通过；
- 固定评测：15 个场景、101/101 cases，selector 无重复；
- 已知非阻塞：Web 主 chunk 约 571 kB，属本地作品集性能优化项。

脱敏固定评测报告：`/private/tmp/accesspilot-product-evaluation.json`（本机运行产物，不入库）。

## AC-01–AC-13

| AC | 结果 | 主要证据 |
|---|---|---|
| 01 | PASS | `test_t18_cursor.py::test_duration_cursor_accepts_111_up_to_catalog_limit_without_model` |
| 02 | PASS | T18 无 Cursor、越上限、非期限与非法数字矩阵 |
| 03 | PASS | T18 Cursor 失效、CAS、JSON/SSE Outcome 一致性测试 |
| 04 | PASS | `test_t19_auth.py` 四个独立 AuthSession/Workspace |
| 05 | PASS | T19 Origin/CSRF/Cookie/身份注入与旧 Demo 路由攻击矩阵 |
| 06 | PASS | `test_t20_case_acl.py` 正式 Case 跨 Session、私有草稿/对话/Cursor |
| 07 | PASS | T20/T22 IDOR、错角色、越序、自审、并发拒绝 |
| 08 | PASS | T21 唯一冻结 Decision Packet、四类来源和版本 |
| 09 | PASS | T21 no-key/timeout/schema/citation 失败均显式 `unavailable` |
| 10 | PASS | `test_t22_approval_lifecycle.py` EMP-002 → EMP-003 串行审批 |
| 11 | PASS | `test_t23_admin_provisioning.py` 仅 EMP-004、稳定幂等键、unknown 恢复和唯一 Grant |
| 12 | PASS | `test_t24_product_verification.py` 申请人注销重登后读同一 Case/Grant |
| 13 | PASS | 全量门禁、四角色浏览器主链、三视口与键盘烟测 |

## 真实浏览器证据

使用 Playwright CLI 的独立命名浏览器 Session，串行执行以避免命令交叉：

1. `emp001`：EMP-001 登录，补全并确认申请，创建 Packet，显式启动审批；
2. `emp002`：读到当前 pending 步骤并批准；
3. `emp003`：经理批准后才读到 pending 步骤并批准；
4. `emp004`：读到 approved Case，执行开通，页面显示唯一 Grant；
5. EMP-001 注销后重新登录，在「我的申请」中重读本次已批准 Case 和同一 Grant。

本次浏览器 Case：`d5ecb751-215d-4c4b-91ba-c2f62d5da466`；Grant：`f94e27d7-4177-4951-8e03-66b48fc9f6ef`。它们只是本地虚构运行证据，不是生产对象。

视口/键盘烟测：

- 1440×900：管理员 Grant 详情可见，`scrollWidth=1440`；
- 1024×768：申请历史页 `scrollWidth=1024`；
- 390×844：`scrollWidth=390`，Tab + Enter 成功选择 EMP-001 登录。

## 攻击矩阵映射

| 攻击 | 结果/证据 |
|---|---|
| Cookie 篡改/过期/注销 | T19 auth 测试；T24 长 SSE 逐事件重验 Session |
| 缺/错 Origin 与 CSRF | T19 交叉矩阵，零写入 |
| body/query/form/header 身份注入 | T19 recursive/camel/multipart/+json 测试 |
| 无关系 Case IDOR | T20 existing/random 等价 404 |
| 旧 Workspace Cookie/匿名 ensure | T19/T24 返回 401/404 |
| EMP-003 越过经理 | T22/T24 返回 409、零写 |
| EMP-001 自审 | T22/T24 无关系 404、零写 |
| EMP-002/003 开通 | T23/T24 返回 403、IAM 零调用 |
| Packet 缺失时启动审批 | T21 返回 409、路线零变化 |
| 未全批准时开通 | T23/T24 对 admin 统一 404 |
| 重复/并发审批与开通 | T22/T23 行锁 + expected step/服务端 key |
| 模型超时/无 Key/非法输出/越权路线 | T21 输出 unavailable，固定路线不变 |
| `111` 错误/失效上下文 | T18 零草稿/确认/工具/模型副作用 |
| 旧 Demo actor 切换 | T19 产品档案 404 |
| 注销/过期误删 Case | T19/T20 新 Session 重读正式事实 |
| GET 意外生成 Packet/调模型/IAM | T20/T21/T24 调用计数与业务快照不变 |

## 评测 lineage

- T17 历史基线仍是 `c683d84` 的 30/30，不与当前分母混比；
- T08 `eval02/03/04/07/08/09/10/12` 保留同类语义；
- T08 黄金主链 `eval01` 迁移为 T24 四角色集成测试；
- T08 越序/驳回 `eval05/06` 由 T22 真实资源审批测试替代；
- T08 旧 Workspace 隔离 `eval11` 由 T19 AuthSession + T20 Case ACL 替代，不重复计数。

## 边界

- Mock Login 不是 OIDC/SAML 或密码认证；
- IAM 是确定性模拟器，不是真实企业开通系统；
- 未实现 Tenant 开户、到期回收/撤销、生产 SLA 或生产多租户；
- 无 Key 档案已用固定测试验证 `unavailable`。本地浏览器观察到 provider Packet，但未做质量基准，不形成真实 DeepSeek 质量或 SLA Claim；
- 本次仅本地运行，未推送、合并或部署。
