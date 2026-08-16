# AccessPilot v1.3 T27 验收证据

**Ticket：** T27 — ConversationOrchestrator 兼容层与 Legacy 黄金基线  
**基线 revision：** `90667e4`  
**日期：** 2026-08-17  
**状态：** `Verified`；独立只读验收无 P0/P1，等待本 Ticket 本地提交

## 1. 已验证改变

- 新增可注入 `ConversationOrchestrator`，JSON 入口只调用 `handle`，SSE 入口只调用 `prepare`。
- `LegacyConversationOrchestrator` 薄包装现有实现；默认配置仍完全走 Legacy。
- `ConversationRunResult.finalize_success()` 持有引擎自己的成功收尾，SSE 保持 `message.completed` 持久化 → Cursor 收尾 → 终态发送的既有顺序；接口层不再导入或解释 Legacy Cursor 规则。
- 未创建 checkpoint、execution/pending、flow 事实、新迁移或新图事件。

## 2. 验收结果

| 验收标准 | 结果 | 证据 |
|---|---|---|
| JSON/SSE 只依赖可替换编排边界 | PASS | 注入、falsey 合法实现、参数与依赖透明转发及接口层旁路反证测试 |
| Legacy outcome、Cursor、SSE、异常与唯一终态一致 | PASS | 全意图黄金样本；确认正/负/无三态 Cursor；成功、错误与中断顺序 |
| 默认纯 Legacy且无 T28+ 事实 | PASS | 生产 diff 与 fail-fast 图/checkpointer 反证测试 |

验证结果：

- T27 定向：`27 passed`；
- 会话、Cursor、政策、SSE、事件与工具相关回归：`151 passed`；
- 完整 API：`467 passed, 1 skipped`，跳过项仅为需要专用数据库变量的 T26 隔离 probe；
- Ruff、MyPy、Alembic drift 与 `git diff --check`：通过；
- 独立只读验收：P0/P1 均为 0。

## 3. 评审闭环

初次独立验收发现 SSE 入口仍直接执行 Legacy Cursor 状态转换，违反可替换边界；同时确认三态只冻结了 outcome，没有冻结 Cursor。修复后，Cursor 收尾移动到 engine-owned result finalizer，并补齐三态、异常 identity、falsey 注入和旁路反证测试，复验通过。

## 4. 简历与面试价值

可讲解：

- 用 Strangler 模式先建立可回滚边界，再逐步替换生产编排内核；
- 用依赖注入和黄金契约测试防止 JSON/SSE、Cursor 与错误语义漂移；
- 独立验收如何发现“表面已注入、业务规则仍泄漏在接口层”的假解耦，并完成闭环修复。

候选表述：

> 为 AccessPilot 的 LangGraph 渐进迁移建立 `ConversationOrchestrator` Strangler 边界，将 JSON/SSE 入口与 Legacy 编排解耦，并以全意图 outcome、Cursor 三态、流式终态和异常黄金测试锁定兼容合同；独立验收发现并修复接口层 Cursor 业务规则泄漏，相关 151 项与完整 API 467 项回归通过。

## 5. 限制

- 当前默认与真实产品入口仍 100% 使用 Legacy；生产 LangGraph 主链尚未接入。
- T27 不提供 PostgreSQL checkpoint、interrupt/resume、崩溃恢复或真实轨迹。
- 本文件只产生简历候选内容；正式简历仍需用户确认后才能修改。
