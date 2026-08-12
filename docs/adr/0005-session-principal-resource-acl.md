# ADR-0005：由 AuthSession 派生 Principal，再按资源关系授权

- 状态：已接受
- 日期：2026-08-12
- 范围：T19、T20、T22、T23；AC-04–AC-07、AC-10–AC-12、AC-14

## 背景与问题

把 `employee_id`、`role` 或 `tenant_id` 放进请求体，或提供一个 actor 下拉框，只是在请求中“声明身份”，不能证明请求来自谁。v1.2 还需要让申请人、经理、数据负责人和权限管理员在不同 Session 中读取同一个正式 Case；私人草稿又不能因此变成共享数据。必须同时解决身份来源、Session 生命周期、资源级关系和旧 Cookie 的兼容风险。

## 决策与不变量

1. Mock Login 只接受四个固定虚构 `account_id`。登录事务原子创建私有 Workspace 和 AuthSession；`Principal` 从服务端 `EmployeeRecord` 派生，角色不接受客户端声明。
2. 除登录外，所有业务路由严格走 `accesspilot_session → AuthSession → Principal → Workspace`。旧 `accesspilot_workspace` Cookie 只是 locator，单独携带不能授权；请求体、query、form、Header 中的身份字段在副作用前拒绝。
3. Workspace 保存申请人私有草稿、对话和 Cursor。提交后的 Access Case 是共享正式事实，SQL ACL 只允许：请求人本人、被分配且角色匹配的审批人，或已全部批准 Case 的权限管理员。`workspace_id` 只用于绑定相关事实，不是授权本身。
4. Case 详情、列表、审批和开通都在服务端使用同一 Principal/资源关系查询；无关系对象统一按“申请不存在”处理，避免 IDOR 和存在性泄露。Session 注销只吊销登录，不删除正式 Case。

## 被拒绝的替代方案

| 方案 | 拒绝理由 |
|---|---|
| actor 下拉框或 body 中的 `employee_id/role` | 客户端可以直接冒充经理或管理员；字段校验不能替代可信身份来源。 |
| 把 Workspace Cookie 当作权限 | Cookie 是空间定位符，不表达“谁与 Case 有关系”；旧 Cookie、复制 Cookie 或跨窗口都可能越权。 |
| 只按全局 RBAC 放行 | “manager” 角色不代表能读任意 Case；需要具体 requester/approver/approved-admin 关系，并且要能撤销单个 Session。 |
| 把 ACL 完整编码在长期客户端 JWT | 关系变更、注销和审批状态难以及时收回；服务端 SQL 关系更易审计和保持事实一致。 |

## 结果、代价与限制

- 结果：四个独立 Session 能围绕同一个 Case 协作，私有草稿仍隔离；越权请求在业务写入前稳定拒绝。
- 代价：每个业务入口都必须携带 Principal 上下文，Case 查询需要相关 `EXISTS` 子查询和角色匹配；统一 404 会牺牲部分“为什么不能看”的交互细节。
- 限制：四个账号是 Mock Login，不是 OIDC/SAML、密码或企业目录；ACL 是单实例、虚构目录的资源关系演示，不宣称生产多租户或 Tenant 开户。

## 失败、恢复与回滚

- 缺失、篡改、过期或注销 Session：返回 401，不进入 Workspace 或业务逻辑；重新登录只创建新 Session，原 Case 保留。
- 误传身份字段、Origin/CSRF 不正确：在 Pydantic/业务副作用前返回 422/403，草稿、Case、IAM 计数不变。
- 已登录但无 Case 关系：详情/收件箱按 404；角色不匹配、越序或自审批按 403/409；不写入审计以外的业务事实。
- 若未来切换到 OIDC，应保留 `Principal` 和 Case ACL 作为不变量，仅替换 AuthSession 的建立/验证；切换失败时回退到 Mock Login，不把客户端字段重新提升为身份。

## 可核验链接

| 证据 | 链接与定位 |
|---|---|
| Mock Login、Principal、Session 绑定 | [`auth.py`](../../apps/api/src/accesspilot/auth.py#L16-L170)；[`require_workspace`](../../apps/api/src/accesspilot/main.py#L680-L705) |
| 身份注入、旧入口与 CSRF 边界 | [`auth_boundary` middleware](../../apps/api/src/accesspilot/main.py#L519-L621) |
| Case 资源关系 SQL ACL | [`_case_acl_request_query`](../../apps/api/src/accesspilot/operations.py#L517-L541)；[`_case_acl_relation`](../../apps/api/src/accesspilot/operations.py#L601-L637) |
| T19 会话隔离/注入矩阵 | [`test_t19_auth.py`](../../apps/api/tests/api/test_t19_auth.py#L54-L250)；[`test_t19_auth.py`](../../apps/api/tests/api/test_t19_auth.py#L261-L500) |
| T20 Case ACL 与注销重读 | [`test_t20_case_acl.py`](../../apps/api/tests/api/test_t20_case_acl.py#L225-L325)；[`test_t20_case_acl.py`](../../apps/api/tests/api/test_t20_case_acl.py#L469-L535) |
| 跨角色审批与攻击矩阵 | [`test_t22_approval_lifecycle.py`](../../apps/api/tests/api/test_t22_approval_lifecycle.py#L153-L240)；[`test_t24_product_verification.py`](../../apps/api/tests/api/test_t24_product_verification.py#L267-L319) |
| 运行与边界 | [`v1.2 Product Manifest`](../evidence/accesspilot-v1.2-product-manifest.md#边界)；[`T19/T20 Ticket 验收记录`](../tickets/accesspilot-productized-agent-v1.2.md#t19--独立-mock-登录页、authsession-与-principal) |
