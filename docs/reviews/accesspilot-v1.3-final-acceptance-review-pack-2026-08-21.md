# AccessPilot v1.3 最终验收交叉评审包

**评审类型：** `final-acceptance`

**评审包版本：** 2026-08-21 / `final-acceptance-pack-v1`

**当前 HEAD：** `f56912d12b3dde6597389ee4c6fdd63f71bca202`

**固定产品验收 revision：** `9e757fd433f10fbff22fba9654c54cc3b4e9bec2`

**权限边界：** 只读评审；不得修改、暂存、提交、push、merge、deploy、删除数据或修改正式简历。不得读取或输出 `.env`、密钥、私有地址及无关项目数据。

## 1. 目标与非目标

目标：判断 AccessPilot v1.3 是否已经满足 Canonical Spec 的完成定义和 AC-01–AC-14，能否形成“可交给用户决定是否发布”的最终结论。

本版本目标包括：真实 JSON/SSE 生产 `CompiledStateGraph`、官方 PostgreSQL checkpointer、持久 interrupt/resume、重放幂等、真实 pgvector RAG、只读可信轨迹、sticky 切流与 Legacy 回滚、完整质量门禁和可追溯 Claim Ledger。

明确非目标：ReAct、自主工具选择、Multi-Agent、混合检索/重排、真实企业 SSO/IAM、生产 SLA、Token 成本平台、OpenTelemetry/LangSmith、模型思维链展示。正式简历不在本次修改范围；`interview_ready=pending_user_verification`。

## 2. 权威产出物

- [Canonical Spec](../specs/accesspilot-langgraph-agent-loop-v1.3.md)
- [T26–T42 Tickets](../tickets/accesspilot-langgraph-agent-loop-v1.3.md)
- [T41 同 revision 验收证据](../evidence/accesspilot-v1.3-t41-acceptance-2026-08-20.md)
- [v1.3 Change Ledger](../evidence/accesspilot-v1.3-change-ledger.md)
- [Product Manifest](../evidence/accesspilot-v1.3-product-manifest.md)
- [Claim Ledger](../evidence/accesspilot-v1.3-claim-ledger.md)
- [Interview Evidence Pack](../evidence/accesspilot-v1.3-interview-evidence-pack.md)
- [T42 证据审计脚本](../../scripts/verify-t42-evidence.py)

评审者必须亲自完整读取仓库根 `AGENTS.md`，并以当前 Spec/Ticket 为合同；不能只依赖本摘要。

## 3. 已确认决策与验收口径

- PostgreSQL 业务表是身份、草稿、正式申请、审批与 Grant 的唯一权威源；checkpoint 只保存执行位置和安全派生状态。
- 模型不审批、不开通、不修改身份；经理、数据负责人和 IAM 继续走确定性领域状态机。
- Provider 与只读工具执行为 at-least-once；只有本地 quota、草稿 revision、step/head、轨迹和 terminal 在已声明边界内最多一次。
- JSON/SSE 必须按 Workspace sticky flow 使用同一引擎；running/pending/flow 冲突固定失败闭合。
- 任何 `product_verified` 结论只绑定 T41 同一产品 revision；T42 只增加文档和审计脚本。

## 4. 当前证据

T41 在固定内容 revision 上的完整门禁：

- API：`868 passed, 0 skipped`；Web：`109 passed, 0 skipped`；
- Alembic upgrade/check、Ruff、MyPy 58 个源码文件、ESLint、两套 TypeScript、Vite build 全部通过；
- 固定 33 个 v1.3 语义场景连续两轮，route/outcome/path 均 100%，两轮零差异；
- fresh product eval：15/15 scenarios、101/101 cases、4/4 安全攻击阻断；
- rollback wrapper：9/9；真实 restart + rollback 恢复 2/2；轨迹必需事件 7/7；副作用步骤 4/4；敏感泄漏 0；
- 真实 flow 2 浏览器完成：1440×900、1024×768、390×844、键盘路径、RAG、安全拒绝、可恢复错误、重启 pending/resume、EMP-001→002→003→004 的 Request→Packet→两级审批→IAM→唯一 Grant 与跨登录读回；console/network 错误 0；
- disposable T41 资源已精确清理；来源不明的既有 11 个测试 DB / 22 个角色保持未动，不归因本轮也不擅自删除。

T42 当前 revision 的审计：

- 六条 Claim 6/6、T41 指标来源 14 项；
- mutation probes 8/8 rejected；
- 当前 HEAD 必须是固定 T41 revision 的后代；实现、测试、T41 指标源均从固定 revision 读取；非后代 fail-closed；
- Ruff、diff-check 通过；正式简历未修改；冻结本评审输入时工作区干净。冻结后只新增本评审包、两份外部只读结果、决策记录与最终验收证据，并做精确文档一致性修复；T41 之后没有产品代码变化。

## 5. 已知非阻塞项与授权边界

- T39 P2：reduced-motion 下折叠箭头仍有 150ms transition；不影响键盘、主动作或内容可读性。
- T40 P2：后台 cleanup task 异常的额外观察性可更强；现有唯一 terminal、lease/head 与断线闭合合同均已通过。
- 未授权 push、merge、deploy、修改正式简历或删除 Legacy/checkpoint/来源不明测试资源。

## 6. 聚焦评审问题

请独立质疑主控的“最终验收初检已通过”结论：是否存在通过受支持输入、部署模型或当前证据可达的 P0/P1，使 AC-01–AC-14、回滚、真实状态、视觉/可访问性、证据可复现性或发布就绪度仍不成立？

按严重度输出：

`问题 → 证据或原因 → 影响 → 最小改进建议`

每个 P0/P1 必须同时指出违反的验收标准或不变量、可达路径、具体后果和最小充分修复；否则降为 P2/P3。若没有 P0/P1，请明确给出是否同意结论：`可交给用户决定是否发布`。
