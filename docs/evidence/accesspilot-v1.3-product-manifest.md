# AccessPilot v1.3 Product Manifest

**T42 状态：** `Verified`；独立验收修复轮 P0/P1/P2=0/0/0
**产品证据 revision：** `9e757fd433f10fbff22fba9654c54cc3b4e9bec2`
**产品状态：** `product_verified=true`
**面试状态：** `interview_ready=pending_user_verification`
**唯一当前指标源：** [T41 验收证据包](accesspilot-v1.3-t41-acceptance-2026-08-20.md)

本 Manifest 只转录 T41 在同一新 revision 上的实测结果，不从 v1.2 拷贝门禁数字，也不用 Spec 目标值代替实测。T41 浏览器主证使用的 `apps/ + scripts/` 内容身份为 `ffcdb3151df94fe4795b989a23f798e150869060c1073548d602f46b011c9e8a`；该内容与 T41 本地提交中的产品/门禁内容一致。

## T41 本轮实测指标

| ID | T41 实测值 | 口径/源 |
|---|---|---|
| M-API | `868 passed, 0 skipped` | T41 §3；包含 v1.2 ACL / Decision Packet / 审批 / IAM 攻击与回归矩阵 |
| M-WEB | `109 passed, 0 skipped` | T41 §3；Vitest 完整 Web 分母，并通过 ESLint、两套 TypeScript 和 Vite build |
| M-PARITY | `33 个语义场景 × 2 轮`，两轮都零差异，zero-difference rounds `2/2` | 每轮 route/outcome `28/28`、path `28/28`、confirmation 3 场景；详细场景 ID 见 T41 §3 |
| M-EVAL | fresh `15/15` 固定场景、`101/101` product cases、安全攻击 `4/4`、reconnect duplicate `0/2` | 先对 disposable DB 执行 Alembic upgrade，再用 [run-t41-product-evals.py](../../scripts/run-t41-product-evals.py) 重跑；不是历史 `425 / 87 / 101` 复用 |
| M-ROLLBACK | `9 passed, 0 skipped`；完整 drill `1/1 = 100%` | T41 §5；[run-t41-rollback.py](../../scripts/run-t41-rollback.py) 包含 7 个早期失败阶段、非法前缀和完整回滚 |
| M-RECOVERY | browser restart `1/1`，rollback `1/1`；restart + rollback `2/2 = 100%` | 两个分母都是 T41 本轮新跑，未复用历史恢复数字 |
| M-EVENT | terminal event 完整率 `7/7 = 100%` | disposable browser DB 中 7 个非 running turn 全部有同 Workspace terminal |
| M-SIDE-EFFECT | side-effect step `4/4` completed | 同一 browser DB 的步骤事实；live pending 0、resolved pending 1 |
| M-LEAK | 泄漏数 `0` | fresh eval 攻击矩阵与浏览器 security probe 都未显示 prompt、凭据或隐藏推理 |
| M-BROWSER-START | 3 个 chat HTTP 小样本 response-start `[51.0, 38.9, 40.0] ms`；p50 `40.0 ms`，nearest-rank p95 `51.0 ms` | 最终新私有 Workspace，n=3；不外推为路径级或生产延迟 |
| M-BROWSER-END | 同一 n=3 completion `[217.2, 179.6, 228.7] ms`；p50 `217.2 ms`，p95 `228.7 ms` | 本地 deterministic offline browser 主链，不是外部 Provider 端到端性能 |
| M-EVAL-SAMPLE | fresh eval 单样本 first event `9.30775 ms`、first token `37.595958 ms`、completion `44.574791 ms`、charged model calls `1` | 单样本，明确不写成 p50/p95；未聚合每轮 retry/tool call 分布 |

## 指标 lineage 与不可填项

- v1.2 的 API 425 / Web 87 / product 101 是历史基线；T41 只沿用了 product case 定义，在 fresh disposable DB 重跑 101/101，没有复用历史“通过”结果。
- `provider_token_latency`、`policy_recall_at_k`、`production_sla` 未测；不以浏览器 n=3 或单样本替代这些指标。
- 没有真实 Provider token 计量，因此没有 Token 成本结论；没有真实规模环境，因此没有生产 SLA、容量或可用性结论。
- 没有将 checkpoint 行数当作性能/质量指标，也没有输出 checkpoint payload。

## 运行档案

- 真实产品路径：本地 Vite + FastAPI + disposable PostgreSQL + official PostgresSaver；Flow 2 JSON/SSE sticky 共用同一图引擎。
- 浏览器档案：`demo_mode_enabled` + `DeterministicEmbeddingModel` 无密钥路径；这是 deterministic offline browser 运行，没有伪造网络回包，也没有证明外部模型质量。
- 业务身份：四个 allowlist Mock Login 账号，服务端 AuthSession/Principal/ACL 为真实实现；不是真实 SSO/OIDC/SAML。
- 开通：确定性模拟 IAM 与唯一 Grant 是真实业务原型事实；没有真实 IAM/IGA、生产开通、回收或撤销。
- 所有员工、政策、系统、Case、Grant 和审批数据都是虚构本地数据。

## 限制与非目标

- Provider 与只读工具执行均为 at-least-once：`AFTER_TOOL_COMPLETION_BEFORE_EVENT` 恢复可重复工具调用，Provider 已返回但 completion 事实/checkpoint 未持久化时可能重调并重复计费。只有本地 quota/草稿/step/head/轨迹/terminal 最多一次；不把本地幂等外推到 Provider 或工具调用。
- 没有 ReAct、Multi-Agent、Supervisor/Worker、混合检索/重排、LangSmith/OpenTelemetry 可观测平台、Token 成本平台。
- strict serializer 在部分 checkpoint 读取中对未列入 allowlist 的 DraftPatch/SafeToolResult 记录过警告；本轮没有放宽 strict 模式，且重启、resume 和全门禁都通过，但不将观察包装为已解决的可观测能力。
- 手工停 API 时一个活跃 SSE 使首次 SIGINT 等待、第二次才终止，记为 shutdown 运维观察；未新增 shutdown 功能或指标。
- Vite 保留 588.78 kB single-chunk warning；构建通过不等于已完成前端分包优化。

## 结论门禁

`product_verified=true` 来自 T41 对 AC-01–AC-13 的同 revision 验收。T42 只准备 AC-14 证据和候选表述；它不推导用户本人已掌握，也不修改正式简历。
