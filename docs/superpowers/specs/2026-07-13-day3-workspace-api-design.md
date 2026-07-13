# Day 3：FastAPI 与 Demo Workspace 设计

## 目标

建立一个最小可运行的 FastAPI 后端，为公开演示提供健康检查、Workspace 创建、当前 Workspace 查询、Workspace 重置和申请草稿预览接口。

## 领域边界

Workspace 是一个按随机 Token 隔离的演示空间，不等同于正式员工账号。每个 Workspace 独立保存演示角色、故障模式、申请和审批数据。重置操作只能清空当前 Token 对应的 Workspace，不能影响其他 Workspace。

## 分层设计

```text
FastAPI Router
    → WorkspaceService
    → WorkspaceStore（Day 3 内存实现）
```

- Router 负责 HTTP 请求、Cookie 读取和响应状态码。
- Service 负责 Token 校验、创建和重置等业务规则。
- Store 负责保存和读取 Workspace；Day 3 使用内存字典，Day 4 替换为 PostgreSQL。

## 接口设计

### `GET /health`

返回 `{"status": "ok"}`，用于本地和部署健康检查。

### `POST /api/workspaces`

创建一个 Workspace，生成不可预测的 Token，并通过 `HttpOnly` Cookie `accesspilot_workspace` 返回给浏览器。

### `POST /api/workspaces/reset`

读取当前请求的 Workspace Cookie，只清空当前空间的申请、审批和故障设置，保留 Token，使浏览器可以继续使用原空间。

### `POST /api/drafts/preview`

读取当前 Workspace 中的申请草稿，返回草稿内容、按固定顺序排列的 `missing_fields` 和 `is_complete`。不调用模型，也不提交申请。

## 错误处理

- 没有 Workspace Cookie 时，创建接口可以创建空间；其他需要空间的接口返回 `401`。
- Cookie 中的 Token 不存在时返回 `404`。
- 请求体不符合 Pydantic 模型时由 FastAPI 返回 `422`。
- 重置不存在的 Workspace 不得创建新空间或影响其他空间。

## 安全与测试不变量

- Token 使用安全随机数生成，不使用递增 ID。
- Workspace 数据不能通过另一个 Token 读取或重置。
- 重置只能影响当前 Workspace。
- Day 3 的内存数据会在 API 重启后丢失；持久化由 Day 4 负责。
