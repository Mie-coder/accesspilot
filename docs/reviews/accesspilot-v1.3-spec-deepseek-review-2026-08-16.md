# AccessPilot v1.3 LangGraph Spec — DeepSeek 独立评审

**评审模型：** DeepSeek-V4-Flash（只读评审角色）
**评审日期：** 2026-08-16
**评审对象：** `docs/specs/accesspilot-langgraph-agent-loop-v1.3.md`
**评审方式：** 独立、只读，并核对评审包引用的当前代码事实
**文件改动：** 无

## 独立评审结果

### [P1] 渐进切流的控制机制与 §9 配置合同冲突

**证据或原因：** §9 只允许全局 `legacy|langgraph`；§10.1 却要求 JSON 只读/RAG、JSON 申请、SSE、flow v2 interrupt 分阶段切换。单一二元开关无法表达这些中间态。

**影响：** 迁移步骤不能独立执行或验收，“JSON 先切、SSE 仍 Legacy”没有配置表达。

**最小改进建议：** 增加 endpoint + flow_version 的混合引擎选择矩阵，并在 AC-12 覆盖混合态和逐入口回切。

### [P1] 业务 interrupt 与 5 分钟 turn lease 的生命周期未定义

**证据或原因：** `append_turn_started` 会拒绝同 Workspace 中无终态且 lease 未过期的前一轮；§6.3 没有说明 interrupt 轮是否写 `message.completed`、是否释放 lease、resume 是否使用新 `turn_id`。

**影响：** 若 interrupt 轮不闭合，用户 5 分钟内确认会被拒绝；AC-04 可能不可达。

**最小改进建议：** 业务 interrupt 的 HTTP 轮写 `message.completed` 闭合；resume 使用同一 `agent_thread_id` 上的新 `turn_id`，并增加 5 分钟内确认用例。

### [P2] Resume 路径未显式重跑权威快照

**证据或原因：** 拓扑从 `await_requester_confirmation` 直接到 `apply_confirmation_cas`；LangGraph resume 不会重跑 START/hydrate。

**影响：** checkpoint 派生状态可能陈旧，输出与权威草稿漂移。

**最小改进建议：** resume 后强制经过 rehydrate 节点，并在 AC-04 断言基于重新读取的权威草稿输出。

### [P2] “interrupt 之前不做副作用”与拓扑字面矛盾

**证据或原因：** `persist_draft_cas` 在 interrupt 前已经写草稿。

**影响：** 实现可能误解允许的副作用边界。

**最小改进建议：** 改为“interrupt 节点本身不新增副作用；等待前只允许草稿 CAS 与事件持久化”，并列允许/禁止清单。

### [P2] event_key 生成与存量事件迁移未定义

**证据或原因：** 现有 `workspace_events` 无 event_key；随机 key 无法去重，直接非空唯一约束又无法兼容历史行。

**影响：** AC-05/AC-09 无法判定，迁移可能失败。

**最小改进建议：** 新事件使用确定性 key；历史行保持 null，并使用 `event_key IS NOT NULL` 的 partial unique index，或给出确定性回填。

### [P2] agent_thread_id 的生成与持久化未定义

**证据或原因：** §6.2 只说服务端生成的稳定不透明 ID，没有说明确定性派生或数据库列。

**影响：** App B 跨进程恢复缺少稳定定位依据。

**最小改进建议：** 明确 Workspace 表新增唯一 agent_thread_id 并回填，或定义稳定 HMAC 派生。

### [P2] Checkpoint schema 的迁移职责边界未写死

**证据或原因：** §6.1 说 setup 只在迁移阶段，AC-11 又要求 Alembic downgrade/no-drift；执行账号、顺序和范围不明确。

**影响：** downgrade 或回滚可能误伤 checkpoint schema。

**最小改进建议：** 官方 setup/migrate 管 checkpoint schema，Alembic 管业务表；紧急回滚不触碰 checkpoint DDL。

### [P2] draft_patch / safe_tool_result 的字段范围未定义

**证据或原因：** State 含两个可能携带原始值的字段，但 checkpoint 只允许安全派生状态。

**影响：** 可能重复持久化敏感业务字段或工具原始结果，AC-03 无法判断。

**最小改进建议：** 定义字段级 allowlist，并与 checkpoint 泄漏扫描对齐。

### [P2] interrupt 决策语法未映射到现有确定性确认逻辑

**证据或原因：** 未说明是否复用 `_explicit_confirmation_from_text`，也没有 confirm/revise/defer 判定样例。

**影响：** LangGraph 与 Legacy 的确认语义可能漂移。

**最小改进建议：** 复用现有判定函数，明确最小样例集并纳入 parity。

### [P3] 最近三轮轨迹与 SSE seq 的顺序键未统一

**证据或原因：** 轨迹按全局事件 ID；SSE seq 是 turn 内局部序号；event key 又未定义。

**影响：** 跨引擎后顺序解释可能不一致。

**最小改进建议：** 轨迹与 Last-Event-ID 始终使用全局递增事件 ID；seq 只用于单 turn transport。

### [P3] Legacy “完整版本周期”退役条件未定义

**证据或原因：** 只有保留时长，没有退出判据。

**影响：** 治理边界长期悬置。

**最小改进建议：** 明确 v1.3 不删除 Legacy；后续单独 Spec 决定退役。

### [P3] 依赖矩阵应先做兼容性 spike

**证据或原因：** 当前从 0.6.11 跨到 1.x，API 与迁移语义可能变化。

**影响：** 可能改变 Ticket 拆分，但不推翻架构目标。

**最小改进建议：** 第一张 Ticket 验证候选矩阵，再锁定版本。

## 总体结论

**修订后进入 Tickets。** 未发现需要推翻方向的 P0；checkpoint 非权威、应用级幂等、敏感扫描和不删除业务数据的回滚原则值得保留。两项 P1 和 event/thread/schema 等 P2 应先回流 Spec。

## 最值得保留的设计

1. 业务表唯一权威 + 恢复重新鉴权/重读 revision；
2. 白名单安全事件与真实工具边界；
3. 渐进切流、排空后回滚且不删除 checkpoint schema。

## 建议从 P0 延后

1. 最近三轮轨迹可缩成当前轮；
2. p50/p95 与 RAG Recall@4 可作为证据而非首版阻塞门槛；
3. 多视口矩阵可缩为桌面、移动各一个与键盘路径。

## 主控备注

本文件保留外部模型的独立观点；严重度与是否采纳由主控在决策记录中重新判定，不能把外部观点直接当作项目事实。
