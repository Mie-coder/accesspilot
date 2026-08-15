# AccessPilot T14 最终验收交叉评审记录

**评审日期：** 2026-08-10
**评审类型：** `final-acceptance`
**范围：** T14 真实流式响应、Workspace 事件长流、取消与刷新恢复、SSE 部署边界
**权限：** 外部模型与独立 reviewer 只读，不得修改文件、提交、推送或部署

## 1. 评审包

### 目标

- 当前轮通过 `POST /api/chat/messages/stream` 按 SSE 增量返回，并以 `turn_id + seq` 关联事件。
- Workspace 通过 `GET /api/events` 回放后保持长连接，支持游标、心跳、断线重连与去重。
- 每轮只有一个终态；取消不持久化半截助手文本，刷新可恢复完成、中断或可重试错误。
- 浏览器只能收到白名单事件与通过安全校验的文本；Nginx 不缓冲两条 SSE 路由。

### 非目标

- T14 不负责政策 RAG、引用证据和供应商生成式回答；这些属于 T15。
- T14 的确定性业务回答按服务器真实多帧传输，不宣称是 DeepSeek token 流。
- 旧 JSON 对话接口继续兼容，但产品前端只使用新流式接口。

### 已确认决策与实现

- 当前轮流和 Workspace 事实流分离；`message.delta` 不持久化。
- `message.completed`、`error.recoverable`、`turn.interrupted` 通过 Workspace 行锁保证同一轮终态唯一。
- 同一 Workspace 的第二个活跃流式轮次返回稳定 409；5 分钟租约避免异常退出永久锁死。
- 每个 delta 在发送前做类型、累计长度和敏感信息校验；失败只返回安全的可重试错误。
- 前端增量解析支持任意字节切片、CRLF、多行 data、心跳与 AbortSignal；刷新从已持久化终态恢复。
- 取消时删除当前 UI 半截文本并显示安全中断提示；迟到更新不会让半截内容复活。
- `/api/events` 与 `/api/chat/messages/stream` 均配置 Nginx 禁缓冲、禁缓存和长读取超时。

### 验收证据

- 后端全量：253 passed；Ruff 通过；MyPy 38 个源文件通过。
- 前端全量：9 个测试文件、25 tests passed；TypeScript、ESLint、正式构建通过。
- 生产路径 SSE：`turn.started → intent.detected → tool.started/completed → draft.updated → tool.started/completed → business.status → 3 × message.delta → message.completed`；`terminal_count=1`。
- 真实浏览器：正式权限展示名称解析成功，草稿完整；刷新后用户消息、助手消息、草稿和活动均恢复；1440×900、1024×768、390×844 核心操作均可访问。
- Nginx 合同测试覆盖两条 SSE 路由；后端测试覆盖敏感/非字符串/超长 delta、终态竞争、活跃轮次、取消和旧接口兼容。

### 已知限制

- 结构化字段提取仍是同步 DeepSeek HTTP 调用，取消外层异步任务后，已进入线程的调用最多继续到 30 秒超时；它不能写完成终态，但已经验证的业务事实允许保留。异步可取消 provider 适配属于后续演进。
- 确定性回答采用服务器多片分段；真正的 provider token 流在 T15 生成式政策回答接入时实现。
- Demo 功能关闭时前端探测 `/api/demo/session` 会产生一条已处理的 404 网络日志，不影响用户界面或业务链路，作为 P2 集成清洁项保留。

### 聚焦评审问题

当前实现是否仍存在会违反 T14 已确认关键验收标准的 P0/P1？请按“问题 → 证据或原因 → 影响 → 最小改进建议”输出；把非阻塞限制标为 P2/P3，不要扩大到 T15–T17。

## 2. 评审模型状态

- Codex 专用 DeepSeek reviewer：此前返回 `401 Authentication Fails (governor)`，不构成有效评审。
- 项目 `.env` DeepSeek Key 直接只读评审：经用户明确授权发送脱敏验收摘要后成功完成；返回 `no_p0_p1`、`confidence=high`，没有新增 P0/P1。
- 独立实现验收 reviewer：首轮发现 1 个 P1；修复后定向复核确认关闭，最终无残留 P0/P1。

## 3. 评审议题与决策

### T14-CR-01 — P1 — 接受并关闭

**问题：** 刷新恢复原先会反向寻找任意历史 `error.recoverable`，即使后续重试已经成功，仍可能显示过期错误 banner 和错误消息。

**决策与修复：** 以事件顺序中的最后终态为准；后续 `message.completed` 或 `turn.interrupted` 会同时消解持久化 hydration 和实时订阅中的旧错误。新增“错误后重试成功”回归，前端全量增至 25 tests passed；独立 reviewer 定向复核确认 P1 关闭。

### T14-CR-02 — P2 — 接受为已知限制

同步结构化 DeepSeek 调用进入工作线程后不能被浏览器取消立即终止；取消仍会获得唯一 `turn.interrupted`，不会写伪完成或持久化半截回答，已经校验的业务事实按既定合同允许保留。

### T14-CR-03 — P2 — 接受为兼容边界

旧 JSON `/api/chat/messages` 不参与流式 active-turn 租约；产品前端只调用新 SSE 接口，旧接口仅作为兼容路径保留。

### T14-CR-04 — P2 — 回流 T15

T14 的确定性业务回答是服务器真实多帧 SSE，但不是 provider token 流；T15 接入生成式政策回答时必须由供应商流直接产生 token 增量。

### T14-CR-05 — P3 — 接受为清洁项

DeepSeek 指出 Demo 功能关闭时，前端探测 `/api/demo/session` 会留下已处理的 404 网络日志。它不影响界面、身份隔离或业务链路，不阻塞 T14；后续统一配置探测时清理。

### DeepSeek 结果核验

DeepSeek 的 3 条 P2 与独立 reviewer 已记录限制一致，另补 1 条日志清洁 P3。其输出末尾复述了评审包中“仍需用户授权”的旧状态；实际调用发生前用户已经明确授权，因此该句属于输入快照的过期描述，不影响其 `no_p0_p1` 结论。外部模型只审阅脱敏验收包，没有获得源码、密钥、私有地址或写权限。

## 4. 结论

本地独立验收修复后无残留 P0/P1；DeepSeek 脱敏验收包复核同样返回 `no_p0_p1`。后端 253 tests、前端 25 tests、静态检查、正式构建、生产 SSE 序列、刷新恢复和三档浏览器证据均通过。T14 结论为：**可进入本地提交，不代表推送、合并、部署或最终发布。**
