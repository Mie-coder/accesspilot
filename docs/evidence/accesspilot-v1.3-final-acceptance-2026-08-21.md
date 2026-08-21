# AccessPilot v1.3 整体最终验收

**日期：** 2026-08-21

**最终验收输入 HEAD：** `f56912d12b3dde6597389ee4c6fdd63f71bca202`

**固定产品验收 revision：** `9e757fd433f10fbff22fba9654c54cc3b4e9bec2`

**Ticket 状态：** T26–T42，17/17 `Verified`

**产品状态：** `product_verified=true`

**面试状态：** `interview_ready=pending_user_verification`

## 1. 验收范围

本轮整体验收只判断 v1.3 Canonical Spec 完成定义与 AC-01–AC-14，不把本地作品集原型外推为真实企业上线。正式简历、push、merge、deploy、正式 canary、真实 SSO/IAM 和来源不明的历史测试资源均不在授权范围。

## 2. 产品与质量证据

固定 T41 revision 的同一轮验收结果：

- API `868 passed, 0 skipped`；Web `109 passed, 0 skipped`；
- Alembic upgrade/check、Ruff、MyPy 58 files、ESLint、两套 TypeScript 和 Vite build 通过；
- 固定 33 个 v1.3 语义场景连续两轮，route/outcome/path 均 100%，两轮零差异；
- fresh product eval 15/15 scenarios、101/101 cases，安全攻击 4/4 阻断；
- rollback 9/9，restart + rollback 2/2，必需事件 7/7，副作用步骤 4/4，敏感泄漏 0；
- 真实 PostgreSQL/official PostgresSaver、JSON/SSE 双入口、persistent interrupt/resume、精确 head、lease/fence、故障恢复和 Legacy 回滚均有定向证据；
- flow 2 浏览器主链在 1440×900、1024×768、390×844 完成键盘路径、RAG、安全拒绝、可恢复错误、API 重启后的 pending/resume，以及 EMP-001→002→003→004 的 Request→Packet→两级审批→IAM→唯一 Grant 与跨登录读回；console/network error 0。

完整来源见 [T41 验收证据](accesspilot-v1.3-t41-acceptance-2026-08-20.md) 与 [Product Manifest](accesspilot-v1.3-product-manifest.md)。T42 的六条 Claim、14 项指标来源、8/8 mutation probes 和边界披露均已独立复核，正式简历未修改。

## 3. 最终交叉评审

- [Claude Opus 5 评审](../reviews/accesspilot-v1.3-final-acceptance-claude-opus-review-2026-08-21.md)：P0/P1/P2/P3 = 0/0/2/3；同意 `可交给用户决定是否发布`。
- [DeepSeek 评审](../reviews/accesspilot-v1.3-final-acceptance-deepseek-review-2026-08-21.md)：P0/P1/新 P2/新 P3 = 0/0/1/0；同意 `可交给用户决定是否发布`。
- [主控决策记录](../reviews/accesspilot-v1.3-final-acceptance-decisions-2026-08-21.md)：接受并修复评审产物白名单/冻结时点措辞和根 README 漂移；其余均为非阻塞披露。

两位外部评审无 P0/P1，也无需要用户裁决的技术分歧。外部模型均为只读，未获得代码修改或发布权限。

## 4. 已知非阻塞限制

- reduced-motion 下折叠箭头仍有 150 ms 过渡；
- 后台 cleanup task 异常观察性仍可增强；
- strict serializer allowlist 在依赖/状态类型变化时需要维护；
- 活跃 SSE 下本地首次 SIGINT 可能等待；
- Vite 构建保留约 588.78 kB 单 chunk warning；
- Mock Login、模拟 IAM、deterministic/offline 浏览器样本、Provider/只读工具 at-least-once 均不是生产身份、真实外部副作用或 exactly-once 保证；
- `provider_token_latency`、`policy_recall_at_k`、`production_sla` 未测；没有真实 SSO/IAM、ReAct、Multi-Agent、成本平台或集中可观测平台。

这些限制不推翻当前本地合同，但必须在演示、面试和任何未来发布说明中继续披露。

## 5. 最终结论与授权边界

Canonical Spec AC-01–AC-14 已完成，T26–T42 全部 Verified，产品和证据链不存在已知 P0/P1。最终结论：

> **可交给用户决定是否发布。**

截至本验收完成，未 push、merge、deploy、修改正式简历或删除 Legacy/checkpoint/来源不明测试资源。发布和正式简历采用仍需用户另行明确授权。
