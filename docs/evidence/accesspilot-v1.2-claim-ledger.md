# AccessPilot v1.2 Claim Ledger

**代码证据 revision：** `eec27d7`  
**产品结论：** `product_verified=true`  
**面试结论：** `interview_ready=false/pending_user_verification`  
**用途：** 面试或简历只引用“有限、可回溯”的 Claim；每条 Claim 都必须能从代码、测试、ADR 和实际运行记录回到同一个边界。

> Ownership 规则：AI 协助了实现草稿、测试脚手架和文档整理；候选人只有在能解释设计、指出限制、复现实验并完成无稿演练后，才把对应 Claim 作为自己的面试表述。本文不预填“本人已通过”。

| ID | 可说的有限 Claim | 个人 ownership / AI 协助 | 代码与测试 | ADR / 运行证据 | Mock / 真实边界与限制 | Resume / interview 状态 |
|---|---|---|---|---|---|---|
| CL-01 | 在明确的期限上下文中，纯 `111` 由服务端 Cursor 解释为 111 天；换上下文会澄清而不是全局猜测。 | 能解释 Cursor、revision/CAS、失效规则；AI 协助测试与实现草稿。 | [`workspaces.py`](../../apps/api/src/accesspilot/workspaces.py)；[`test_t18_cursor.py`](../../apps/api/tests/conversation/test_t18_cursor.py)。 | [ADR-0004](../adr/0004-contextual-numeric-cursor.md)；[Manifest AC-01–03](accesspilot-v1.2-product-manifest.md#AC-01AC-13)。 | 只覆盖本版本字段和纯数字规则；不是通用 NLU 或模型质量 Claim。 | 产品事实可讲；无稿复述仍待本人确认。 |
| CL-02 | 登录后业务身份由服务端 AuthSession 派生 Principal，Case ACL 在服务端阻断自审、越序、错角色和 IDOR。 | 能解释为何不信任 body/query/前端角色；AI 协助攻击测试矩阵。 | [`auth.py`](../../apps/api/src/accesspilot/auth.py)、[`operations.py`](../../apps/api/src/accesspilot/operations.py)；[`test_t19_auth.py`](../../apps/api/tests/api/test_t19_auth.py)、[`test_t20_case_acl.py`](../../apps/api/tests/api/test_t20_case_acl.py)、[`test_t22_approval_lifecycle.py`](../../apps/api/tests/api/test_t22_approval_lifecycle.py)。 | [ADR-0005](../adr/0005-session-principal-resource-acl.md)；[Manifest AC-04–07](accesspilot-v1.2-product-manifest.md#AC-01AC-13)。 | 四个账号是 allowlist Mock Login；不是真实 SSO、OIDC、密码认证或生产多租户。 | 只能以“服务端身份与资源 ACL 原型”表述；个人讲解待确认。 |
| CL-03 | Decision Packet 将冻结事实、政策证据、申请人说明和 AI/规则建议分源；模型失败时明确 unavailable，不能审批或开通。 | 能解释结构化输出校验、建议/授权分离；AI 协助适配器与文档。 | [`decision_packets.py`](../../apps/api/src/accesspilot/decision_packets.py)、[`risk/decision_packet.py`](../../apps/api/src/accesspilot/risk/decision_packet.py)；[`test_t21_decision_packet.py`](../../apps/api/tests/api/test_t21_decision_packet.py)、[`test_t21_decision_advisory.py`](../../apps/api/tests/risk/test_t21_decision_advisory.py)。 | [ADR-0006](../adr/0006-advisory-cannot-authorize.md)；[Manifest AC-08–09](accesspilot-v1.2-product-manifest.md#AC-01AC-13)。 | 本地模拟/受限 provider；没有真实 DeepSeek 质量、token latency 或 SLA 结论。 | 可讲“受约束建议层”；禁止讲“模型自动审批/真实质量已验证”。 |
| CL-04 | 四个独立角色可围绕同一 Case 完成申请、两级审批和一次幂等模拟开通，申请人重登后能读到同一 Grant。 | 能说明状态机、expected step、server key、unknown recover 和唯一 Grant；AI 协助集成测试。 | [`approvals.py`](../../apps/api/src/accesspilot/approvals.py)、[`provisioning.py`](../../apps/api/src/accesspilot/provisioning.py)；[`test_t22_approval_lifecycle.py`](../../apps/api/tests/api/test_t22_approval_lifecycle.py)、[`test_t23_admin_provisioning.py`](../../apps/api/tests/api/test_t23_admin_provisioning.py)、[`test_t24_product_verification.py`](../../apps/api/tests/api/test_t24_product_verification.py)。 | [ADR-0002](../adr/0002-separate-approval-and-provisioning.md)、[ADR-0006](../adr/0006-advisory-cannot-authorize.md)；[Manifest AC-10–12](accesspilot-v1.2-product-manifest.md#AC-01AC-13)。 | IAM 是确定性模拟器；未实现真实异步 IAM、到期回收、撤销、生产运维。 | 可讲“可验证生命周期原型”；不得包装为企业 IAM/IGA。 |
| CL-05 | v1.2 在当前 revision 通过后端/前端质量门禁、101/101 固定评测、四角色主链、三视口和攻击矩阵。 | 能解释测试分母、历史 lineage、哪些结果只是本地观测；AI 协助整理报告。 | [`test_t24_product_verification.py`](../../apps/api/tests/api/test_t24_product_verification.py)、[`verify-local.sh`](../../scripts/verify-local.sh)、[`evaluation.py`](../../apps/api/src/accesspilot/evaluation.py)。 | [ADR-0004](../adr/0004-contextual-numeric-cursor.md)、[ADR-0005](../adr/0005-session-principal-resource-acl.md)、[ADR-0006](../adr/0006-advisory-cannot-authorize.md)；[Manifest 质量门禁](accesspilot-v1.2-product-manifest.md#质量门禁)。 | 本地单机、虚构数据、固定 fixture；不构成生产容量、安全率、成本、SLA 或用户成功率。 | 只能在“本地作品集原型已验证”口径使用；`interview_ready` 仍待本人演练。 |

## 明确禁止的外推

Claim Ledger 不授权以下表述：真实 SSO/OIDC/SAML 或密码认证；真实企业 IAM/IGA、生产开通/回收/撤销；完整生产多租户与合规审计；生产高可用、容量、成本或 SLA；真实 DeepSeek 质量、延迟或可靠性；用户本人已经完成未记录的面试演练。

## 使用前人工审计

- [ ] 每条 Claim 能给出当前 revision 的代码和测试路径。
- [ ] 每条 Claim 能说清一项代价/限制，而不是只报全绿数字。
- [ ] 若引用浏览器 Case/Grant，只使用虚构本地 ID，不泄露凭据。
- [ ] 面试前候选人独立运行主线、解释三张决策卡、回答两道迁移题；未完成前保持 `interview_ready=false`。
