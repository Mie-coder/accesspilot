# AccessPilot v1.3 LangGraph Spec — Claude Code Sonnet 5 独立评审

**评审模型：** Claude Code Sonnet 5
**评审日期：** 2026-08-16
**评审对象：** `docs/specs/accesspilot-langgraph-agent-loop-v1.3.md`
**评审方式：** 独立、只读；首次连接中断后，按同一评审包缩短请求重试成功
**文件改动：** 无

## 独立评审结果

### [P0] 二阶段 defer/revise 转场缺少崩溃安全覆盖，可能静默丢弃用户新消息

**证据或原因：** §6.3.3 规定“修改字段、换题或拒绝 → 先以 defer/revise 无副作用结束旧 interrupt，再按新消息启动正常图运行”，即一次用户输入被拆成两次独立的图调用（先 resume 关闭旧 interrupt，再 invoke 新一轮）。但 §7 列出的五个崩溃点均未覆盖“defer resume 已提交、新一轮 invoke 尚未开始”这一窗口。

**影响：** 若在两次调用之间进程崩溃或请求失败，旧 interrupt 已经关闭（不可逆），但用户真实意图从未进入图执行，且没有专门的幂等键或重试记录来发现这一状态。用户会得到看似正常但实际丢失请求的响应，直接影响唯一 P0 interrupt 的可信度。

**最小改进建议：** 在关闭旧 interrupt 与启动新一轮之间引入同一 turn 下的持久化转场记录，或改为单次 invoke 内部完成 close + reprocess，并把该窗口加入故障验证。

### [P0] Checkpointer schema 生命周期与 Alembic 的治理边界未定义

**证据或原因：** §6.1 要求 `setup()` 只在显式初始化/迁移阶段执行，暗示直接调用 `langgraph-checkpoint-postgres` 自带 setup/迁移；AC-11 又要求 Alembic upgrade/downgrade/no-drift。两者是否是同一套迁移权威、downgrade 时 checkpoint 表如何处置、CI 中谁先谁后执行均未说明。

**影响：** 两套互不感知的迁移系统共存于同一数据库，容易在 CI、回滚演练和 no-drift 判定中产生口径不一致，并直接影响回滚可执行性。

**最小改进建议：** 明确二选一：checkpointer 建表 SQL 纳入 Alembic，或 checkpoint schema 由官方库自身版本化且 Alembic no-drift 显式排除该 schema。

### [P1] Resume 消息无法归类时的兜底行为未定义

**证据或原因：** §6.3.3 要求服务端确定性识别下一条消息属于确认或修改/换题/拒绝，但没有说明无关话题、沉默重试、二次安全探测等无法明确归类的输入。

**影响：** 实现可能误判为确认或让 interrupt 无限悬挂，导致 Ticket 间语义不一致且无法形成确定性验收。

**最小改进建议：** 无法归类时统一按 defer 处理、保持草稿不变，并补自动化用例。

### [P1] Flow v2 待确认线程回退到 Legacy Cursor 的映射机制未具体化

**证据或原因：** §10.2 只说“按权威 draft/revision 恢复为旧 Cursor 确认流程”，没有给出 interrupt kind/revision 到 Cursor 字段的映射。

**影响：** AC-12 的回滚演练可能依赖人工临时判断，无法做到可重复、可判定。

**最小改进建议：** 增加最小映射表，或把迁移脚本作为独立 Ticket 并给出验收标准。

### [P1] 节点重命名与 schema_version 演进缺少具体机制

**证据或原因：** §6.2 提出 schema_version，§10.2 又禁止删除/重命名 pending interrupt 指向的节点，但没有说明如何发现存量 pending interrupt 或如何兼容。

**影响：** 后续拓扑演进会退化为临时决策，与 Spec 追求的可判定验收冲突。

**最小改进建议：** v1.3 先采用硬约束：只有检测到 pending interrupt 为零才允许改名；完整双版本迁移另行立项。

### [P1] 同一 agent_thread_id 的并发 invoke/resume 缺少显式保护

**证据或原因：** operation ID 和唯一约束只保证业务写入幂等，没有说明重复点击确认、断线重试等并行 resume 是否会并发更新同一 checkpoint。

**影响：** 即使业务写入不重复，checkpoint 的位置/分支状态仍可能漂移。

**最小改进建议：** 明确同一 thread 的 invoke/resume 应用层串行化，并增加独立验收用例。

### [P2] 五个崩溃点的故障注入手段未约定

**证据或原因：** §7/AC-05 列出崩溃点，但未约定如何准确触发。

**影响：** Ticket 可能各自使用不可比、不可复现的验证方式。

**最小改进建议：** 统一使用测试专用可控 fault hook；跨进程恢复另用真实 App A/App B 测试，不增加产品故障控制面。

### [P2] event_key 公式与 shadow 阶段通过阈值未定义

**证据或原因：** §8.2 只有唯一约束，没有具体构成；§10.1 的 shadow 也没有比较窗口、样本量或允许差异。

**影响：** AC-07/AC-09 的去重和切流通过边界不明确。

**最小改进建议：** Ticket 拆分前明确 event key 公式，并给 deterministic parity/shadow 设置显式验收阈值。

## 总体结论

**修订后进入 Tickets。**

架构方向合理且克制：状态权威清晰、interrupt 只有一个、应用级唯一约束不依赖框架 exactly-once。但两项 P0 和多项 P1 必须先回流 Spec，否则后续 Ticket 会分别即兴决定实现细节，削弱“验收可判定”和“回滚可执行”。

## 最值得保留的设计

1. checkpoint 仅保存执行位置与派生快照，不成为第二业务事实源；
2. 经理/数据负责人审批与 IAM 不改造成 LangGraph HITL 或模型工具；
3. operation ID + 数据库唯一约束的应用级幂等，并诚实披露外部调用 at-least-once。

## 建议从 P0 延后

1. SSE 最终切流可在 JSON + interrupt 稳定后再做；
2. 精确补丁版本可先作为候选矩阵，由兼容性 Ticket 验证后锁定；
3. 节点重命名的完整双版本迁移机制可延后，v1.3 先采用 pending=0 的硬门禁。

## 主控备注

本文件保留外部模型的独立观点；严重度与是否采纳由主控在决策记录中重新判定，不能把外部观点直接当作项目事实。
