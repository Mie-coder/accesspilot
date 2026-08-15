# ADR-0004：纯数字输入必须由上下文游标解释

- 状态：已接受
- 日期：2026-08-12
- 范围：T18、T19；AC-01–AC-03、AC-14

## 背景与问题

`111` 本身没有业务类型：它可能是期限、编号的一部分，也可能只是用户误输入。v1.1 曾把没有上下文的纯数字路由成 `help`，而把所有数字直接当作期限又会把用户意图猜错。这个边界必须由服务端可审计的业务上下文决定，不能交给前端状态或模型猜测。

## 决策与不变量

1. 每个活动 `ConversationCursor` 由服务端保存，绑定 `workspace_id`、`actor_id`、`auth_session_id`、`draft_revision`、`expected_field` 和最近追问类型；模型和客户端不能创建或改写它。
2. 纯数字在模型配额和结构化解析之前处理：只有活动 Cursor 的 `expected_field=duration_days` 才将数字作为期限候选；无 Cursor 或期待其他字段时返回 `unknown/needs_clarification`，不调用模型、不调用工具、不写入业务草稿。
3. 期限还必须满足规范正整数和目录最大期限。成功消费通过 revision + expected field + Session 的 CAS 一次完成；Cursor 消费后不可复用。
4. 追问只有在成功终态持久化后才激活下一 Cursor；明确换题、提交、帮助、Session/actor/revision 不匹配都会使旧 Cursor 失效。

## 被拒绝的替代方案

| 方案 | 拒绝理由 |
|---|---|
| 所有全数字输入一律视为期限 | 没有上下文时会把权限编号或其他短句误写进草稿，无法解释“为什么是期限”。 |
| 让 LLM 判断数字含义 | 非确定、会消耗模型配额；模型无法成为 Cursor 的并发/Session 所有者，也不能保证零副作用。 |
| 只在 React 内保存当前问题 | 刷新、换账号或多窗口后状态可被伪造/丢失；无法做服务端 CAS 和审计。 |

## 结果、代价与限制

- 结果：`111` 的含义可由“上一轮明确追问的字段”解释，输入合同、草稿 revision 和 SSE/JSON 终态一致。
- 代价：Workspace 需要保存 Cursor 和 revision，写路径需要 CAS；失效场景必须向用户返回可恢复的澄清提示。
- 限制：这不是通用短句理解，也不做代词绑定或历史摘要；只覆盖受支持的字段和目录上限。Cursor 依附 v1.2 的 Mock Login/AuthSession，不代表生产身份系统。

## 失败、恢复与回滚

- 无 Cursor、期待非期限字段、格式非法或超过目录上限：保留原草稿、Cursor 与 revision，只返回澄清/校验错误。
- 竞争写入或 Session/actor/revision 过期：CAS 拒绝并返回 `CURSOR_STALE`/冲突，不覆盖更新较新的草稿；用户重新说明业务意图即可恢复。
- 流式回答在终态前中断：不激活下一 Cursor；重试从持久化 revision 重新开始。
- 若后续取消 T18，可关闭数字快捷路径并保留 `ConversationCursor` 历史字段；不得把旧客户端输入重新解释成跨身份写入。

## 可核验链接

| 证据 | 链接与定位 |
|---|---|
| Cursor 合同与消费语义 | [`ConversationCursor`](../../apps/api/src/accesspilot/domain/models.py#L89-L120)；[`Workspace.active_cursor`](../../apps/api/src/accesspilot/workspaces.py#L31-L75) |
| 激活/消费 CAS | [`WorkspaceService.activate_cursor`](../../apps/api/src/accesspilot/workspaces.py#L376-L422)；[`consume_cursor_cas`](../../apps/api/src/accesspilot/workspaces.py#L153-L188) |
| 数字路由与上限校验 | [`_numeric_follow_up`](../../apps/api/src/accesspilot/conversation.py#L525-L680)；数字入口在 [`handle_chat_message`](../../apps/api/src/accesspilot/conversation.py#L810-L839) |
| 红测与并发/失效矩阵 | [`test_t18_cursor.py`](../../apps/api/tests/conversation/test_t18_cursor.py#L190-L345)、[`test_t18_cursor.py`](../../apps/api/tests/conversation/test_t18_cursor.py#L511-L708) |
| 固定评测映射 | [`evaluation.py`](../../apps/api/src/accesspilot/evaluation.py#L220-L235) |
| 运行与边界 | [`v1.2 Product Manifest`](../evidence/accesspilot-v1.2-product-manifest.md#边界)；[`T18 Ticket 验收记录`](../tickets/accesspilot-productized-agent-v1.2.md#t18--conversationcursor-与纯数字上下文续答) |
