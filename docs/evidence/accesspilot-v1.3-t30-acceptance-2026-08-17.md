# AccessPilot v1.3 T30 验收证据

**Ticket：** T30 — 安全 GraphState、Runtime Context 与生产拓扑骨架  
**实现基线 revision：** `76676a7`  
**日期：** 2026-08-17  
**状态：** `Verified`；独立只读验收 AC1–AC3 全部 PASS，P0/P1/P2 均为 0，等待本 Ticket 本地提交

## 1. 已验证改变

- 新增严格、冻结、`extra=forbid` 的 Graph input/state/output 与嵌套 `DraftPatch`、`SafeToolResult`，显式约束 26 个 State 字段的类型、枚举、长度和数值范围。
- 服务、Principal、当前 turn/fence、Workspace token、Cookie、CSRF 与 API key 只通过 LangGraph Runtime Context 注入，不进入 State。
- 新增包含 Spec 22 个命名节点、全部固定边/条件边与 `rehydrate_resume_snapshot` 的真实 `CompiledStateGraph`；旧 ConversationService 没有被包装成单节点。
- 本 Ticket 节点均为可替换的无副作用 stub，不调用模型、RAG、工具、草稿/申请写入、事件、quota 或 interrupt，也未接入 JSON/SSE。

## 2. 持久化前安全边界

实测 LangGraph 1.2.11 的两个时机陷阱：

1. 仅配置 Pydantic `input_schema(extra=forbid)` 时，传给 `compiled.invoke()` 的额外键可能先写入首个 `__start__` checkpoint，再在后续 channel 被过滤；因此生产调用边界先显式 `model_validate`，无效输入在 saver 首次调用前失败。
2. 节点返回错误类型时，错误 update 可能先进入 `put_writes`，到下一节点构造 Pydantic state 才报错；因此所有生产节点通过统一 wrapper 在返回 LangGraph 前合并并验证完整 State。

测试证明：恶意 invoke 输入在 saver 首次调用前失败，目标 checkpoint thread 零行；恶意节点 update 在返回 LangGraph 前失败，先前合法输入产生的 checkpoint 可以保留，但非法 update 与 canary 不进入 `put_writes` 或 checkpoint bytes。

## 3. 真实 checkpoint 扫描

在 `LANGGRAPH_STRICT_MSGPACK=true` 与真实隔离 PostgresSaver 下：

- 扫描 `checkpoints` JSONB、`checkpoint_blobs`、`checkpoint_writes`、type 列、原始 BYTEA、实际 serde 解码值、`get_tuple(exact)` 和 pending writes；
- 植入 Runtime Context canary secrets，确认字段名、值和对象均不落盘；
- 禁止 token/cookie/csrf/api_key/authorization/password/prompt/sql/vector/provider/employee/role/confirmed、pickle 和未知类型；
- 合法非空 `DraftPatch`、`SafeToolResult` 能以严格模型 round-trip；恶意额外字段、自定义对象与错误类型零写入。

T30 同时修复 T29 facade 的 `with_allowlist`：白名单传播到底层实际 I/O saver，clone 继续共享 invocation/candidate holder，并保留 recording/fault decorator。T29 真实 PostgreSQL 故障注入回归已重新通过。

## 4. 验收结果

| 验收标准 | 结果 | 核心证据 |
|---|---|---|
| 严格 State 与 Runtime 隔离 | PASS | 26 字段显式集合、嵌套白名单、恶意字段/类型/长度矩阵、Runtime canary |
| 完整生产拓扑骨架 | PASS | 22 节点、固定与条件边快照、代表路径、无副作用 spy 全零 |
| strict-msgpack 真实 checkpoint 安全 | PASS | 三表 raw+decoded+pending 递归扫描、合法 round-trip；恶意输入整 thread 零行、恶意 update/canary 零落盘 |

验证数字：

- T30 单元：`57 passed`；
- T29/T30 相关：`69 passed`；
- Agent 全套：`134 passed, 1 skipped`；
- 完整 API：`559 passed, 3 skipped`；三项 skip 是 T26/T29/T30 专用 PG 环境门控，其中 T29/T30 已分别真实运行通过；
- T29 与 T30 真实隔离 PostgreSQL：各 `1 passed`；临时数据库已全部清理；
- Ruff、MyPy（49 个 source files）与 `git diff --check`：通过。

## 5. 简历与面试价值

候选表述：

> 为 AccessPilot 设计 26 字段严格 GraphState、Runtime Context 与 22 节点生产 LangGraph 拓扑，在 invoke 前和节点 update 返回前建立双重校验，避免 LangGraph 将非法输入/更新先写入 checkpoint；通过真实 PostgresSaver 对 JSONB、BYTEA、pending writes 与 decoded state 递归扫描，验证服务对象、身份与敏感 canary 零落盘，并保持完整 API 559 项回归通过。

可展开讲解：State 与业务权威数据库如何分工；为什么 Runtime Context 不进入 checkpoint；为什么只配置 Pydantic schema 仍可能晚校验；为什么既扫描反序列化结果，也扫描数据库原始 bytes。

## 6. 限制

- T30 只完成类型、拓扑和无副作用骨架；T31 才实现确定性路由、只读工具和政策 RAG，T32 才实现解析与草稿 CAS。
- `await_requester_confirmation` 本 Ticket 不调用 `interrupt()`；T34 才实现持久 interrupt/resume。
- 生产 JSON/SSE 仍默认 Legacy；T38/T40 才做隔离入口与 canary 切流。
- 本文件只提供简历候选内容；正式简历仍需用户确认后才能修改。
