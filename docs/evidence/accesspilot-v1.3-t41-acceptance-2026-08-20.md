# AccessPilot v1.3 T41 验收证据包（Verified）

**Ticket：** T41 — v1.3 全量门禁、真实浏览器与回滚演练

**实现基线：** `51ce85f`（T40 Verified）

**验收日期：** 2026-08-21

**状态：** `Verified`；独立验收修复轮 P0/P1/P2=0/0/0，`product_verified=true`；用户已授权仅本地提交，未推送、合并或部署。

## 1. 口径与边界

- 全部场景使用虚构员工、权限、政策和新建 disposable PostgreSQL；没有使用真实企业数据，没有输出凭据。
- 浏览器通过仓库支持的 `demo_mode_enabled` + `DeterministicEmbeddingModel` 无密钥路径运行，未添加 fixture、测试入口、网络拦截或模型假回包。
- 评测的 101 个产品 case 与 v1.3 parity 严格分开；parity 的固定分母是 33 个语义场景，连续两轮。
- 本轮没有将 skip 计为通过；API、Web、parity 和 rollback 报告均由脚本检查 `skipped == 0`。
- P1 回修后的最终浏览器取证冻结内容身份为：HEAD `51ce85f2b28ef769330a0ebc30751526fc5d6e90`；`apps/` + `scripts/` 下 tracked/untracked 文件名与内容的确定性 SHA-256 为 `ffcdb3151df94fe4795b989a23f798e150869060c1073548d602f46b011c9e8a`。浏览器开始前与完成后两次计算完全相同；截图和本文不在该生产/门禁内容哈希范围内。复算算法为：合并 `git ls-files apps scripts` 与 `git ls-files --others --exclude-standard apps scripts`，按 `LC_ALL=C sort -u` 排序；每个现存文件向外层哈希写入 `<path><两个空格><sha256sum 输出>`，删除项写入 `<path><两个空格>DELETED`，最后再执行一次 `/sbin/sha256sum`。主控与独立验收者均按该算法复算出同一值。

## 2. 可信红灯与最小修复

1. 首轮全 API wrapper 未显式传递 strict msgpack，得到 `44 failed / 813 passed`；补齐与产品运行相同的 `LANGGRAPH_STRICT_MSGPACK=true`。
2. 第二轮为 `7 failed / 850 passed`：6 条 T40 是 wrapper 把空模型 key 传成“已配置”，改为从子进程环境移除模型 key；1 条 T30 是真实 PostgreSQL 测试仍用过时的 runtime context/节点 monkeypatch，只更新测试以调用当前生产合同，保留真 checkpoint canary。定向 T30 `1/1`、T40 `16/16` 后全量转绿。
3. 首轮 fresh product eval 未在 disposable DB 上先跑 Alembic，可信失败为“业务表不存在”；先 upgrade head 后才执行固定 manifest。
4. rollback drill 首轮证据测试错把 Legacy 后续轮的首事件限定为旧名称；改为校验当前 Legacy 终态事件与数据不变式，未修改 `rollback.py`。
5. 真实浏览器在 checkpoint 重启后先暴露“服务端已确认、UI 仍未确认”。首轮修复虽让所有 SSE draft 都能更新 UI，却没有 `draft_revision` 单调合同。独立验收构造 `snapshot R → current-turn R+1 → delayed workspace R` 后可使 UI 回退。新增 WorkbenchRuntime 可信红灯共 13 条，其中 3 条失败：旧 revision 会覆盖、同 revision 冲突不会 fail closed/reload、缺失/非法 revision 仍会写 draft；同 revision 同内容幂等已通过。
6. 最小生产修复为一个共享 revision reducer/ref：`< current` 拒绝；`== current` 且同内容幂等；同 revision 不同内容进入错误态并调用 `/api/drafts/current` 权威 reload；缺失/非法 revision 不写；只有 `> current` 原子推进。用户在本地输入框中的文字不构成服务端 draft，preview 返回后才进入 server-authoritative draft。`/api/drafts/current`、preview、Workspace snapshot、current-turn JSON/SSE 与必要 DTO 均携带权威 `draft_revision`。
7. 第一轮修复后的真实浏览器又发现同 revision null/object 冲突：`/api/drafts/current` 是 `{draft:null, draft_revision:0}`，但只读 turn terminal 发出空对象 draft、revision 仍为 0。API 新红灯精确得到 `draft is {}` 而期望 `null`；最小修复让 JSON/SSE terminal 和 LangGraph 持久化 terminal 都重新读取 Workspace 权威 draft/revision，当前无 draft 时不伪造空对象。定向 API 消费者 `4/4`、WorkbenchRuntime `14/14`、WorkbenchRuntime + ChatThread `18/18` 后转绿。
8. rollback helper 的 `try/finally` 原先在 CREATE ROLE/DB 之后才开始。新增 setup / Settings / extension / Alembic / checkpoint 分阶段资源集合测试；Settings 失败红灯显示前态无资源、后态残留一个本轮 role。修复后从第一次 CREATE 起进入分阶段 `try/finally`，只按已成功创建的精确 validated ID 清理；T41 使用专属 `t41rb` 前缀。wrapper 自行设置并由子测试验证 `LANGGRAPH_STRICT_MSGPACK=true`，不再依赖调用方环境。
9. 最终 integrated gate 第一轮有 3 个陈旧测试仍期待未持久化空 draft；生产合同此时正确返回 null/revision 0。只同步 `test_t38_json_langgraph`、`test_t27_orchestrator` 和 fixed eval 的直接期望，定向 `4/4` 后从头运行第二次完整 gate；这三条是旧测试预期，不是新增产品失败。

## 3. AC1：同一工作树的全量门禁

最终命令：

```bash
ACCESSPILOT_T29_ADMIN_DATABASE_URL=<local-admin-url> \
ACCESSPILOT_T41_EVIDENCE_DIR=/private/tmp/accesspilot-t41-rework-final2-20260821 \
./scripts/verify-t41.sh
```

最终实测：

- Alembic fresh `upgrade head` + `alembic check`：通过，disposable DB 前缀 `t41_alembic_`。
- 全 API pytest：`868 passed, 0 skipped`；包含 v1.2 ACL / Decision Packet / 审批 / IAM 攻击与回归矩阵。仅有 2 条既有 JUnit `record_property` warning。
- Ruff：通过。MyPy strict：58 个 source files 通过。
- Web Vitest：`109 passed, 0 skipped`；ESLint、`tsconfig.app`、`tsconfig.node`、Vite production build 通过。
- 受影响前端定向回归：`WorkbenchRuntime` `14/14`；`WorkbenchRuntime` + `ChatThread` `18/18`。T39/T40 受影响消费者也包含在最终全 Web `109/109` 中。
- rollback wrapper 在调用方显式 unset strict 的情况下：`9 passed, 0 skipped`。
- fresh 产品评测：15/15 固定场景、101/101 product cases、4/4 安全攻击阻断、reconnect duplicate `0/2`。
- Vite 仅有既有 588.78 kB single-chunk warning，不影响构建结果。

### v1.3 parity 固定分母

两轮各运行 23 条 pytest，共固定 33 个语义场景；两轮都是零差异：

- route/outcome 一致率：`100%`（28 场景）；
- path 一致率：`100%`（28 场景）；
- confirmation semantics：3 场景；
- zero-difference rounds：`2/2`。

场景 ID：

```text
readonly.help
readonly.security_probe
readonly.numeric_no_cursor
readonly.numeric_non_duration
readonly.numeric_invalid
readonly.numeric_over_limit
readonly.eligible_access
readonly.active_access
readonly.request_status
readonly.policy_catalog
readonly.self_approval
readonly.policy_grounded
readonly.policy_insufficient
readonly.policy_unavailable
request.missing_fields
request.canonical_eligible
request.alias_matched
request.old_draft_without_current_entitlement
request.safe_justification_cursor
request.ambiguous_entitlement
request.canonical_but_ineligible
request.invalid_resolver_argument
request.validation_failed
request.malformed_then_retry_success
request.malformed_twice
request.provider_http_failure
request.revision_race
request.numeric_duration
request.quota_primary
request.quota_retry
confirmation.explicit_confirm
confirmation.explicit_reject
confirmation.non_confirmation
```

## 4. AC2：真实浏览器与产品主链

P1 回修后的所有主证均在新的 ego-browser task space 8 中重做，使用真实 Vite/FastAPI/disposable PostgreSQL/official PostgresSaver；API 中途重启后继续使用原 DB 与 checkpoint。task space 开始、结束时的 HEAD 与 `apps/` + `scripts/` 内容身份均为 §1 所列同一值；旧 task space 截图未继续作为同 revision 主证。

- grounded chat RAG：对话主链输入“请查询政策：客户数据导出权限如何审批，有什么期限规定？”，路由 `policy`，真实节点 `retrieve_policy_pgvector`，返回 POL-004 / POL-003 / POL-005 / POL-008，回答引用 POL-004 / POL-003。
- 只读权限解析：“我的权限”和“客户数据导出”使用只读路径，唯一解析为 `insighthub.customer_export`。
- 安全探测：主链输入伪造的 prompt/DB password/chain-of-thought 索取，产品安全拒绝，浏览器输出泄漏数 `0`。
- 可恢复错误：新私有 Workspace 用真实键盘 Enter 提交虚构未知权限；Flow 2 轨迹显示 `request_access → resolve_entitlement → 生成可恢复错误答复`，输入框仍可继续使用。
- checkpoint 重启：完整申请在 Flow 2 进入 pending；停止 API 并使用同一 DB/checkpoint 重启后，草稿、pending 和最近三轮仍在，键盘确认成功恢复。重启恢复成功率 `1/1 = 100%`。
- 四角色闭环：EMP-001 创建正式 Request/immutable Packet；EMP-002 直属经理审批；EMP-003 数据负责人审批；EMP-004 权限管理员仅看到已批准 Case 并执行 IAM。实测 1 份 Request、1 份 Packet、1 份 approved Case、2/2 approved steps、1/1 succeeded provisioning attempt、1 份 Grant。EMP-001 重新登录后“我的申请”继续读到 Case、Packet、两级审批和 Grant。
- 最近三轮：重启恢复的原 Workspace 显示 RAG、security、pending 三轮；最终截图用新的私有 Workspace 显示 RAG、security、recoverable 三轮。每轮都有 `LangGraph · Flow 2 · accesspilot-langgraph-v1.3` badge与真实节点；pending 轮还显示 HITL 事实。
- 事件完整率：disposable browser DB 有 7 个 turn（6 completed + 1 历史 waiting_input），`7/7` 非 running turn 均有同 Workspace terminal event，即 `100%`；4/4 side-effect step 为 completed；live pending `0`、resolved pending `1`；全局 Workspace event 127 条。
- checkpoint 可观测计数：53 checkpoints / 223 blobs / 1158 writes；仅记录行数，未输出 checkpoint payload。
- 浏览器控制台/网络：在真实“我的申请”读取期间采集 14 个 CDP 事件，`Runtime.exceptionThrown=0`、`Log.entryAdded(error)=0`、`Network.loadingFailed=0`。

### 尺寸与键盘

- 1440×900：Flow 2 最近三轮轨迹。
- 1024×768：请求人跨登录回读已批准 Case、Packet 与真实 Grant。
- 390×844：Flow 2 可恢复错误轨迹。
- 完整键盘路径实测包括：Mock Login `Tab → Enter`、表单 `Enter` 发送、对话/轨迹 `ArrowRight/ArrowLeft`、重启后确认 `Tab → Enter`。390 视口从首页链接开始的顶层 Tab 顺序实测为首页链接 → 退出 → 权限助手 → 我的权限 → 政策中心 → 我的申请 → 当前选中的轨迹 tab。

截图：

- [1440 Flow 2 轨迹](assets/accesspilot-v1.3-t41/flow2-trajectory-1440.png)
- [1024 请求人 Grant 回读](assets/accesspilot-v1.3-t41/requester-grant-1024.png)
- [390 可恢复错误轨迹](assets/accesspilot-v1.3-t41/recoverable-error-trajectory-390.png)

### 可观测性能

- 最终新私有 Workspace 的 3 个 chat HTTP 样本：response-start `[51.0, 38.9, 40.0] ms`，实测 p50 `40.0 ms`、nearest-rank p95 `51.0 ms`；completion `[217.2, 179.6, 228.7] ms`，p50 `217.2 ms`、p95 `228.7 ms`。
- fresh product eval 的单样本（不冒充 p50/p95）：first event 9.30775 ms，first token 37.595958 ms，completion 44.574791 ms，charged model calls 1。
- product eval 明确未测：`provider_token_latency`、`policy_recall_at_k`、`production_sla`；未用目标值代替实测。

## 5. AC3：disposable PostgreSQL 完整回滚演练

`scripts/run-t41-rollback.py` 使用 helper 的 T41 专属 `t41rb` 资源前缀创建新数据库、迁移/runtime 角色和 checkpoint schema，演练完成后只清理已成功创建的精确 validated ID。wrapper 在外部未设置 strict 时自行设置并验证严格模式；最终 `9 passed, 0 skipped`，包括 7 个早期失败阶段、非法前缀拒绝和 1 个完整 drill。

同一演练证明：

1. flow 1 中先用四个独立会话创建旧 Workspace 的 Case 和 Grant。
2. flow 2 在新 Workspace 上使用 official PostgresSaver 写入 active pending/checkpoint。
3. 执行“停收 → 排空 → pending/checkpoint 双向对账”；preflight 在有 active pending 时拒绝切回。
4. 精确将 pending 标记为 `abandoned_to_legacy`，切 `flow_version=1`；保留 pending row、accepted checkpoint id、checkpoint head 和 Cursor，未执行删除。
5. 重启 Legacy 后，旧/新 Workspace、draft/event/Cursor、正式 Case/Grant 全部继续可读；后续 turn 由 Legacy 处理，不写新 graph ledger row。

rollback 恢复成功率 `1/1 = 100%`。与浏览器 checkpoint 重启合并的本轮恢复路径为 `2/2 = 100%`，两个分母均是本轮新跑，未复用历史数字。未发现 `rollback.py` 产品缺陷。

独立验收探针曾精确留下 disposable 数据库 `t35chk_6c3cf441aa7f` 与角色 `t35m_6c3cf441aa7f`、`t35r_6c3cf441aa7f`。清理前这三个精确资源均存在，`t35*` 总量为 12 DB / 24 roles；按本 Ticket 的 disposable 清理授权只删除这三项，清理后它们均不存在，`t35*` 总量为 11 DB / 22 roles。该删除不可恢复，但探针没有业务数据。余下 11 DB / 22 roles 来源无法由本轮日志精确归因，全部保留未动。最终 browser runtime 另行精确清理后，`t41_browser_*` DB 与 `t41b*` roles 均为 0。

## 6. 泄漏、残余边界与修改范围

- 泄漏数：fixed product eval 4/4 攻击全部阻断，duplicate 0；额外 browser security probe 未显示 prompt、凭据或隐藏推理，本轮安全输出泄漏数 `0`。
- 无外部模型密钥时 Decision Packet 的 AI 建议诚实显示 `unavailable`，已验证事实、政策和固定人工路线不受影响。
- 手工停止本地 API 时，一个活跃 SSE 使第一次 SIGINT 等待，第二次终止后正常重启；本轮无数据丢失，记为非阻塞运维观察，不扩大到新的 shutdown 功能。
- strict serializer 在部分 checkpoint 读取中记录了拒绝未列入 allowlist 的 `DraftPatch` / `SafeToolResult` 警告；严格模式没有放宽，重启草稿、pending resume、terminal event 和全量门禁均通过，本轮未得到可达的数据或功能后果，记为非阻塞运行观察。
- T39 reduced-motion P2 和 T40 cleanup P2 未改变本轮门禁结果，未扩大范围。

T41 实现修改：

- `apps/api/tests/db/test_t30_postgres.py`
- `apps/api/tests/db/test_t35_postgres.py`
- `apps/api/tests/db/test_t41_rollback_drill.py`
- `apps/api/tests/evals/test_t41_release_gate.py`
- `apps/api/src/accesspilot/agent/json_orchestrator.py`
- `apps/api/src/accesspilot/main.py`
- `apps/api/tests/api/test_chat_stream.py`
- `apps/api/tests/api/test_draft_preview.py`
- `apps/api/tests/api/test_t16_business_facts.py`
- `apps/api/tests/api/test_t20_case_acl.py`
- `apps/api/tests/api/test_t38_json_langgraph.py`
- `apps/api/tests/conversation/test_t27_orchestrator.py`
- `apps/api/tests/evals/test_fixed_scenarios.py`
- `apps/web/src/App.test.tsx`
- `apps/web/src/WorkbenchRuntime.tsx`
- `apps/web/src/WorkbenchRuntime.test.tsx`
- `apps/web/src/api.test.ts`
- `apps/web/src/api.ts`
- `apps/web/src/auth.test.ts`
- `apps/web/src/types.ts`
- `scripts/assert-t41-test-report.py`
- `scripts/run-t41-alembic.py`
- `scripts/run-t41-api-suite.py`
- `scripts/run-t41-browser-runtime.py`
- `scripts/run-t41-parity.py`
- `scripts/run-t41-product-evals.py`
- `scripts/run-t41-rollback.py`
- `scripts/verify-t41.sh`
- `docs/evidence/accesspilot-v1.3-t41-acceptance-2026-08-20.md`
- `docs/evidence/assets/accesspilot-v1.3-t41/*.png`

本轮没有改动生产数据、真实 Legacy/checkpoint、`.env` 或正式简历。四份 canonical 状态文档的工作树变更由上游主控持有，不属于本实现者的修改清单。

## 7. 独立验收与当前停点

独立验收从头运行 T41 release gate，确认 Alembic、API、Ruff、MyPy、两轮 parity、rollback、Web、ESLint、两套 TypeScript、Vite 与 fresh product eval 全部通过。首轮发现两项 P1：两条 SSE 乱序可使旧 draft revision 覆盖新值；rollback helper 的早期失败发生在清理保护建立前并泄漏 disposable 资源。两项均先由可信红灯复现，再完成最小修复。

修复轮独立实测：

- WorkbenchRuntime、ChatThread、App/API/Auth 直接消费者 `36/36`；API/T27/T38/fixed-eval 权威 draft 消费者 `5/5`，0 skip；
- rollback wrapper 在调用方 unset strict 时 `9/9`，0 skip；七个早期失败阶段、非法 prefix 与完整 drill 均没有新增 disposable 资源；
- Ruff、MyPy 58 个源码文件、ESLint、两套 TypeScript 与 `git diff --check` 通过；
- 独立复算 HEAD 与 `apps/ + scripts/` 内容身份均和 task space 8 主证一致；三张新截图尺寸和内容匹配，旧跨 revision 截图未计入主证；
- exact `6c3` 探针资源与所有 `t41rb`、`t41_suite`、task 8 browser 资源均不存在；来源不明的既有 11 个测试 DB / 22 个角色保持未动。

T41 三条验收标准与 AC-01–AC-13 已在同一内容 revision 上通过，故设置 `product_verified=true`。T41 等待按用户长期授权仅本地提交；T42 尚未开始，`interview_ready` 或正式简历表述仍不得据此自动成立。
