# AccessPilot v1.2「上下文意图与可信多角色闭环」Spec（精简版）

**状态：** 用户已于 2026-08-11 确认按 T18 → T25 串行实施；T18 已完成并通过独立验收；T19–T25 待开始；不推送、合并或部署
**更新时间：** 2026-08-11
**上游产品定义：** `docs/product/accesspilot-product-function-book-v1.2.md`
**已验证基线：** AccessPilot v1.1，本地提交 `c683d84`
**历史交叉评审：** `docs/reviews/accesspilot-v1.2-spec-deepseek-review-2026-08-11.md`，仅适用于精简前草案

## 0. 确认与开发边界

本 Spec 取代同路径下旧的扩张版 v1.2。用户已确认按 T18 → T25 串行实施；当前仅 T18 已落地并通过独立验收，T19–T25 仍待开始。

当前交付边界：

- 不把 T19–T25 的目标能力声明为已实现；
- 不修改正式简历；
- 不推送、合并或部署。

此前已接受的 DeepSeek 意见，只在不与本精简范围冲突时继续有效。本 Spec 尚未针对精简范围重新进行外部模型评审。

## 1. 目标与完成定义

### 1.1 P0 目标

1. 让纯数字续答根据服务端期限上下文获得可解释的结果，先闭合用户输入 `111` 的实际问题，不承诺通用短句理解；
2. 用独立 Mock 登录、AuthSession 和 Principal 取代产品内直接切换 EMP 身份；
3. 让四个独立账号围绕同一个正式 Case 完成申请、两级审批、显式开通和重新登录重读；
4. 增加简化 Decision Packet，使事实、政策、用户说明和模型建议来源可核验；
5. 用固定测试、攻击矩阵和面试证据证明边界，不扩大为生产 IAM。

### 1.2 完成结论

- `product_verified` 只能由 T24 在 AC-01–AC-13 全部通过后设置；
- `interview_ready` 只能由 T25 在 AC-14 与个人讲解检查通过后设置；
- 两者互相独立，代码全绿不自动证明候选人会解释。

## 2. 非目标优先级

本版本明确不实现：

- 到期回收、RevocationOperation 或回收 Attempt；
- Tenant 预算桶、复杂 ModelInvocation 对账或生产 Worker；
- Evidence Lab、产品内故障注入或测试时钟；
- 完整 Workspace 外键 cutover；
- 通用多租户平台；
- 真实 OIDC/SAML/LDAP/IAM；
- Async Answer Provider 与原始 Provider Streaming；
- 168 小时延迟复测协议。

若实现中发现必须依赖上述任一项，先回到产品说明书和本 Spec 重新确认，不在 Ticket 内隐式扩张。

## 3. 术语

- **Mock Login：** 仅用于虚构账号的登录入口，不代表真实企业认证。
- **AuthSession：** 服务端保存的 Mock 登录会话，绑定员工、角色、Workspace、过期和注销状态。
- **Principal：** 当前请求由 AuthSession 派生的可信身份；业务接口不从请求体接受身份。
- **Workspace：** 单次登录下的私有草稿、对话和当前游标空间。
- **Access Case：** 正式提交后的共享业务事实，可被有资源关系的不同 Session 重读。
- **ConversationCursor：** 服务端保存的待补字段、草稿 revision、当前身份和最近追问合同。
- **Decision Packet：** 每个 Case 唯一的决策材料快照。

## 4. 全局不变量

1. 身份和角色只来自有效 AuthSession；
2. 未提交草稿与对话不跨 Workspace，共享 Case 不依赖原 Workspace 才能读取；
3. 所有资源查询按 requester/已参与审批人/permissions_admin 关系授权，不能只按 Workspace 放行；
4. 模型只能生成建议，不能改变身份、目录编码、审批路线、业务状态或外部副作用；
5. GET 请求不调用模型、不审批、不执行 IAM；
6. 越权、错状态和输入注入在数据库写入或模型/IAM 调用之前失败；
7. 一个 Case 最多一个 Decision Packet、一个审批流和一个 Grant；
8. v1.1 已验证的政策 RAG、审批顺序和幂等开通能力不得退化。

## 5. FR-01 ConversationCursor 与上下文意图

### 5.1 Cursor 最小合同

服务端为当前 Workspace 保存：

```text
workspace_id
actor_id
auth_session_id?
draft_revision
expected_field
last_question_kind
issued_at
consumed_at?
```

T18 在 v1.1 基线上使用 `workspace_id + actor_id` 作用域，`auth_session_id` 暂为空；T19 完成登录后，该字段变为必填，换 Session 必须使旧 Cursor 失效。

`expected_field` 只允许：

- `entitlement_id`
- `duration_days`
- `justification`
- `confirmation`
- `none`

### 5.2 revision 与 Cursor 状态转换

- `draft_revision` 从 0 开始；任何被接受的申请字段新增或修改都通过 CAS 原子写入并递增 revision；
- Cursor 绑定创建时的 draft revision，只有对应追问已经持久化为成功终态后才激活；`turn.interrupted`、`error.recoverable` 或用户未看到的追问不得留下活动 Cursor；
- 合法纯数字期限先完成目录上限校验，再以 CAS 同时更新草稿、递增 revision 并消费当前 Cursor；若仍缺字段，只在下一条追问持久化完成后为新 revision 建立下一 Cursor；
- 超过目录上限、0、负数、小数或超长整数不改变草稿/revision，当前期限 Cursor 继续有效；
- 同一个 `111` 重复发送时，第一次成功消费后旧 Cursor 已失效，第二次不得再次按相同期限消费；
- `issued_at` 只用于审计，本版本没有时间 TTL，不使用“时间过期”语义。

Cursor 在以下任一条件下清除或失效：

- AuthSession、身份或 Workspace 改变；
- 草稿 revision 不匹配；
- 当前问题已经消费；
- 用户明确切换到帮助、政策、权限查询或申请状态；
- 草稿已提交、重置或被新申请替换。

### 5.3 路由优先级

1. 安全探测独立标记，不改变可信身份；
2. 明确的帮助、政策、可申请权限、已有权限和申请状态优先于 Cursor；
3. 对纯数字输入，有效 Cursor 按当前待补字段执行确定性续答；
4. 明确申请内容进入申请收集；
5. 纯数字且没有有效 Cursor 时返回 `intent=unknown/business_status=needs_clarification`；其他非数字输入保持 v1.1 已验证路由，本版本不做通用 unknown 重构。

### 5.4 `111` 的二元行为

| 上下文 | intent / business_status | 结果与副作用 |
|---|---|---|
| 有效 Cursor 正在等待 `duration_days` | `request_access / collecting` 或 `awaiting_confirmation` | 按 111 天解析并执行目录上限校验；合法才原子更新草稿，模型调用数不变 |
| 当前权限上限小于 111 天 | `request_access / collecting` | 返回明确上限并继续等待期限；不保存 111、不递增 revision |
| 有效 Cursor 正在等待 `entitlement_id` | `request_access / collecting` | 返回权限字段提示，不修改草稿 |
| 有效 Cursor 正在等待 `justification` | `request_access / collecting` | 返回理由字段提示，不修改草稿 |
| 有效 Cursor 正在等待 `confirmation` | `request_access / awaiting_confirmation` | 提示明确回复“确认提交”或修改；不确认、不提交 |
| 无/失效 Cursor 或身份变化 | `unknown / needs_clarification` | 询问数字含义；不改草稿、revision、确认、配额、模型、工具或业务事实 |
| 明确帮助 | `help / answered` | 返回帮助并清除当前 Cursor，不消费模型额度 |

### 5.5 数字规则

- 仅 `^[1-9][0-9]{0,3}$` 可作为期限候选；
- `0`、负数、小数、超长整数返回字段级错误；
- 期限必须再次通过当前 entitlement 的 `max_duration_days` 校验；
- 定义规范化 Outcome：intent、business_status、draft_revision、draft、assistant_message/error_code；SSE 从持久化事件重建 Outcome 后必须与 JSON 入口一致；
- 纯数字期限处理不调用 DeepSeek，不消耗模型额度。
- “零副作用”表示草稿、revision、确认、模型配额、模型调用、工具调用和业务事实不变；允许追加脱敏的用户/助手/审计事件，目录上限校验属于允许的确定性只读校验。

### 5.6 明确不做

- 不把任意数字都解释为期限；
- 不做代词绑定、多人对话主体推理或完整历史摘要；
- 不把未接入生产链的 LangGraph 教学模块冒充主流程能力。

## 6. FR-02 Mock 登录、AuthSession 与 Principal

### 6.1 账号

P0 只允许以下虚构账号：

| account_id | 角色 |
|---|---|
| `EMP-001` | requester |
| `EMP-002` | manager |
| `EMP-003` | data_owner |
| `EMP-004` | permissions_admin |

登录只接受 `account_id` 并由服务端 allowlist 选择四个虚构账号之一，不设置密码、注册、重置或账号管理。登录页可以展示账号用途，但必须标记为作品集 Mock 数据；该入口只证明 Session 与 ACL，不证明访问者真实身份。

### 6.2 Session

- 登录成功原子创建私有 Workspace 与 AuthSession，生成不可预测 token，数据库只保存 token hash；
- Cookie 为 HttpOnly、SameSite，Secure 由运行档案控制；
- Session 固定绑定 employee、Workspace、过期和注销时间；
- 登录只校验配置中的精确 Web Origin；其余写接口同时校验 Origin 和 CSRF；
- CSRF 明文通过独立可读 Cookie 或等价响应合同在刷新后恢复，数据库只保存其 hash，轮换规则固定并有测试；
- 登出、过期、注销或篡改 Cookie 后，Session 立即失效；
- 四个浏览器上下文可以同时拥有互不覆盖的 Session。
- 所有业务路由只允许 `accesspilot_session → AuthSession → Principal` 进入；旧 Workspace Cookie 单独携带必须 401，匿名 Workspace 创建/ensure 不能形成业务身份；
- 若为兼容保留 Workspace Cookie，它只能是与 `AuthSession.workspace_id` 强绑定的非权威 locator；
- 登出、过期和注销只吊销 AuthSession，不删除 Workspace；v1.2 P0 禁止 Workspace GC/删除，避免现有 CASCADE 破坏 Case。

### 6.3 身份输入边界

- 除登录请求唯一允许的 `account_id` 外，body、query、form 和保留自定义 Header 中的 employee、role、organization、workspace、session 字段一律在副作用前稳定 422 拒绝；
- 角色从 EmployeeRecord 派生，登录请求不能自由声明；聊天中声称“我是 EMP-x”也不能改变 Principal；
- v1.1 的 `POST /api/demo/session` 在 v1.2 产品档案中必须 404；
- 产品页面不得出现直接切换 actor 的 DemoConsole；
- 测试 helper 不进入公开路由或生产构建。

## 7. FR-03 共享 Case、私有 Workspace 与资源 ACL

### 7.1 单组织共享语义

- v1.2 固定四个账号处于同一个 Demo 组织，不新增 organization/tenant 表或跨组织验收；
- 正式 AccessRequest 继续作为 Case 根，既有 `workspace_id` 只表示来源/兼容关系；
- 本版本不删除、不重命名 Workspace 外键，也不保证删除来源 Workspace 后 Case 存活；
- 业务查询不得再把“相同 Workspace”当作共享 Case 的授权条件，而是直接编码 requester、已参与审批人和 permissions_admin 资源关系；
- 由于现有外键仍可能 CASCADE，P0 禁止 Workspace 清理；Session 吊销不能删除 Workspace。

### 7.2 ACL 矩阵

| 资源/动作 | 申请人 | 经理 | 数据负责人 | 权限管理员 |
|---|---|---|---|---|
| 私有草稿/聊天 | 仅自己的 Workspace | 仅自己的 Workspace | 仅自己的 Workspace | 仅自己的 Workspace |
| 查看正式 Case | 自己提交的 | 已轮到或已决定的本人步骤 Case | 已轮到或已决定的本人步骤 Case | 全部已批准的 Case，含 failed/unknown/succeeded/Grant |
| 经理审批 | 禁止 | 仅当前指定步骤 | 禁止 | 禁止 |
| 数据负责人审批 | 禁止 | 禁止 | 仅经理通过后的指定步骤 | 禁止 |
| 执行开通 | 禁止 | 禁止 | 禁止 | 仅全部批准的 Case |

### 7.3 拒绝合同

- 未登录返回 401；
- 已登录但动作职责不符返回稳定 403；
- 猜测 ID 或无任何资源关系时返回 404，避免泄露对象是否存在；
- 所有拒绝路径断言模型调用数、IAM 调用数和业务写入数为零。

## 8. FR-04 简化 Decision Packet 与风险建议

### 8.1 Packet 内容

每个 Case 最多一个 Packet，至少包含：

- 冻结的申请人、权限、期限和理由；
- 目录风险等级、期限上限和确定性审批路线；
- 本轮政策证据与引用；
- 申请人说明；
- 风险摘要、未知项和建议；
- 目录/政策/Packet 版本与生成方式。

每个展示条目标记：

- `verified_fact`
- `policy_evidence`
- `user_claim`
- `advisory`

`advisory` 同时记录 `generation_mode=provider|deterministic|unavailable`。只有经过验证的真实 DeepSeek 调用才显示“AI 建议”；fake/offline 显示“规则建议”或“测试适配器”。

### 8.2 生成边界

1. 只有申请人本人可为自己的 submitted Case 触发；提交页面自动调用一次，失败时允许申请人重试同一幂等动作，审批人打开页面和 GET 永不触发；
2. 读取冻结 Case、目录和本轮政策证据；
3. 可选调用一次受限 DeepSeek，使用严格 Schema、脱敏输入和配置化超时；
4. 无 Key、超时或非法输出时生成 `generation_mode=unavailable` 的诚实 Packet，不编造模型文本；
5. 以唯一约束保证并发重试最多持久化一个 Packet；
6. Packet 建立后只读，业务字段变化必须创建新 Case；
7. Provider 返回的 clear/blocked/risk 文本始终只是 advisory，既不能跳过也不能阻断固定的经理→数据负责人路线。

模型不可输出或选择：

- Principal、organization、entitlement code；
- 审批人或审批路线；
- approve/reject；
- idempotency key；
- 开通工具调用。

P0 不建设风险 Operation、预算预留、Provider 多 Attempt 或崩溃后自动恢复。若进程在 Packet 持久化前中断，申请人可重试同一只读生成动作；无 Packet 时不得审批。

## 9. FR-05 跨账号串行审批

- Packet 已存在后才创建审批流；现有 approval-case 入口只允许申请人本人调用，只从 Packet 与目录构建路线，不得再次调用模型；
- 路线由目录确定：EMP-002 经理→EMP-003 数据负责人；
- 前序未通过时，后序收件箱无可决策项；
- 每次决定同时校验 Case 资源关系、当前 assignee、顺序、非自审批、非终态和行锁；
- 驳回必须有非空原因；
- 重复、并发、乱序、错角色和自审批均零副作用；
- 新 Session 或刷新后从数据库重读状态，不依赖原对话窗口。

## 10. FR-06 权限管理员开通

- 只有 EMP-004 的 permissions_admin Principal 可触发；
- Case 必须已经完成全部审批；
- 服务端生成并保存稳定 idempotency key，客户端不能覆盖；
- 沿用 v1.1 的 succeeded/failed/unknown 和“unknown 查询原操作”能力；
- 重复点击、并发、超时和恢复最多创建一个 Grant；
- 只有模拟 IAM 明确成功时创建 Grant；
- EMP-001 重新登录后可读取自己的 Grant；
- v1.2 不实现到期回收。
- 复用并扩展现有 `ProvisioningAttemptRecord`、幂等和 unknown-recover 链路，不新增通用 Operation 平台；provision 与 recover 使用相同 permissions_admin ACL。

## 11. FR-07 页面与状态

### 11.1 登录页

- 未登录访问业务页面时进入登录页；
- 包含四个虚构账号卡、登录按钮和角色说明，不包含密码或账号管理；
- 显示错误、加载、过期和退出状态；
- 登录后顶部显示当前账号与角色，但不提供直接角色切换；
- 切换角色必须退出登录或使用另一浏览器上下文。

### 11.2 角色工作台

- 申请人：对话、草稿、我的申请；
- 经理/数据负责人：当前审批待办；
- 权限管理员：已批准的开通任务；
- Case 详情：Packet、审批、开通和审计时间线；
- 不可操作状态不能只靠隐藏按钮，服务端仍必须拒绝。

沿用现有设计语言，检查 1440×900、1024×768、390×844 和键盘路径。

## 12. 数据模型

### 12.1 新增

- `auth_sessions`：token_hash、employee_id、workspace_id、csrf_hash、expires_at、revoked_at；
- `decision_packets`：request_id 唯一、版本、生成方式、冻结内容、创建时间；
- 可选 `decision_packet_items`：source_kind、source_ref、展示内容和顺序。

### 12.2 扩展

- Workspace：draft_revision 与 ConversationCursor 字段；
- AccessRequest：保留 workspace_id 作为来源，正式授权语义改为资源关系；
- `ProvisioningAttemptRecord`：服务端幂等键归属与 permissions_admin Principal；
- AuditEvent：记录 Principal、Case、动作前后状态，禁止保存 Cookie、CSRF、原始 prompt 或隐藏推理。

迁移采用 expand/backfill，不删除 v1.1 业务事实；本版本不做破坏性 cutover。

## 13. 公开接口语义

最终路径可在 Ticket 内按现有路由习惯调整，但语义不得弱化：

- `POST /api/auth/login`
- `GET /api/auth/session`
- `POST /api/auth/logout`
- `POST /api/chat/stream`
- `GET /api/requests/mine`
- `GET /api/requests/{case_id}`
- `POST /api/requests/{case_id}/decision-packet`（仅 Case requester；提交页自动调用并可安全重试）
- `POST /api/requests/{case_id}/approval-case`（仅 Case requester；要求 Packet 已存在且不得再次调用模型）
- `GET /api/approval-inbox`
- `POST /api/approval-cases/{id}/decisions`
- `GET /api/provisioning-tasks`
- `POST /api/requests/{case_id}/provision`
- `POST /api/requests/{case_id}/provision/recover`

所有外部请求模型使用 `extra=forbid`。响应不暴露 Session token/hash、CSRF hash、Provider Key、原始 prompt、内部错误或隐藏推理。

## 14. 安全与攻击矩阵

固定测试至少覆盖：

1. 篡改/过期/注销 Cookie；
2. 缺失或错误 CSRF/Origin；
3. body/query/form/header 注入 employee、role、organization、workspace 或 session 字段，以及聊天声称“我是 EMP-x”；
4. EMP-001 猜测他人 Case；
5. 只携带旧 Workspace Cookie或调用匿名 Workspace ensure 访问业务接口；
6. EMP-003 越过经理提前审批；
7. EMP-001 自审批；
8. EMP-002 或 EMP-003 尝试开通；
9. Packet 缺失时审批；
10. 未全部批准时开通；
11. 重复/并发审批与开通；
12. DeepSeek 超时、无 Key、非法输出或返回 blocked/clear 越权改变固定路线；
13. `111` 在错误/失效上下文中修改草稿或确认；
14. v1.1 Demo actor 切换端点在产品档案中仍可访问；
15. 登出、过期或注销错误删除 Workspace/Case；
16. 审批人打开 Case/GET 意外触发 Packet 或模型调用。

每个失败场景必须验证 HTTP/业务状态、数据库变化、模型调用数、IAM 调用数和可见错误文案。

## 15. 质量与证据

- 后端：路由、Cursor、Session、ACL、Packet、审批、开通、迁移和攻击测试；
- 前端：登录、过期、角色工作台、上下文回复、空/错/加载状态和可访问性；
- 固定评测新增上下文数字、登录隔离、越权和四角色全流程类别；
- fake provider 用于确定性 CI；`no_key_verification` 启动档案或测试夹具验证 unavailable，不增加产品 UI/API 故障控制面；真实 DeepSeek smoke 仅显式启用并单列；
- 未运行真实 smoke 时标为 `N/A/unverified`，不得形成真实模型质量 Claim；
- 四个 clean browser context 共同处理同一个 Case；
- 所有证据记录实际 Git revision、配置档案和限制。

## 16. P0 二元验收矩阵

| AC | 通过条件 |
|---|---|
| AC-01 | 有效期限 Cursor 下，`111` 确定性解析并执行目录上限校验，模型调用数不变 |
| AC-02 | 无效/其他上下文中的 `111` 返回差异化澄清，草稿、确认、配额和工具均不变 |
| AC-03 | Cursor 失效规则、显式帮助优先级及 JSON/SSE 一致性通过 |
| AC-04 | 四账号通过独立登录获得互不覆盖的 AuthSession |
| AC-05 | 身份字段注入、Cookie/CSRF/Origin 攻击失败，Demo actor 切换在产品档案 404 |
| AC-06 | 正式 Case 跨 Session 可重读，未提交草稿与聊天保持私有 |
| AC-07 | ACL 矩阵、IDOR、自审批、乱序、错角色和旧 Workspace Cookie 攻击失败闭合 |
| AC-08 | 每个 Case 唯一 Packet，四类来源与生成方式可追溯 |
| AC-09 | DeepSeek 失败时明示 unavailable，不伪造 AI 建议且不改变确定性路线 |
| AC-10 | EMP-002→EMP-003 在独立 Session 中按顺序完成审批 |
| AC-11 | 只有 EMP-004 可在全部批准后开通，重复/unknown 恢复最多一个 Grant |
| AC-12 | EMP-001 重新登录后读取同一 Case、时间线和 Grant |
| AC-13 | 四浏览器在桌面视口完成主链，另外两档响应式/键盘烟测、迁移、构建、固定评测和攻击矩阵通过 |
| AC-14 | 文档、ADR、Demo 话术和 Claim 明确 Mock/真实、已实现/延期边界 |

## 17. Interview Evidence Pack（精简）

不建立正式录屏哈希和 168 小时协议。T25 只要求：

- 不超过 90 秒的项目介绍；
- 8–10 分钟四角色主线；
- `111` 上下文、越权拒绝、模型不可用三个追问点；
- 三张决策卡：上下文游标、Session/ACL、模型/确定性边界；
- Claim Ledger：每条表述链接代码、测试、ADR、运行证据与限制；
- 用户本人能不看逐字稿完成一次讲解，并回答两道未写进 Demo 的迁移问题。

该证据包不进入企业产品 UI/API，不阻塞 `product_verified`。

## 18. Ticket 关口

本 Spec 拆为 T18–T25：

- T18：ConversationCursor 与 `111`；
- T19：登录与 AuthSession；
- T20：共享 Case 与 ACL；
- T21：Decision Packet 与风险建议；
- T22：跨账号审批；
- T23：管理员开通；
- T24：集成验收；
- T25：ADR 与面试证据。

Ticket 列表已由用户于 2026-08-11 确认，按 T18 → T25 串行实施。当前停止点是 T18 已完成并独立验收，T19–T25 尚未开始；后续实现仍须逐 Ticket 测试先行、独立验收，并保持不推送、不合并、不部署。
