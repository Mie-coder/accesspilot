# ADR-0006：模型建议只能是 advisory，不能授权或触发副作用

- 状态：已接受
- 日期：2026-08-12
- 范围：T21–T24；AC-08–AC-13、AC-14

## 背景与问题

模型输出可能超时、格式错误、引用不存在的政策，或用自然语言声称“可以放行”。如果模型直接选择审批人、批准 Case 或调用 IAM，提示词错误就会变成权限副作用，且无法回答“事实来自哪里”。因此模型只能补充可解释建议，授权事实必须由服务端固定状态机和服务端拥有的幂等键产生。

## 决策与不变量

1. 申请人触发一次只读 Decision Packet。Packet 冻结已验证申请事实、目录事实、政策证据、用户说明和代码计算出的 `fixed_route`；每个 Case 最多一个只读版本，GET、审批人打开页面不生成 Packet。
2. 可选 DeepSeek/确定性适配器只接收脱敏、严格 Schema 的上下文，并只能返回 `assessment/summary/unknowns/recommendations/citations`。Schema `extra=forbid`；输出不得包含 Principal、审批路线、approve/reject、幂等键或工具调用；引用必须属于本轮政策证据。
3. Provider 超时、无 Key、非法 Schema、未知引用或越权文本都落为 `generation_mode=unavailable`，保留事实/政策/固定路线，不伪造 AI 文本。`provider`/`deterministic` 只标示来源，不改变路线。
4. 审批路线由目录与服务端 Case 状态机执行；只有指定经理/数据负责人按顺序决定。只有 `permissions_admin` 在全部批准后能调用开通；服务端生成 `accesspilot:<request_id>`，IAM 明确成功后才创建一个 Grant。模型没有业务写工具。

## 被拒绝的替代方案

| 方案 | 拒绝理由 |
|---|---|
| 让模型直接输出 approve/reject、审批人或 IAM 参数 | 自然语言不是授权合同；会把不可验证的模型输出提升为高影响副作用，也破坏幂等和审计。 |
| 仅依赖 Prompt 说“不要越权” | Prompt 不是服务端边界；模型换版本、超时或返回额外字段时无法保证拒绝和零写入。 |
| 完全删除模型，只保留规则 | 最安全但失去来源分层和可选的风险解释；本版本选择“规则负责权威、模型只做 advisory”，在无 Key 时仍可诚实降级。 |

## 结果、代价与限制

- 结果：模型不可用不会阻断或放行审批；评审者可区分 verified fact、policy evidence、user claim 和 advisory，且重复生成/读取不增加调用或写入。
- 代价：需要维护 Packet 版本、严格 DTO、脱敏、引用校验和 unavailable UI；模型建议可能不完整或延迟，人工仍须复核事实。
- 限制：本地 IAM 是 `SimulatedIamProvisioner`，不是生产 IAM；provider Packet 的存在不等于 DeepSeek 质量、准确率或 SLA。生产接入仍需单独的 OIDC、IAM 合同、审计和回滚设计。

## 失败、恢复与回滚

- Advisory 失败：保存 `unavailable` Packet，固定路线继续可读；申请人可重试同一幂等生成动作，但已有 Packet 不被静默改写。
- Packet ACL/状态失败：先拒绝再调用模型，事务无 Packet/业务写入；政策快照或审计原子保存失败则回滚本次 Packet。
- 审批越序、自审批、错误角色：返回 404/409/403，Case 状态不变；模型建议不会被重新解释成决定。
- IAM failed/unknown：保留 `ProvisioningAttemptRecord`；unknown 只能按原服务端幂等键查询恢复。只有明确 succeeded 才创建 Grant，重复/并发最多一个。
- 若撤回 provider，保留同一 Packet Schema 和 deterministic/unavailable 模式；不迁移 advisory 为权限事实，也不删除已审计的 Grant。

## 可核验链接

| 证据 | 链接与定位 |
|---|---|
| Advisory DTO、脱敏、禁用字段与 unavailable | [`decision_packets.py`](../../apps/api/src/accesspilot/decision_packets.py#L46-L112)；[`_review_advisory`](../../apps/api/src/accesspilot/decision_packets.py#L194-L259) |
| 冻结 Packet、固定路线与唯一持久化 | [`_packet_snapshot`](../../apps/api/src/accesspilot/decision_packets.py#L262-L363)；[`generate_decision_packet`](../../apps/api/src/accesspilot/decision_packets.py#L366-L455) |
| 审批不咨询模型 | [`require_approval_startable/start_approval_case`](../../apps/api/src/accesspilot/approvals.py#L90-L220)；[`test_approval_requires_packet_and_never_reconsults_advisory`](../../apps/api/tests/api/test_t21_decision_packet.py#L270-L291) |
| Provider/Packet 失败矩阵 | [`test_t21_decision_packet.py`](../../apps/api/tests/api/test_t21_decision_packet.py#L112-L220)；[`test_t21_decision_advisory.py`](../../apps/api/tests/risk/test_t21_decision_advisory.py#L65-L145) |
| 固定审批路线与管理员开通 | [`decide_approval`](../../apps/api/src/accesspilot/approvals.py#L241-L373)；[`provision_access`](../../apps/api/src/accesspilot/provisioning.py#L253-L397) |
| 幂等/unknown/越权零写 | [`test_t23_admin_provisioning.py`](../../apps/api/tests/api/test_t23_admin_provisioning.py#L161-L298)；[`test_t24_product_verification.py`](../../apps/api/tests/api/test_t24_product_verification.py#L189-L319) |
| 运行与边界 | [`v1.2 Product Manifest`](../evidence/accesspilot-v1.2-product-manifest.md#边界)；[`T21/T23 Ticket 验收记录`](../tickets/accesspilot-productized-agent-v1.2.md#t21--decision-packet-与风险建议) |
