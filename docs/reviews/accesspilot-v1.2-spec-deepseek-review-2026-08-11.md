# AccessPilot v1.2 Spec DeepSeek 交叉评审记录（历史，已被精简范围取代）

**评审日期：** 2026-08-11
**关口：** `to-spec` 完成后、`to-tickets` 前；评审回流已获用户确认
**评审类型：** `spec`
**用户决策：** D01–D05 已确认；用户选择 DeepSeek；2026-08-11 接受评审回流并授权起草 Ticket
**权限：** DeepSeek 只读，无文件、Ticket、代码、提交、推送或部署权限
**评审输入：** 2026-08-11 交叉评审前的产品说明书与 Spec
**当时回流产物：** 当时使用与当前相同的 product/spec 路径；原扩张内容随后已被精简版覆盖，当前路径内容不是本次 DeepSeek 实际评审输入
**后续范围变更：** 用户于 2026-08-11 将 v1.2 收窄为“上下文意图 + 独立登录 + 可信多角色闭环”；本记录只作为精简前草案的历史评审，不代表当前精简 Spec 已重新完成外部评审

## 1. 评审包

### 1.1 目标

把 AccessPilot 从“流程可演示”的权限申请 Agent 升级为“决策可核验、生命周期可闭环、能力可证明”的访问决策与履约原型。软件通过 AC-01～AC-20 形成 `product_verified`；候选人通过独立 Evidence Pack 的 IR-01～IR-06 形成 `interview_ready`，两者互不推导。

### 1.2 已确认约束

- D01：产品主语是访问决策与履约工作台；
- D02：v1.2 只增加限时授权到期与回收；
- D03：风险审查在提交后由内部系统生成一次可对账的只读结果；
- D04：首次开通仍由权限管理员显式执行；
- D05：未提交草稿不跨登录恢复；
- ConversationCursor、真实 async Answer Provider、业务 SSE 与生产 Worker 属于 P1。

### 1.3 非目标

不增加真实 OIDC/SAML、企业目录、真实 IAM/IGA、通用多租户运营、生产调度器/SLA、盘点/续期/委托审批或面试评分 UI。

### 1.4 可用证据与限制

- v1.1 基线为本地提交 `c683d84`；作品集证据记录 10 个固定场景、30 个案例全部通过，后端 339 passed、前端 48 tests，以及三档浏览器检查，见 `docs/evidence/accesspilot-v1.1-portfolio-evidence.md`。
- v1.1 评测适配器是 `deterministic_offline`，不证明真实 DeepSeek token 质量或生产能力。
- v1.2 当前只有文档，没有实现、测试或运行证据；评审不得把目标值当成实际结果。

### 1.5 聚焦问题

在 D01–D05 不变、且不增加非目标的前提下，两份文档是否仍有会导致 P0 无法二元验收、权限/状态/副作用语义不闭合、难以合理拆 Ticket，或仍显得只是复杂模拟器的矛盾与隐含假设？

## 2. DeepSeek 状态与独立结论

首个只读 reviewer 会话只收到角色约束，未收到评审包，因此记录为“交接失败、无评审结论”。主控没有伪造或补写其结果。

第二个 DeepSeek 只读 reviewer 会话成功读取评审包、v1.2 文档、v1.1 证据与相关现有代码，全程未修改文件。它返回：无 P0；2 条 P1、3 条 P2，以及 6 个 P3 清晰度建议。外部结论为“修订后可拆 Ticket”，没有要求改变 D01–D05，也没有建议扩大到已确认的非目标。

## 3. 评审议题与主控决策

### CR-01 — P1 — 接受

**来源：** DeepSeek。

**问题与证据：** 评审输入把 `ACCESSPILOT_DEMO_AUTH_ENABLED` 设为默认关闭，同时 AC-01/02 又把 Mock SSO 作为唯一登录入口；Evidence Lab 也被混入同一个关闭语义。默认配置与验收配置不可同时判定。

**影响：** 同一份验收可能既要求登录成功、又要求登录端点 404。

**决策：** Spec §5 分离 Demo Auth 与 Evidence Lab 开关，新增 `p0_verification/evidence_lab_verification/closed_surface` 三个记录化档案；AC-01～19 与 AC-20 使用不同明确档案。

### CR-02 — P1 — 接受

**来源：** DeepSeek。

**问题与证据：** AC-16 要求第二 Tenant 使用真实 AuthSession 发起攻击，但评审输入没有定义测试 Tenant Employee 如何得到 Session，又禁止直接构造 Principal。

**影响：** 实现者只能增加测试登录后门或违反认证合同。

**决策：** Spec §6.1 规定测试夹具创建隔离 Tenant/Employee，并直接调用同一 AuthSession 创建服务获得 Cookie/CSRF；攻击请求必须经过完整 HTTP/ASGI 中间件，不增加公开测试登录端点。

### CR-03 — P2 — 接受

**来源：** DeepSeek。

**问题与证据：** 风险审查声称有 Tenant 预算和预算耗尽分支，但没有默认上限、桶语义、数据约束或 AC。

**影响：** 预算不足分支无法稳定实现和验收。

**决策：** Spec §15 定义 UTC 日桶、默认 `ACCESSPILOT_RISK_TENANT_DAILY_ATTEMPT_LIMIT=100`、原子消费与 `budget_exhausted` 结算；§17 增加预算桶实体，AC-08/15 覆盖耗尽与不伪造调用。

### CR-04 — P1 — 接受并由主控升级严重度

**来源：** DeepSeek 外部标记 P2；主控核验后升为 P1。

**问题与证据：** 现有 `apps/api/src/accesspilot/db/models.py` 中 `access_requests.workspace_id` 为 NOT NULL/CASCADE，Approval、Grant、Provisioning、Audit 等通过 `(workspace_id, request_id)` 复合外键连接。评审输入却只写“新增 nullable 字段”，没有定义旧外键与级联的切换顺序。

**影响：** 若按原文拆 Ticket，Workspace 清理可能继续级联删除 Tenant Case，直接违反 P0 数据完整性不变量。

**决策：** Spec §18 显式定义 Tenant 键扩展、双写/回填、新复合外键验证、逐表切换、Workspace CASCADE 移除和回滚证据；AC-18 对该切换二元验收。

### CR-05 — P2 — 接受

**来源：** DeepSeek。

**问题与证据：** 主演示要求读取预先持久化的到期 Case，但没有定义它怎样生成和证明 IAM 执行事实。

**影响：** 可以直接改库伪造到期/回收状态，强化“模拟器感”。

**决策：** Spec §12.3 与产品说明书 §8.6 要求场景工厂走正式领域服务，保存 Provider receipt、测试时钟、revision 和审计链；直接改库时 AC-13/14 为 FAIL。

### CR-06 — P3 — 接受

**来源：** DeepSeek。

**问题与证据：** AC-15 的“unknown usage 不为 0”可能被误读为必须虚构一个非零数值。

**决策：** 改为 `usage_status=unknown`，明确不是数值 0。

### CR-07 — P2 — 接受

**来源：** DeepSeek。

**问题与证据：** Spec 只提到 draft revision 确认，没有把 v1.1 已验证的确认失效机制规格化。

**影响：** Ticket 可能遗漏并发旧确认与半提交守卫。

**决策：** Spec §7 定义单调递增 `draft_revision`、`confirmed_revision`、业务字段变化失效和旧 revision 409；AC-09 覆盖。

### CR-08 — P3 — 接受

**来源：** DeepSeek。

**问题与证据：** “新 Case 关联旧 Case”没有稳定字段。

**决策：** 定义同 Tenant 可空 `replaces_case_id`，禁止自指和成环；不引入 Packet 多版本。

### CR-09 — P3 — 接受

**来源：** DeepSeek。

**问题与证据：** “保留身份 Header”没有名称清单。

**决策：** Spec §5.3 固定九个大小写不敏感的拒绝名称；不泛化拒绝所有自定义 Header。

### CR-10 — P2 — 接受

**来源：** DeepSeek。

**问题与证据：** 风险执行器写“提交后立即领取”，但 P1 才有生产 Worker，P0 的进程模型不清楚。

**影响：** 实现可能依赖不持久化后台任务，或把生产队列悄悄带入 P0。

**决策：** Spec §9 定义 best-effort 进程内 `run_once`、持久化 operation、授权恢复入口与 CAS；无生产 Worker、持续调度或时延承诺。

### CR-11 — P2 — 接受

**来源：** DeepSeek。

**问题与证据：** v1.1 的 30 个 Case 依赖旧 Demo/Workspace 语义，评审输入未说明在 v1.2 中保留、迁移还是替换。

**影响：** AC-19 可能重复计数或让旧角色切换继续冒充新 Session 验收。

**决策：** Spec §21 要求逐项标记 `retained|migrated|replaced`，禁止旧项与替代项重复计入分母；产品说明书同步该证据边界。

### CR-12 — P1 — 接受

**来源：** 主控回流后的独立一致性复核。

**问题与证据：** 修订稿一度要求“确认 Spec 和后续 Ticket 列表前不得创建 Ticket”，同时又要求确认 Spec 后才进入 Ticket 拆分，形成尚未生成的列表必须先被确认的循环条件。

**决策：** Spec §0 改为两道顺序门禁：用户确认交叉评审修订稿后，只授权起草 Ticket；用户再次确认 Ticket 列表后，才授权实现。

### CR-13 — P1 — 接受

**来源：** 主控回流后的独立一致性复核。

**问题与证据：** 修订稿在旧 Workspace CASCADE 仍存在的 pre-cutover Verify 阶段安排 Workspace 删除测试，但真正移除级联外键发生在后续 Cutover，测试必然失败。

**决策：** Spec §18 拆成 pre-cutover 与 post-cutover 两次验证：前者只验证双键和数据一致性；Cutover 移除级联后，后者才使用可丢弃 Workspace fixture 验证 Case 不丢失。AC-18 同步顺序。

### CR-14 — P1 — 接受

**来源：** 主控回流后的独立一致性复核。

**问题与证据：** “出站前事务记账”与“没有出站不消费”无法跨数据库和外部 Provider 原子实现；事务提交后、网络调用前崩溃时无法证明请求是否已发送。

**决策：** Spec §15 明确选择保守预留语义：可能出站前先提交 reservation 并消费预算，提交后不退款；崩溃窗口记 dispatch/usage unknown，不冒充已调用；陈旧 attempt 不原地重发。AC-08/15 增加该窗口的二元验收。产品说明书同步说明这项取舍。

## 4. 分歧与范围判断

- 没有需要用户在 D01–D05 之间重新取舍的实质分歧。
- 主控未接受任何扩大到真实 SSO/IAM、生产 Worker、盘点或续期的建议。
- 主控仅把迁移外键问题从 P2 升为 P1，因为本地模型证明确有 Workspace CASCADE 与复合外键，且它直接影响 Case 数据完整性。
- DeepSeek 回流后的独立一致性复核另发现并闭合 3 条 P1；它们均为顺序或原子性合同修正，不改变 D01–D05。

## 5. 结论

- 交叉评审修订后，没有未处理 P0/P1。
- 用户已接受评审回流；当时的结论是：**已进入 `to-tickets`，只允许起草和修订 Ticket 列表**。该阶段状态已被后续串行实施决定取代。
- 本历史评审不设置 `product_verified` 或 `interview_ready`；当前 T18 的实现证据与后续状态以产品书、Spec 和 Ticket 为准。
- 该结论随后被用户的精简范围决定更新；当前有效 Ticket 为 T18–T25，见第 6 节。

## 6. 精简范围后的适用性

- 当前 Ticket 已重写为 T18–T25，原 T18–T35 扩张版草案不再生效；
- Session、Principal、资源 ACL、严格输入和证据诚实性意见继续适用；
- 到期回收、复杂预算/Attempt、Evidence Lab、完整外键 cutover 和 168 小时门禁已延期，相关意见不再是 v1.2 P0；
- 当前精简 Spec 尚未重新进行 DeepSeek 或其他外部模型评审；
- 本条记录的是评审阶段的历史门禁；用户随后于 2026-08-11 确认按 T18 → T25 串行实施。当前有效状态见产品书、Spec 与 Ticket：T18 已完成并独立验收，T19–T25 尚未开始。
