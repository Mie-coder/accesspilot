# AccessPilot v1.2「上下文意图与可信多角色闭环」Tickets（T18–T23 已完成）

**状态：** 用户已确认按 T18 → T25 串行实施；T18–T23 已独立验收，T24–T25 待开始；不推送、合并或部署
**更新时间：** 2026-08-12
**继承基线：** v1.1，HEAD `c683d84`，T01–T17 已完成
**产品说明书：** `docs/product/accesspilot-product-function-book-v1.2.md`
**Canonical Spec：** `docs/specs/accesspilot-productized-agent-v1.2.md`

## 0. 执行边界

- 本文件取代旧的 T18–T35 扩张版草案；
- 用户确认后默认严格串行执行 T18 → T25，一次只实现一张；
- 每张实现 Ticket 先写失败测试，再写最小实现；
- 每张完成后进行独立只读验证，通过后才允许本地提交；
- 当前停止点在 T22 开始前；T18–T21 已独立验收，T22–T25 尚未开始；
- 本 Ticket 列表不授权推送、合并、部署或修改正式简历；
- 到期回收、复杂预算/Attempt、Evidence Lab、完整外键 cutover、真实 SSO 和 168 小时门禁不进入任何 Ticket；
- `product_verified` 只由 T24 判定，`interview_ready` 只由 T25 判定。

## 1. 依赖与用户可见结果

| Ticket | 用户可见结果 |
|---|---|
| T18 | 输入 `111` 不再总返回帮助，而是根据当前问题给出正确结果 |
| T19 | 出现独立登录页，四个账号通过登录获得不同 Session |
| T20 | 不同账号可以安全地查看和处理同一正式 Case |
| T21 | Case 中出现来源清晰的 Decision Packet |
| T22 | 经理和数据负责人通过不同登录按顺序审批 |
| T23 | 只有权限管理员能开通，申请人重登后看到结果 |
| T24 | 四窗口全流程、越权攻击和质量检查全部通过 |
| T25 | 形成可在面试中独立讲解的证据包 |

## T18 — ConversationCursor 与纯数字上下文续答

**状态：** 已完成（2026-08-11 独立验收通过）

**目标：** 先修复用户输入 `111` 时被错误归为 `help` 的真实问题，让短输入由服务端待补字段上下文解释。

**Spec 映射：** FR-01；AC-01–AC-03。

**验收：**

1. T18 先以 Workspace+actor 为作用域；唯一活动 Cursor 的 expected_field=duration_days 且 draft revision 匹配时，合法纯正整数被确定性解析并通过目录上限校验。111 对 180 天上限可用 CAS 写入并递增 revision，对 30 天上限返回明确上限、不改 revision且 Cursor 继续有效，模型调用数不变。
2. 有效但等待 entitlement/justification/confirmation 时保持 request_access 并返回对应字段状态；无/失效 Cursor 时固定为 unknown/needs_clarification；显式帮助为 help/answered。上述非成功期限路径的草稿、revision、确认、模型配额、模型调用、工具调用和业务事实均不变。
3. Cursor 只在对应追问持久化为成功终态后激活，绑定 Workspace、actor、draft revision、expected_field 和最近追问；成功消费原子更新草稿、递增 revision并清除旧 Cursor，换题/身份/Workspace/revision/重置/提交使其失效。SSE 重建出的规范化 Outcome 与 JSON 的 intent、business_status、draft_revision、draft、message/error_code 一致。

**测试要求：** 先增加失败的路由、会话、CAS/重复消费、Cursor 持久化、目录上限、流中断、JSON/SSE Outcome 和固定评测案例；保留现有显式帮助、政策、安全探测和“数字+天”回归。

**依赖：** T17。

**主要风险：** 把任意数字都猜成期限会制造新的误填；只有有效、唯一的期限上下文可以触发确定性解析。

### T18 验收记录（2026-08-11）

- 红灯基线：修复前纯 `111` 被错误路由为 `help/answered`；新增失败测试先证明该行为错误。
- T18 实现与 registry：T18 专用测试与 registry 回归 30 passed；固定 T18 评测 11/11；T18 验收当时的历史组合分母为 T17 30 + T18 11 = 41/41；API 全量 373 passed；前端 48 passed，lint/build 通过。该 41 不是 T19 current registry 分母。
- 质量与迁移：Ruff、MyPy 通过；Alembic `0007` upgrade/downgrade/no-drift 通过；独立 verifier 结论 PASS（P0=0、P1=0）。
- P2 残余/延期边界：legacy JSON 及 `set_actor`/`reset`/`exit_demo`/submit 的极端并发最后写入者风险；未来真实 Answer Provider 的 canonical `assistant_message` 定义；无 Cursor 数字澄清文案暂固定引用 `111`。这些不扩大 T18 范围，后续 Ticket 再处理。
- T20 已完成共享 Case/ACL 独立验收；T21–T25 尚未开始；不推送、合并或部署。

## T19 — 独立 Mock 登录页、AuthSession 与 Principal

**状态：** 已完成（2026-08-12 独立验收通过）

**目标：** 用四个虚构账号的独立登录和服务端 Session 取代产品内直接切换 EMP 身份。

**Spec 映射：** FR-02、FR-07.1；AC-04、AC-05。

**验收：**

1. 未登录用户进入账号选择式 Mock Login；EMP-001～004 的 account_id 经服务端 allowlist 后，登录原子创建私有 Workspace、AuthSession、token hash、HttpOnly Cookie、可恢复/轮换的 CSRF、过期/注销状态和固定 Principal，角色只从 EmployeeRecord 派生，不实现密码或账号管理。
2. 所有业务路由只接受 accesspilot_session→AuthSession→Principal；旧 Workspace Cookie 单独访问必须 401，匿名 Workspace 入口不能形成身份。登出/过期/注销、篡改 Cookie、错误 Origin/CSRF，以及除 login account_id 外的保留身份字段注入均在副作用前稳定拒绝；旧 demo/session 与 actor 控制台不可用。
3. 四个 clean browser context 可以同时登录不同账号且互不覆盖；刷新恢复当前 Session，退出后不能继续访问。T18 Cursor 增加必填 auth_session_id 并在换账号时失效；Session 吊销只撤销登录，不删除 Workspace。

**测试要求：** allowlist、Auth cutover、Cookie/CSRF/Origin、严格身份字段 Schema、聊天身份声明、四上下文隔离、登录/退出/过期前端状态和关闭旧 Demo/匿名身份入口测试。

**依赖：** T18。

**主要风险：** 账号选择式 Mock Login 只能证明会话与 ACL，任何访问者仍可选择四个虚构账号，不能包装成真实用户认证。

### T19 验收记录（2026-08-12）

- 已实现 0008 `auth_sessions` 迁移、hash-only token/CSRF、四账号 allowlist、原子私有 Workspace 绑定、Session/Principal/Origin/CSRF 边界及旧 Demo/Workspace 入口关闭。
- T19 auth/session 与 migration 固定评测 23/23；连同严格 Draft Preview 合同的后端 focused 选择器在真实 test DB 共 38 passed。API 全量 382 passed（1 条既有依赖 warning）；前端 59 passed，lint 和 build 通过。
- 固定评测新增独立 `T19-01 auth_session_isolation`（23 cases，完整 T19 auth + migration 专测）。v1.1 T17 历史清单冻结为 30/30 @ `c683d84`；T19 current compatible/new registry 将已被产品边界取代的 `T17-01`/`T17-09` 标为 superseded，唯一计数为 active T17 24 + T18 11 + T19 23 = 58，不与历史 30 同比。
- T08 历史 12 条在 `c683d84` 留档；`eval01/05/06` 不再用 T19 的 404 替代原成功/乱序/驳回语义，而是明确 deferred 至 T20/T22；旧 Workspace 隔离 `eval11` 由 T19 AuthSession 隔离证据 supersede。当前 runner 仅收集 8 条语义仍兼容的案例且不注册 skip；其中 `eval07/08` 通过预置 approved Case 与确定性 IAM 保留 timeout/recover/幂等开通真实证据。完整四角色 E2E 由 T24 恢复。
- 当前 compatible/new 产品评测为 58/58（active T17 24 + T18 11 + T19 23），不与历史 T17 30/30 做同分母比较；Ruff、MyPy、Compose、Alembic `0008 → 0007 → 0008` 与 no-drift 均通过。
- 四个真实浏览器上下文分别登录 EMP-001～004，HttpOnly Session 对页面脚本不可见，刷新恢复原账号；退出 EMP-002 后另三个会话不受影响。登录后申请人由 Principal 自动展示，不再要求在对话中自报员工编号。
- 独立 verifier 结论 PASS（P0=0、P1=0），允许本地提交。已建立的事件长连接不会在 Session 注销后持续重验，作为 P2 纳入 T24 攻击矩阵；T24 前不得声明 `product_verified`。

## T20 — 共享 Access Case 与资源级 ACL

**状态：** 已完成（2026-08-12 独立验收通过）

**目标：** 在固定单 Demo 组织内，让正式申请按资源关系跨 Session 重读，同时保持草稿和聊天私有。

**Spec 映射：** FR-03、数据模型；AC-06、AC-07。

**验收：**

1. AccessRequest 继续作为 Case 根，既有 workspace_id 只保留为来源/兼容关系；v1.2 不新增组织表、不传播 organization_code、不删除旧外键，也不声明来源 Workspace 删除后 Case 仍存活。登出/过期/注销不得删除 Workspace，P0 禁止 Workspace GC。
2. Case 列表/详情在 SQL 查询中编码资源关系：requester 永久可读本人 Case；审批人可读已轮到或已决定的本人步骤但只有 pending 可操作；permissions_admin 可读全部已批准 Case及 failed/unknown/succeeded/Grant，但只有合法状态可开通/恢复。猜 ID、无关系和错角色均 404/403 且不泄露对象。
3. EMP-001 新 Session 可读取自己已提交的 Case，其他账号只在上述职责生效时可见；未提交草稿、对话和 Cursor 仍只属于原 Workspace，旧 Workspace Cookie不能授权，所有拒绝路径的模型/IAM/业务写入计数为零。

**测试要求：** 跨 Session 重读、已决定审批人刷新、管理员终态重读、私有 Workspace、IDOR/角色矩阵、旧 Workspace Cookie、SQL 过滤和 Session 吊销不删除业务事实测试。

**依赖：** T19。

**主要风险：** 只在前端隐藏按钮或加载后再过滤，仍会留下真实越权入口。

### T20 验收记录（2026-08-12）

- SQL ACL 按 requester、审批人 ID+可信角色、approved Case 管理员关系过滤；正式 Case 跨 Session 可读，草稿、事件和 Cursor 仍属私有 Workspace。
- T20 攻击矩阵 14/14、API 全量 396 passed、前端 61 passed；当前产品固定评测 72/72（active T17 24 + T18 11 + T19 23 + T20 14）。
- 独立 verifier 结论 PASS（P0=0、P1=0）；未引入 Packet、审批成功链、开通动作、组织表或迁移。

## T21 — 简化 Decision Packet 与受约束风险建议

**状态：** 已完成（2026-08-12 独立验收通过）

**目标：** 为每个正式 Case 生成一份可追溯决策材料，保留 AI 深度但不建设复杂异步风险平台。

**Spec 映射：** FR-04；AC-08、AC-09。

**验收：**

1. 只有申请人本人可为自己的 submitted Case 生成 Packet；提交页面自动调用一次并在失败时允许同一申请人重试。每个 Case 最多一个只读 Packet，冻结事实、目录路线、政策引用和版本；条目标记 verified_fact、policy_evidence、user_claim 或 advisory，并明示 provider/deterministic/unavailable。
2. DeepSeek 只接收脱敏冻结事实和本轮政策证据并返回严格 Schema；无 Key、超时、非法输出或未知引用时 Packet 明示 unavailable。模型返回的 clear/blocked/risk 只作为 advisory，不能跳过或阻断固定审批路线，fake/offline 不显示为真实 AI。
3. 生成动作先做 requester ACL 和状态校验，并发/重复最多保存一个 Packet；GET/审批人打开页面不生成 Packet或调用模型。现有 approval-case 入口只允许 requester、要求 Packet 已存在、只从 Packet/目录创建路线且不得再次调模型。

**测试要求：** Packet 唯一/来源/版本、Provider 成功与失败、非法引用、并发重试、GET 零调用、ACL 和前端来源标签测试；真实 DeepSeek smoke 单列且可选。

**依赖：** T20。

**主要风险：** 模型建议一旦混入确定性事实或审批路线，就会把可解释原型变成不可控风控黑箱。

### T21 验收记录（2026-08-12）

- 0009 新增唯一冻结 Decision Packet；四类来源、版本、受限脱敏 DeepSeek 与 provider/deterministic/unavailable 标签已实现。
- 后端与相关回归 39/39、前端 69/69；T21 固定评测 16 项，当前产品 registry 88 项。
- 独立 verifier 结论 PASS（P0=0、P1=0）；0009 downgrade→upgrade→head 通过，未进入 T22+。

## T22 — 独立账号的经理→数据负责人串行审批

**状态：** 已完成（2026-08-12 独立验收通过）

**目标：** 让 EMP-002 与 EMP-003 在各自登录会话中按确定性顺序处理同一个 Packet。

**Spec 映射：** FR-05；AC-10。

**验收：**

1. Packet 存在后，EMP-002 收到经理步骤；只有其通过后 EMP-003 才收到数据负责人步骤，两个账号均通过独立 Session 从数据库重读同一 Case。
2. 每次决定原子校验 Case 资源关系、当前 assignee、步骤顺序、非自审批、非终态和并发版本；错角色、自审批、越序、重复、并发和迟到操作均零副作用，驳回要求非空原因。
3. 登录切换、刷新和重新进入后，待办、评论、操作者、审批状态与只追加审计一致；审批动作不触发新的风险模型调用。

**测试要求：** 角色/顺序/并发/自审批矩阵、跨 Session 收件箱、刷新重读、驳回和审计唯一性前后端测试。

### T22 验收记录（2026-08-12）

- EMP-002 与 EMP-003 通过独立 Session 按固定顺序审批；`approval_step_id` 防止迟到误批，Principal ID+可信角色双重守卫。
- 后端与相关审批回归 28/28、前端 76/76；T22 固定评测 5 项，当前产品 registry 93 项。
- 独立 verifier 结论 PASS（P0=0、P1=0、P2=0）；无关系 pending/terminal/random Case 统一 404，未进入 T23。

**依赖：** T21。

**主要风险：** 如果仍按原 Workspace 查审批，独立登录只会变成新的 UI 外壳而不能共同处理 Case。

## T23 — 权限管理员显式开通与申请人重读

**状态：** 已完成并独立验收

**目标：** 复用 v1.1 已有幂等能力，但把开通职责收紧到 EMP-004，并让申请人跨登录看到唯一 Grant。

**Spec 映射：** FR-06；AC-11、AC-12。

**验收：**

1. 只有 permissions_admin Principal 可对已全部批准的 Case 执行开通；服务端生成稳定幂等键，普通用户、经理、数据负责人、模型和客户端自带身份/幂等字段均不能触发或改变操作。
2. 复用并扩展现有 ProvisioningAttemptRecord；provision/recover 使用同一 permissions_admin ACL。重复点击、并发、HTTP 重试和 unknown 恢复复用原 attempt/幂等键，最多产生一次模拟 IAM 副作用和一个 Grant；只有 IAM 明确成功才能创建 Grant。
3. EMP-001 退出并重新登录后，可在“我的申请”和 Case 时间线读取相同审批、开通和 Grant 事实；页面明确 v1.2 不包含到期回收。

**测试要求：** permissions_admin ACL、未批准/错角色、稳定幂等、成功/失败/unknown/并发恢复、唯一 Grant、管理员终态重读和申请人跨 Session UI 测试。

**依赖：** T22。

**主要风险：** v1.1 开通主要依赖 Workspace 且接受客户端幂等键，若只改按钮不改服务端仍可越权。

### T23 验收记录（2026-08-12）

- 只有 EMP-004 `permissions_admin` 可对 approved Case 开通；waiting/无关系/未批准资源统一 404，有关系但无职责返回 403。
- 服务端生成 `accesspilot:{request_id}` 稳定幂等键；repeat/concurrent/unknown recover 复用原 Attempt，最多一次 IAM 副作用与一条 Grant。
- 后端联合 31/31、前端 83/83；T23 固定评测 5 项，当前产品 registry 98 项。v1.2 明确不包含到期回收/撤销。
- 独立 verifier 结论 PASS（P0=0、P1=0）；未进入 T24。

## T24 — 四浏览器闭环、攻击矩阵与 Product Verification

**状态：** 待开始

**目标：** 在同一实际 revision 上证明精简版主线可运行、可攻击验证且没有把目标值冒充结果。

**Spec 映射：** 质量与证据、攻击矩阵；AC-01–AC-13。

**验收：**

1. 后端/前端测试、静态检查、迁移、构建和固定评测通过；固定评测覆盖 `111` 六类上下文、登录隔离、ACL、Packet 降级、审批和幂等开通，旧案例仅按保留/迁移/替换唯一计数。
2. 四个 clean browser context 在 1440×900 完成登录→申请→Packet→两级审批→开通→申请人重登，并演示至少一次越权拒绝；1024×768、390×844 与键盘路径只做响应式烟测。DeepSeek unavailable 使用无 Key 启动档案或测试夹具验证，不增加产品 UI/API 故障控制面。
3. Product Manifest 对 AC-01–AC-13 逐项链接当前 revision 的测试/运行证据和限制；只有 13 项全部通过时设置 product_verified=true，真实 DeepSeek 未 smoke 时标 N/A/unverified 且不形成对应 Claim。

**测试要求：** 完整质量命令、迁移升级、固定评测、攻击矩阵、四浏览器桌面主链和两档响应式/键盘烟测；由独立 reviewer 只读复核证据与当前 revision。

**依赖：** T18–T23。

**主要风险：** T24 只能汇总证据，不能通过修改分母、目标值或补写未实现功能获得全绿。

## T25 — ADR、Demo 主线与 Interview Evidence Pack

**状态：** 待开始

**目标：** 把精简版真正转化为候选人能独立解释的作品，而不是继续增加产品功能。

**Spec 映射：** Interview Evidence Pack；AC-14。

**验收：**

1. 完成三张 ADR：ConversationCursor 为什么不能把所有数字都猜成期限；Mock Login/Principal 为什么优于 actor 下拉框；模型建议为什么不能进入审批与副作用。每张包含替代方案、拒绝理由、代价、限制和对应测试。
2. 形成不超过 90 秒介绍、8–10 分钟四角色 Demo、三张决策卡和 Claim Ledger；每条 Claim 链接代码、测试、ADR、运行证据、个人 ownership 与 Mock/真实边界。
3. 用户本人不看逐字稿完成一次主线讲解，并回答两道未写进 Demo 的迁移题；每题必须同时指出不变量、受影响组件、至少一个失败模式和可执行验证方式才通过。只有两题、讲解、证据链和边界说明全部通过时 interview_ready=true，不能由 product_verified 自动推导。

**测试要求：** 校验文档链接与 revision，人工执行 Demo 检查表、两道迁移题和 Claim 边界审计；不要求录屏哈希或 168 小时延迟复测。

**依赖：** T24。

**主要风险：** AI 生成后直接朗读或把 Mock 登录包装成真实认证，都不能证明个人掌握度。

## 2. 当前停止点

用户已确认 AccessPilot v1.2 精简版 T18–T25 按顺序串行实施；T18–T20 已独立验收，当前停止在 T21 开始前，T21–T25 待开始。后续每张 Ticket 仍须测试先行、独立验收并本地提交；不推送、不合并、不部署。
