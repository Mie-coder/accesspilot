# AccessPilot v1.2 Interview Evidence Pack

**日期：** 2026-08-12  
**代码证据 revision：** `eec27d7`（T24 产品验收）  
**产品状态：** `product_verified=true`  
**面试状态：** `interview_ready=false`；本文档已备齐，但仍需候选人本人不看逐字稿完成一次讲解并回答两道迁移题。

这是一份“可核验的作品集原型”讲解材料，不是产品发布说明。所有账号、权限、政策、审批和 IAM 均为虚构数据。AI 可以协助实现和整理证据，但不能替代候选人的理解、复述和现场判断。

## 1. 90 秒项目介绍（讲解骨架）

> AccessPilot 是一个企业访问申请 Agent 原型。我解决的不是“让模型替用户点按钮”，而是把一次权限申请做成可以核验的生命周期：申请人提出需求，系统用当前上下文收集期限和理由，经理、数据负责人、权限管理员在各自登录 Session 中依次处理同一个 Case，申请人重新登录后还能读到结果。这个版本有三个关键设计。第一，服务端保存 ConversationCursor，所以在刚问期限时输入 `111` 会被确定性解释为 111 天；换了上下文就不会乱猜。第二，身份来自服务端 AuthSession 派生的 Principal，Case ACL 在服务端过滤，前端隐藏按钮不构成授权。第三，Decision Packet 把冻结事实、政策证据、申请人说明和 AI/规则建议分开，模型只提供提醒，不能批准、开通或改身份。我们用 101/101 固定评测、四角色浏览器主链、攻击矩阵和前后端质量门禁验证了这些边界。它仍是 Mock Login 和模拟 IAM：不宣称真实 SSO、生产多租户、回收、SLA 或真实 DeepSeek 质量。

讲解时不要逐字朗读；用自己的话保持“问题 → 决策 → 证据 → 边界”的顺序即可。

## 2. 8–10 分钟四角色 Demo 检查表

### 2.1 开始前（约 30 秒）

- [ ] 打开本地 Web：`http://127.0.0.1:5173/`；确认页面显示“作品集 Mock 登录”。
- [ ] 准备四个独立浏览器窗口/上下文：`EMP-001` 申请人、`EMP-002` 直属经理、`EMP-003` 数据负责人、`EMP-004` 权限管理员。
- [ ] 只展示虚构数据；不展示 `.env`、API key、数据库凭据、私有地址或原始异常栈。
- [ ] 说明：切换角色必须退出并重新登录，不能用 actor 下拉框改身份。

### 2.2 主线（约 7–8 分钟）

| 时间 | 角色与动作 | 必须指出的可观察证据 |
|---|---|---|
| 0:30–1:30 | EMP-001 登录；在申请工作台选择目录权限。系统追问期限时输入 `111`。 | 当前字段是期限时，`111` 被解释为天数；可见期限校验/继续追问。说明模型调用数不因该解析增加。 |
| 1:30–2:30 | EMP-001 补理由，确认提交，生成 Decision Packet，再点击“启动审批”。 | Case ID、Packet 来源标签和“审批已启动”；Packet 是冻结证据，不是审批结果。 |
| 2:30–3:30 | EMP-002 登录审批待办，批准当前步骤。 | 只能看到自己的 pending 步骤；批准后状态变为等待数据负责人。 |
| 3:30–4:30 | EMP-003 登录并批准。可先尝试在经理未通过时操作以展示顺序守卫。 | 未轮到时拒绝/不可操作；经理通过后才出现 pending；通过后 Case 为已批准。 |
| 4:30–5:30 | EMP-004 登录开通任务并执行一次开通。 | 只有管理员可开通；页面显示一次稳定 Attempt/Grant。重复点击不会创建第二个 Grant。 |
| 5:30–6:30 | EMP-001 退出并重新登录，打开“我的申请”。 | 同一 Case、审批时间线和同一 Grant 可重读；登出没有删除正式业务事实。 |
| 6:30–7:30 | 选择一个越权动作（例如 EMP-001 自审、EMP-002 开通或 EMP-003 越过经理）。 | 服务端返回拒绝且业务/模型/IAM 写入为零；强调不是只隐藏按钮。 |
| 7:30–8:30 | 回到对话：没有期限 Cursor 时再输入 `111`；然后展示模型不可用/降级 Packet（可用固定测试档案）。 | 无上下文的 `111` 返回 `unknown/needs_clarification`；模型不可用明确标记 unavailable，确定性审批路线不变。 |
| 8:30–9:00 | 总结三张决策卡和 Mock/真实边界。 | 让面试官看到“决策可解释、权限可拒绝、模型可降级”。 |

**快捷证据：** 本地一次浏览器演示记录的 Case 为 `d5ecb751-215d-4c4b-91ba-c2f62d5da466`、Grant 为 `f94e27d7-4177-4951-8e03-66b48fc9f6ef`；两者只是虚构本地对象，不能当作生产 ID。主线和限制的完整映射见 [Product Manifest](accesspilot-v1.2-product-manifest.md)。

### 2.3 讲解通过标准

- [ ] 不看本文逐字稿，能用 8–10 分钟走完四角色主线。
- [ ] 每一步能指出服务端状态、当前角色和“谁不能做什么”。
- [ ] 能说明一次拒绝为何发生，以及为什么拒绝路径不应触发模型/IAM/业务写入。
- [ ] 能主动说出 Mock Login、模拟 IAM、固定单 Demo 组织和未测量项。

## 3. 三张决策卡

### 决策卡 A：上下文优先于数字猜测

- **问题：** 用户只输入 `111` 时，系统是否把它当成帮助、权限编号或期限？
- **决定：** 先读取服务端 ConversationCursor；只有唯一活动 Cursor 且 `expected_field=duration_days` 时按天数解析，再走目录上限校验。其他上下文返回差异化澄清。
- **拒绝的替代：** 全局正则把所有纯数字当期限；或把所有短输入交给模型猜测。
- **代价与限制：** 需要持久化 Cursor、revision/CAS 和失效规则；只覆盖本版本定义的字段，不是通用代词消解。
- **代码/测试：** [`workspaces.py`](../../apps/api/src/accesspilot/workspaces.py)、[`test_t18_cursor.py`](../../apps/api/tests/conversation/test_t18_cursor.py)。
- **ADR：** `docs/adr/0004-contextual-numeric-cursor.md`（由 T25 维护）。

### 决策卡 B：Principal 与资源 ACL 由服务端决定

- **问题：** 登录后如何避免“改 employee_id/role 就能冒充别人”？
- **决定：** 登录 allowlist 创建 HttpOnly AuthSession；所有业务请求从 Session 派生 Principal，正式 Case 的 requester/approver/admin ACL 在 SQL 查询和写入前执行。
- **拒绝的替代：** 前端角色下拉框、请求体中的 `employee_id`/`role`、先查对象再在前端过滤。
- **代价与限制：** 需要 Session/CSRF/Origin、资源关系查询和跨 Session 测试；它是 Mock 登录，不等于 OIDC/SAML 或密码认证。
- **代码/测试：** [`auth.py`](../../apps/api/src/accesspilot/auth.py)、[`test_t19_auth.py`](../../apps/api/tests/api/test_t19_auth.py)、[`test_t20_case_acl.py`](../../apps/api/tests/api/test_t20_case_acl.py)。
- **ADR：** `docs/adr/0005-session-principal-resource-acl.md`（由 T25 维护）。

### 决策卡 C：模型是建议，不是授权源

- **问题：** Decision Packet 中的 AI/规则建议能否直接批准或开通？
- **决定：** Packet 冻结事实、政策依据、申请人说明和建议来源；模型只生成受约束提醒。审批步骤、角色守卫、管理员开通和幂等 Grant 均由确定性服务完成。
- **拒绝的替代：** 让模型输出 `approved=true`、让模型选择 approver/IAM 参数、模型失败时伪造“低风险”。
- **代价与限制：** 需要结构化校验、来源标签和 unavailable 分支；当前没有真实 DeepSeek 质量基准或生产 SLA。
- **代码/测试：** [`decision_packets.py`](../../apps/api/src/accesspilot/decision_packets.py)、[`risk/decision_packet.py`](../../apps/api/src/accesspilot/risk/decision_packet.py)、[`test_t21_decision_packet.py`](../../apps/api/tests/api/test_t21_decision_packet.py)、[`test_t23_admin_provisioning.py`](../../apps/api/tests/api/test_t23_admin_provisioning.py)。
- **ADR：** `docs/adr/0006-advisory-cannot-authorize.md`（由 T25 维护）。

## 4. 追问卡（不改变主线）

### 追问 1：为什么输入 `111` 不总是期限？

回答应包含：自然语言短输入有上下文歧义；Cursor 绑定 workspace/Session、actor、draft revision 和 expected field；有效期限路径做目录上限校验，失效/其他字段路径不改草稿、不确认、不调模型，返回 `unknown/needs_clarification`。

### 追问 2：越权为什么不是“按钮隐藏”就算完成？

回答应包含：客户端可被改写；服务端从 AuthSession 派生 Principal，Case ACL 在 SQL/写入前过滤；自审、越序、错角色、猜 ID 和未批准开通都应在副作用前拒绝，并验证模型/IAM/业务写入计数不变。

### 追问 3：模型不可用时发生什么？

回答应包含：超时、无 Key、schema/引用非法均显式 `unavailable`；Packet 保留事实和政策证据，审批/开通路线不改，不伪造 AI 建议。可用无 Key 测试档案或固定夹具复现，不把本地 provider 观察当作 DeepSeek 质量结果。

## 5. 两道未写入 Demo 的迁移题与评分表

用户本人需在没有本文逐字稿的情况下回答。每题 4 项全部说清才算通过；文档先记录“题目和验收口径”，不把未发生的回答写成已通过。

### 迁移题 A：Mock Login → OIDC，如何不破坏 Case ACL？

**题目：** 如果把四账号选择式 Mock Login 换成 OIDC Authorization Code + PKCE，你会保留哪些服务端不变量？哪些组件需要改？你会防什么失败？怎么验证？

**通过口径（四点）：**

1. **不变量：** 浏览器不提交 employee/role/workspace 作为事实；OIDC `sub` 经服务端 allowlist/目录映射后生成同一类 Principal；AuthSession/CSRF/注销仍是业务入口；Case requester/approver/admin ACL 语义不变。
2. **受影响组件：** 登录回调与 token 验证、AuthSession/Principal 映射、前端登录页和配置；Case/审批/Packet/开通服务不应改成信任客户端 claims。
3. **失败模式：** 直接信任 `role` claim、未校验 issuer/audience/nonce/PKCE、旧 Mock Session 未吊销、OIDC 用户映射错位导致越权或 Case 丢失。
4. **可执行验证：** 先跑现有身份/ACL 回归：
   ```bash
   cd accesspilot
   pytest apps/api/tests/api/test_t19_auth.py apps/api/tests/api/test_t20_case_acl.py -q
   ```
   再用 OIDC 测试 IdP fixture 覆盖错误 issuer/audience、重放 code、注销后旧 Cookie、恶意 role/employee 字段，并复用同一组 Case IDOR/自审/错角色断言；迁移专用 contract test 必须在合入前红绿一次。

### 迁移题 B：模拟 IAM → 异步真实 IAM，如何保持唯一 Grant？

**题目：** 如果把当前同步确定性 IAM 替换成队列 + 真实 IAM API，你如何保留服务端幂等键、unknown 恢复和“最多一个 Grant”？哪些故障最危险？怎么验证？

**通过口径（四点）：**

1. **不变量：** 只有全部审批通过且 EMP-004 触发才可开通；operation key 由服务端按 Case/attempt 生成；provider 超时先落 `unknown`，恢复查询同一 key；数据库唯一约束确保一个最终 Grant。
2. **受影响组件：** provisioning service、Attempt/Grant 表和唯一索引、事务 outbox/worker、IAM adapter、状态查询与管理员 UI；审批和资源 ACL 不迁移到客户端或模型。
3. **失败模式：** worker 重试创建第二个账号、消息提交成功但响应丢失、客户端自带 operation key、回调伪造成功、未知状态被误标失败后重复开通。
4. **可执行验证：** 先跑现有服务契约：
   ```bash
   cd accesspilot
   pytest apps/api/tests/api/test_t23_admin_provisioning.py apps/api/tests/provisioning/test_service.py -q
   ```
   再给 IAM adapter 注入 timeout/丢响应/重复投递/乱序回调 fixture，断言同一 server key 只产生一个 Grant、恢复读取原 Attempt、非管理员和未批准 Case 的 IAM 调用数为零；异步迁移专用 contract test 通过后才能替换 adapter。

## 6. Ownership、AI 协助与边界清单

- [ ] 候选人能说明自己做的业务选择：三个 ADR 的取舍、服务端授权边界、测试先行和证据整理。
- [ ] 候选人能指出 AI 协助范围：实现草稿、测试脚手架、文档初稿；不能把 AI 生成文本当作本人掌握度或外部模型质量。
- [ ] 候选人能从代码、测试、ADR、Manifest 和浏览器记录逐条回溯 Claim。
- [ ] 候选人能主动说出未做项：真实 SSO/OIDC、真实 IAM、生产多租户、到期回收/撤销、生产 SLA、真实 DeepSeek 质量基准。
- [ ] 候选人完成一次无稿讲解并答对两道迁移题后，才可将状态改为 `interview_ready=true`；在此之前保持 `false/pending_user_verification`。

## 7. 证据索引与安全

- 产品事实与 AC-01–AC-13：[`accesspilot-v1.2-product-manifest.md`](accesspilot-v1.2-product-manifest.md)。
- 产品/范围：[`accesspilot-product-function-book-v1.2.md`](../product/accesspilot-product-function-book-v1.2.md)、[`accesspilot-productized-agent-v1.2.md`](../specs/accesspilot-productized-agent-v1.2.md)。
- T24 集成测试：[`test_t24_product_verification.py`](../../apps/api/tests/api/test_t24_product_verification.py)。
- 质量与历史边界：[v1.1 作品集证据](accesspilot-v1.1-portfolio-evidence.md)、[学习记录 0001](../learning/learning-records/0001-keep-model-keys-on-server.md)、[学习记录 0002](../learning/learning-records/0002-model-output-needs-domain-validation.md)。

证据包不包含源码、`.env`、API key、数据库凭据、私有地址、系统提示词、隐藏推理、原始异常栈或内部预算字段。浏览器只展示虚构账号和本地对象。

## 8. 禁止外推的 Claim

以下说法即使产品验收通过也不得写进简历或面试回答：

- “已接入真实 SSO/OIDC/SAML”“已完成企业账号认证/密码安全”；
- “已接入真实 IAM/IGA”“支持生产开通、回收、撤销或多租户”；
- “具备生产级高可用、合规审计、SLA、容量或成本指标”；
- “DeepSeek 质量已验证/延迟达到某个 p50/p95”；
- “`product_verified=true` 自动证明候选人 `interview_ready=true`”。

