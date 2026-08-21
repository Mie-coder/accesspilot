# Documentation Map

- `adr/`：已经确认的架构决策及其理由。
- `architecture/`：系统流程、数据模型与边界说明。
- `design/`：界面设计语言与交互原则。
- `plans/`：产品级实施计划。
- `product/`：已确认的产品定位、角色、功能模块和完整体验边界。
- `reviews/`：交叉评审包、外部模型状态、议题清单和决策记录。
- `specs/`：版本需求、范围和验收标准。
- `tickets/`：按顺序实施并独立验收的任务卡。
- `learning/`：课程、复盘、学习记录和教学资源。
- `superpowers/`：早期教学阶段形成的历史实施计划，保留用于追溯，不作为当前产品入口。

根目录 `README.md` 同时记录 v1.1/v1.2 历史基线和当前本地完成的 v1.3 T26–T42；Mock 边界、未测指标和个人掌握度不得误写成已验证能力。

## AccessPilot v1.2 历史入口

- 产品定义：`product/accesspilot-product-function-book-v1.2.md`
- Canonical Spec：`specs/accesspilot-productized-agent-v1.2.md`
- 精简前 DeepSeek 历史评审：`reviews/accesspilot-v1.2-spec-deepseek-review-2026-08-11.md`
- T18–T25 精简 Ticket：`tickets/accesspilot-productized-agent-v1.2.md`
- 产品验收：`evidence/accesspilot-v1.2-product-manifest.md`
- 面试证据包：`evidence/accesspilot-v1.2-interview-evidence-pack.md`
- Claim Ledger：`evidence/accesspilot-v1.2-claim-ledger.md`
- 最终 DeepSeek 评审：`reviews/accesspilot-v1.2-final-acceptance-deepseek-review-2026-08-12.md`

T18–T25 的历史产品与证据产物已完成，`product_verified=true`；`interview_ready=pending_user_verification`。该历史交付仅有本地提交，没有推送、合并或部署；当前真实能力以根目录 `README.md`、对应测试和 v1.3 evidence 为准。

## AccessPilot v1.3 证据入口

- [Canonical Spec](specs/accesspilot-langgraph-agent-loop-v1.3.md)
- [串行 Ticket](tickets/accesspilot-langgraph-agent-loop-v1.3.md)
- [T41 同 revision 产品验收](evidence/accesspilot-v1.3-t41-acceptance-2026-08-20.md)
- [Product Manifest](evidence/accesspilot-v1.3-product-manifest.md)
- [Claim Ledger](evidence/accesspilot-v1.3-claim-ledger.md)
- [面试候选表述、Agent Loop 讲解图与 Demo 索引](evidence/accesspilot-v1.3-interview-evidence-pack.md)
- [T42 定向文档审计](../scripts/verify-t42-evidence.py)

- [最终验收交叉评审包](reviews/accesspilot-v1.3-final-acceptance-review-pack-2026-08-21.md)
- [Claude Opus 5 最终评审](reviews/accesspilot-v1.3-final-acceptance-claude-opus-review-2026-08-21.md)
- [DeepSeek 最终评审](reviews/accesspilot-v1.3-final-acceptance-deepseek-review-2026-08-21.md)
- [最终交叉评审决策](reviews/accesspilot-v1.3-final-acceptance-decisions-2026-08-21.md)
- [整体最终验收](evidence/accesspilot-v1.3-final-acceptance-2026-08-21.md)

v1.3 的 T26–T42 已全部 `Verified`，T41 设置 `product_verified=true`；T42 独立验收修复轮 P0/P1/P2=0/0/0。Claude Opus 5 与 DeepSeek 最终评审 P0/P1 均为 0，结论为“可交给用户决定是否发布”。`interview_ready=pending_user_verification`；候选表述不是正式简历变更，也不代表用户本人已完成无稿复现。
