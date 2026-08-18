# AccessPilot v1.3 LangGraph Spec 双模型评审议题与裁决

**日期：** 2026-08-16
**评审关口：** Spec 完成后、Tickets 拆分前
**Canonical Spec：** `docs/specs/accesspilot-langgraph-agent-loop-v1.3.md`
**评审包：** `docs/reviews/accesspilot-v1.3-langgraph-spec-review-package-2026-08-16.md`
**Claude 结果：** `docs/reviews/accesspilot-v1.3-spec-claude-review-2026-08-16.md`
**DeepSeek 结果：** `docs/reviews/accesspilot-v1.3-spec-deepseek-review-2026-08-16.md`

## 1. 评审可用性记录

- DeepSeek 独立只读评审成功完成，并核对了评审包引用的当前源码事实；
- Claude Code Sonnet 5 首次在核验源码时连接中断，未把不完整输出计为评审结果；使用同一评审包缩短为两份文档的只读审查后重试成功；
- 两个外部模型都没有修改文件、Git 状态或发布状态；
- 外部观点只作为待核实发现，以下严重度和取舍由主控根据 Spec、当前代码和工作流边界重新裁决。

## 2. 议题清单

| ID | 来源 | 主控严重度 | 影响范围 | 主控判断 | 处理 |
|---|---|---:|---|---|---|
| R-01 | Claude | P0 | §5–§7、AC-04/05 | 接受 | 删除“两次图调用”的 defer/revise 方案；改为一次 `Command(resume)` 内 update → rehydrate → route，增加中途崩溃不丢消息用例 |
| R-02 | Claude + DeepSeek | P1 | §6.1、§10、AC-11 | 接受 | 官方 setup 独占 checkpoint schema；Alembic 只管应用 schema并排除 checkpoint；明确账号、顺序和不 downgrade checkpoint |
| R-03 | DeepSeek | P1 | §9–§10、AC-12 | 接受并在 R-24 收紧 | 二元配置改为 legacy/mixed/langgraph；评审后审计进一步改为 Workspace sticky flow，不保留按 endpoint 分裂的运行态 |
| R-04 | DeepSeek | P1 | §6.3、events turn lease、AC-04 | 接受 | 业务 interrupt 的 HTTP 轮写正常 `message.completed`；resume 为同 thread 新 turn；5 分钟内可确认 |
| R-05 | Claude | P1 | §6.3、§7、AC-04 | 接受 | 同 Workspace/thread 统一走 active-turn lease；并发第二请求固定 409，不并发写 checkpoint |
| R-06 | DeepSeek | P1 | §5.1/5.2、§6.3、AC-04 | 接受并升为 P1 | resume 后新增 `rehydrate_resume_snapshot`，不能直接使用 checkpoint 派生草稿决定结果 |
| R-07 | DeepSeek | P2 | §5.2 | 接受 | 改写为“interrupt 节点自身无副作用”，明确上游允许的草稿 CAS/安全事件与禁止的模型/工具/正式业务写入 |
| R-08 | Claude + DeepSeek | P1 | §8、migration、AC-05/07/09 | 接受并升为 P1 | 定义 step/event/tool key 公式；历史行 event_key 为空并使用 partial unique index；跨 turn 排序始终用 DB event id |
| R-09 | DeepSeek | P1 | §6.2、migration、AC-04 | 接受并升为 P1 | Workspace 新增唯一非空 `agent_thread_id` 并回填；区分 thread、graph run、input seq 与 HTTP turn |
| R-10 | DeepSeek | P1 | §5.3、AC-03 | 接受并升为 P1 | 给 `draft_patch/safe_tool_result` 字段级白名单，并扫描真实序列化 checkpoint |
| R-11 | Claude + DeepSeek | P1 | §6.3、AC-02/04 | 接受 | 复用现有确认函数；只有明确 True 才确认，其余一律在同一 resume 内重新路由，绝不默认确认 |
| R-12 | Claude | P1 | §10.2、AC-12 | 接受 | 增加 flow v2 pending confirmation → Legacy Cursor 映射表；旧 checkpoint 保留但不再 resume |
| R-13 | Claude | P2 | §10.2 | 接受 | v1.3 用“pending=0 才可改名/重排”的硬门禁；完整 checkpoint 双版本迁移延后单独立项 |
| R-14 | Claude | P2 | §7、§12 | 接受 | 精确崩溃点统一用测试专用 fault hook；跨实例恢复用真实 App A/B，不增加产品故障控制面 |
| R-15 | Claude | P2 | §10.1、AC-02 | 接受 | deterministic parity 固定场景要求 100%；连续两次完整本地运行零差异才进入 canary |
| R-16 | DeepSeek | P3 | §8.3 | 接受 | 最近三轮/Last-Event-ID 用全局 DB event id；SSE seq 只用于单 turn，event key 只去重 |
| R-17 | DeepSeek | P3 | §10.2 | 接受 | v1.3 不删除 Legacy；未来退役必须独立 Spec 与后续 revision 门禁 |
| R-18 | Claude + DeepSeek | P2 | §6.1 | 部分接受 | 精确版本保留为候选矩阵；T26 先做兼容性 spike，成功后才锁定，不静默换版 |
| R-19 | Claude | P2 | v1.3 P0 范围 | 拒绝延后 SSE | 用户真实产品入口是 SSE，且轨迹要在沟通时可见；保留在 v1.3，但严格排在 JSON canary/interrupt 之后 |
| R-20 | DeepSeek | P3 | 轨迹 UI | 拒绝缩成当前轮 | 最近三轮原型已完成定向测试，数据量小且不增加后端状态权威；保留范围 |
| R-21 | DeepSeek | P3 | §12.3 | 接受澄清 | p50/p95、Recall@4 等只在验收后记录，不作为未定义目标值的 P0 门禁 |
| R-22 | DeepSeek | P3 | 浏览器验收 | 拒绝缩减 | v1.2 已有三视口流程，v1.3 只做相关回归；保留不会显著增加架构风险 |
| R-23 | 评审后独立一致性审计 | P1 | §6.2–§7、AC-04/05 | 接受 | 新增 turn execution/pending 窄表；checkpoint head 用 fence CAS 接纳；pending/Cursor/等待事件/terminal/lease 在同一应用事务提交；接管复用原 run/input/turn，只递增 attempt/fence |
| R-24 | 评审后独立一致性审计 | P1 | §9–§10、AC-12 | 接受 | 取消 flow v2 的 JSON/SSE 分裂引擎；Workspace 一次性绑定 flow，running/pending 绑定优先且冲突 409；JSON/SSE 只分步做隔离门禁，真实 canary 整体切换 |
| R-25 | 评审后独立一致性审计 | P1 | §10.2、AC-12 | 接受 | 发布/节点演进 preflight 同时枚举应用 pending/未完成 execution 与它们精确引用的 checkpoint tasks；双向不一致或不可读即阻断 |
| R-26 | 评审后独立一致性审计 | P1 | §7–§8、AC-05/07/08 | 接受 | 用逐事件 Pydantic allowlist 保留 Legacy terminal；step/event/tool/operation 身份改为版本化 canonical JSON tuple + SHA-256，写死 lifecycle 和 attempt→ordinal 映射 |
| R-27 | Ticket 一致性审计 | P1 | §6.2、§10.2、T33–T35 | 接受 | 存在 accepted head 时恢复必须精确续该 head，只有从未接纳 head 才能从 safe input 重建；回滚为已验证的旧 pending head 写不可变 retirement tombstone，tombstone 不再 resume/不计 live，未分类孤立 head 仍阻断 |
| R-28 | Ticket 一致性回归 | P1 | §6.3、AC-04、T33–T34 | 接受，后由 R-29 补全坐标 | 接受 resume 的原子事务必须先验证 active pending head，再把其完整 `checkpoint_thread_id + checkpoint_ns + accepted_checkpoint_id` 种入新 execution 并置 pending=resuming；增加“输入事务已提交、首次图调用前崩溃”的精确 head 恢复测试 |
| R-29 | T26 候选版本兼容性 Spike | P1 | §6.1–§6.3、T26/T28/T33/T34 | 接受 | LangGraph 1.2.11 根图会把业务传入的非空 `checkpoint_ns` 归一为空字符串，`get_state()` 又将非空值视为子图路径；改为每 graph run 独立 `checkpoint_thread_id`，并保存 saver 返回的完整 thread/namespace/checkpoint head，不再伪造 namespace |
| R-30 | T26 候选版本持久顺序 Spike | P1 | §6.3、§7、T26/T29/T33/T34 | 接受 | dynamic interrupt 是 `put(head)` 后再 `put_writes(__interrupt__)`；禁止每次 `put` 立即更新 accepted head，改为图停止后验证精确 interrupt/END，再与 pending/Cursor/terminal 在同一 fenced 应用事务提升 candidate；半写入 checkpoint 只是孤儿 |

## 3. 关键决策记录

### D-01 — 非确认输入采用单次 resume 内重新路由

**选择：** `Command(resume={decision, safe_user_text})` 进入原 interrupted node；节点返回 `Command(update=..., goto=rehydrate_resume_snapshot)`，随后根据决定确认或重新进入 Router。

**拒绝：** “先 resume 关闭旧 interrupt，再第二次 invoke 新输入”。

**理由：** 两次调用之间存在无法由原 Spec 捕获的崩溃窗口，会让旧 pending 已关闭而新消息未执行。单次图调用让新输入和下一节点进入同一持久执行链。

### D-02 — HTTP turn terminal 与 Graph pending 可以同时成立

**选择：** 首次命中业务 interrupt 时，Graph checkpoint 保持 pending；当前 HTTP turn 的应用 pending 投影、confirmation Cursor、`agent.input.required → business.status → message.completed`、execution terminal 和 lease 释放在同一事务提交。下一条真实用户输入使用新 `turn_id/input_seq`、同一 thread/run resume。

**理由：** HTTP/SSE 传输生命周期与长期图运行生命周期不是同一概念；前者必须闭合才能释放现有 Workspace lease，后者由 Postgres checkpoint 持久等待。

### D-03 — 两套数据库迁移权威严格分区

**选择：** 官方 PostgresSaver setup/migrate 管 checkpoint schema；Alembic 管 AccessPilot 应用 schema；运行账号无 DDL；no-drift 与 downgrade 不触碰 checkpoint schema。

**理由：** 复制第三方内部 DDL 到 Alembic 会制造升级漂移；让请求时 setup 又违反最小权限和可预测启动。

### D-04 — 幂等身份区分逻辑运行与 HTTP 请求

**选择：** thread 表示 Workspace 对话；graph run 跨 interrupt；input seq 表示同 run 的每条用户输入；HTTP turn 是首次接受该输入的 transport 身份；崩溃接管保持 run/input/turn 不变，只递增 attempt/fence。普通 operation ID 使用 run + input seq + step，确认写使用稳定 pending_input_id。

**理由：** 仅用 `turn_id` 无法在跨 HTTP resume 时稳定去重，完全只用 run 又会错误去重下一条合法输入。

### D-05 — 恢复使用应用 ledger + fenced checkpoint head

**选择：** `AgentTurnExecutionRecord` 管理逻辑输入、lease、attempt/fence 和 accepted checkpoint locator；`AgentPendingInputRecord` 保存可对账的业务等待投影。Saver 允许先落 checkpoint，但单次 `put` 只产生 candidate；图停止且精确状态验证通过后，只有当前 fence 能在 finalize 事务中提升 accepted head。所有恢复显式传完整 thread/namespace/checkpoint 坐标。

**理由：** 官方 checkpointer 与应用事务不做 2PC。允许 checkpoint-only 窗口，再由无 terminal 的 execution 幂等补全；反过来绝不允许 terminal-only 窗口。

### D-06 — 引擎绑定 Workspace，不绑定 endpoint

**选择：** flow 1/2 分别固定到 Legacy/LangGraph；同 Workspace 的 JSON/SSE 始终同引擎。`mixed` 表示不同 Workspace 的 server-side canary cohort，而不是一个 Workspace 的半切流。

**理由：** 按 endpoint 分引擎会让 JSON 创建的 pending graph run 在 SSE 后续输入中被 Legacy 修改，同时留下无 owner checkpoint。

### D-07 — 事件 Schema 逐类型定义

**选择：** 保留 v1.2 每个事件的 Pydantic Schema，新类型各自 `extra=forbid`；不使用一个全局字段列表覆盖 terminal/draft 合同。所有新身份都使用版本化 canonical tuple 哈希。

**理由：** 这同时保持 Legacy DTO 兼容性，并为重放去重测试提供唯一 oracle。

### D-08 — 评审没有授权实施

双评审选择只授权只读审查与 Spec 回流。生产依赖、数据库、API 和简历仍需 Ticket 列表获得用户确认后，才按一次一张、TDD、独立验证和本地提交执行。

### D-09 — 用 checkpoint thread 隔离 graph run，不伪造根图 namespace

**选择：** Workspace 的 `agent_thread_id` 仅作为应用并发和切流身份；每个 graph run 使用独立、服务端生成的 `checkpoint_thread_id` 传给 PostgresSaver，并连同 saver 返回的 namespace/checkpoint ID 作为 accepted head。

**拒绝：** 向根 `CompiledStateGraph` 传入 `checkpoint_ns=accesspilot:<version>:<run>` 来隔离运行。

**理由：** 候选 LangGraph 1.2.11 已实测将根图 namespace 写入归一为 `""`，同时把非空 namespace 解读为子图路径。独立 checkpoint thread 既符合官方语义，又保留精确 head 恢复和 graph run 隔离。

### D-10 — accepted head 只在图停止后提升

**选择：** Saver `put` 返回的 locator 只是调用内 candidate。invoke/stream 停在 interrupt 或 END 且全部 saver writes 完成后，调用层使用精确 locator 验证状态，再在当前 fence 的 finalize 事务提升 accepted head。

**拒绝：** 每次 `PostgresSaver.put()` 后立即 CAS `accepted_checkpoint_id`。

**理由：** dynamic interrupt 先写 head，后写 `__interrupt__`。过早提升会暴露半写入状态；崩溃后只从上一个已验 accepted head 或安全 input fact 重做，业务幂等交给 operation ledger。

## 4. 修订后结论

**结论：Spec 与 Tickets 已通过评审，用户已授权从 T26 串行实施。**

两份评审均建议“修订后进入 Tickets”，没有要求重新定义项目目标。主控已经把所有 P0/P1 接受项和会影响验收可判定性的 P2 回流 Canonical Spec；评审后的 R-23–R-28 关闭了 terminal/Cursor 原子性、崩溃所有权、resume head 种入、跨入口引擎、事件合同和回滚后 pending head 归类的次生矛盾；T26 实测后的 R-29/R-30 又用候选版本真实持久化语义修正了 root namespace 与 accepted-head 时机。当前没有需要用户额外取舍的实质分歧。

这不是 v1.3 实现完成或最终验收结论。T26–T42 已拆分并获得用户确认；当前只按张推进 T26，每张仍需独立验收和本地提交。
