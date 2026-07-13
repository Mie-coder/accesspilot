# Day 3 Workspace API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建带 HttpOnly Cookie 隔离的最小 FastAPI 演示后端，提供健康检查、Workspace 创建/重置和草稿预览。

**Architecture:** `main.py` 只处理 HTTP、Cookie 和状态码；`workspaces.py` 封装 Workspace、内存 Store 与 Service；`config.py` 集中保存应用和 Cookie 配置。每个 FastAPI 测试通过应用工厂注入新的内存 Store，避免测试之间共享数据。

**Tech Stack:** Python 3.11、FastAPI、Pydantic Settings、pytest、httpx/TestClient、Ruff、MyPy。

## Global Constraints

- 所有演示数据保持虚构，不能提交 API 密钥或 `.env`。
- Workspace Token 必须由 `secrets.token_urlsafe(32)` 生成，不能使用递增 ID。
- Cookie 名为 `accesspilot_workspace`，默认 `HttpOnly=True`、`SameSite=lax`、本地开发 `Secure=False`。
- 缺少 Workspace Cookie 返回 `401`；未知 Token 返回 `404`；无效 JSON 由 FastAPI 返回 `422`。
- 先运行并观察每个测试失败，再实现最小代码使其通过。

---

### Task 1: 应用工厂、配置与健康检查

**Files:**

- Create: `apps/api/src/accesspilot/config.py`
- Create: `apps/api/src/accesspilot/main.py`
- Create: `apps/api/tests/api/test_health.py`

**Interfaces:**

- Produces `Settings`，含 `app_name`、`workspace_cookie_name`、`workspace_cookie_secure`。
- Produces `create_app() -> FastAPI` 和模块级 `app`。
- Produces `GET /health -> {"status": "ok"}`。

- [ ] **Step 1: 写失败的健康检查测试**

```python
from fastapi.testclient import TestClient

from accesspilot.main import create_app


def test_health_returns_ok() -> None:
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

- [ ] **Step 2: 运行测试确认 RED**

Run: `python -m pytest apps/api/tests/api/test_health.py -v`

Expected: 因为 `accesspilot.main` 尚不存在而失败。

- [ ] **Step 3: 实现最小配置和应用工厂**

```python
# config.py
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "AccessPilot API"
    workspace_cookie_name: str = "accesspilot_workspace"
    workspace_cookie_secure: bool = False
    model_config = SettingsConfigDict(env_prefix="ACCESSPILOT_", extra="ignore")
```

```python
# main.py
from fastapi import FastAPI

from accesspilot.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    active_settings = settings or Settings()
    app = FastAPI(title=active_settings.app_name)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
```

- [ ] **Step 4: 运行测试确认 GREEN**

Run: `python -m pytest apps/api/tests/api/test_health.py -v`

Expected: `1 passed`。

- [ ] **Step 5: 提交**

```bash
git add apps/api/src/accesspilot/config.py apps/api/src/accesspilot/main.py apps/api/tests/api/test_health.py
git commit -m "feat: 添加 FastAPI 健康检查"
```

### Task 2: 内存 Workspace Store 与业务 Service

**Files:**

- Create: `apps/api/src/accesspilot/workspaces.py`
- Create: `apps/api/tests/test_workspaces.py`

**Interfaces:**

- Produces `Workspace(token: str, draft: RequestDraft | None, fault_mode: str | None)`。
- Produces `WorkspaceStore` Protocol，以及 `InMemoryWorkspaceStore.get(token: str) -> Workspace | None` 和 `save(workspace: Workspace) -> None`。
- Produces `WorkspaceService.create() -> Workspace`、`get(token: str) -> Workspace`、`save_draft(token: str, draft: RequestDraft) -> Workspace`、`reset(token: str) -> Workspace`。
- `get` 和 `reset` 在未知 Token 时抛出 `UnknownWorkspaceError`。

- [ ] **Step 1: 写失败的隔离与重置测试**

```python
from accesspilot.domain.models import RequestDraft
from accesspilot.workspaces import InMemoryWorkspaceStore, WorkspaceService


def test_reset_only_clears_the_target_workspace() -> None:
    service = WorkspaceService(InMemoryWorkspaceStore())
    first = service.create()
    second = service.create()
    first_draft = RequestDraft(system_name="InsightHub", entitlement_name="客户数据导出")
    second_draft = RequestDraft(system_name="OpsDesk", entitlement_name="运维日志查看")
    service.save_draft(first.token, first_draft)
    service.save_draft(second.token, second_draft)

    service.reset(first.token)

    assert service.get(first.token).draft is None
    assert service.get(second.token).draft == second_draft
```

- [ ] **Step 2: 运行测试确认 RED**

Run: `python -m pytest apps/api/tests/test_workspaces.py::test_reset_only_clears_the_target_workspace -v`

Expected: 因为 `accesspilot.workspaces` 尚不存在而失败。

- [ ] **Step 3: 实现最小 Workspace 业务层**

```python
from dataclasses import dataclass
from secrets import token_urlsafe
from typing import Protocol

from accesspilot.domain.models import RequestDraft


class UnknownWorkspaceError(Exception):
    pass


@dataclass
class Workspace:
    token: str
    draft: RequestDraft | None = None
    fault_mode: str | None = None


class WorkspaceStore(Protocol):
    def get(self, token: str) -> Workspace | None: ...

    def save(self, workspace: Workspace) -> None: ...


class InMemoryWorkspaceStore:
    def __init__(self) -> None:
        self._workspaces: dict[str, Workspace] = {}

    def get(self, token: str) -> Workspace | None:
        return self._workspaces.get(token)

    def save(self, workspace: Workspace) -> None:
        self._workspaces[workspace.token] = workspace


class WorkspaceService:
    def __init__(self, store: WorkspaceStore) -> None:
        self._store = store

    def create(self) -> Workspace:
        workspace = Workspace(token=token_urlsafe(32))
        self._store.save(workspace)
        return workspace

    def get(self, token: str) -> Workspace:
        workspace = self._store.get(token)
        if workspace is None:
            raise UnknownWorkspaceError(token)
        return workspace

    def save_draft(self, token: str, draft: RequestDraft) -> Workspace:
        workspace = self.get(token)
        workspace.draft = draft
        self._store.save(workspace)
        return workspace

    def reset(self, token: str) -> Workspace:
        workspace = self.get(token)
        workspace.draft = None
        workspace.fault_mode = None
        self._store.save(workspace)
        return workspace
```

- [ ] **Step 4: 运行测试确认 GREEN**

Run: `python -m pytest apps/api/tests/test_workspaces.py -v`

Expected: `1 passed`。

- [ ] **Step 5: 提交**

```bash
git add apps/api/src/accesspilot/workspaces.py apps/api/tests/test_workspaces.py
git commit -m "feat: 添加隔离的演示工作区"
```

### Task 3: Workspace Cookie API 与草稿预览

**Files:**

- Modify: `apps/api/src/accesspilot/main.py`
- Create: `apps/api/tests/api/test_workspaces.py`
- Create: `apps/api/tests/api/test_draft_preview.py`

**Interfaces:**

- `create_app(settings: Settings | None = None, store: WorkspaceStore | None = None) -> FastAPI`。
- `POST /api/workspaces` 返回 `201` 和 `{"status": "created"}`，设置 `accesspilot_workspace` Cookie。
- `POST /api/workspaces/reset` 返回 `200` 和 `{"status": "reset"}`。
- `POST /api/drafts/preview` 返回 `draft`、`missing_fields` 和 `is_complete`。

- [ ] **Step 1: 写失败的 Cookie 与预览 API 测试**

```python
from fastapi.testclient import TestClient

from accesspilot.main import create_app


def test_create_workspace_sets_http_only_cookie() -> None:
    client = TestClient(create_app())

    response = client.post("/api/workspaces")

    assert response.status_code == 201
    assert response.json() == {"status": "created"}
    assert "accesspilot_workspace=" in response.headers["set-cookie"]
    assert "httponly" in response.headers["set-cookie"].lower()


def test_preview_requires_a_workspace_cookie() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/api/drafts/preview",
        json={"system_name": "InsightHub", "entitlement_name": "客户数据导出"},
    )

    assert response.status_code == 401


def test_preview_returns_missing_fields_for_current_workspace() -> None:
    client = TestClient(create_app())
    client.post("/api/workspaces")

    response = client.post(
        "/api/drafts/preview",
        json={"system_name": "InsightHub", "entitlement_name": "客户数据导出"},
    )

    assert response.status_code == 200
    assert response.json()["missing_fields"] == [
        "project_code",
        "data_scope",
        "business_reason",
        "start_date",
        "duration_days",
    ]
    assert response.json()["is_complete"] is False


def test_preview_rejects_invalid_request_body() -> None:
    client = TestClient(create_app())
    client.post("/api/workspaces")

    response = client.post("/api/drafts/preview", json={"duration_days": "tomorrow"})

    assert response.status_code == 422
```

- [ ] **Step 2: 运行测试确认 RED**

Run: `python -m pytest apps/api/tests/api/test_workspaces.py apps/api/tests/api/test_draft_preview.py -v`

Expected: 因为 Workspace 路由尚不存在而失败，通常为 `404`。

- [ ] **Step 3: 以最小路由实现 Cookie、状态码与预览**

```python
from fastapi import FastAPI, HTTPException, Request, Response

from accesspilot.config import Settings
from accesspilot.domain.models import RequestDraft
from accesspilot.workspaces import InMemoryWorkspaceStore, Workspace, WorkspaceService, WorkspaceStore


def create_app(
    settings: Settings | None = None,
    store: WorkspaceStore | None = None,
) -> FastAPI:
    active_settings = settings or Settings()
    workspace_service = WorkspaceService(store or InMemoryWorkspaceStore())
    app = FastAPI(title=active_settings.app_name)

    def require_workspace(request: Request) -> Workspace:
        token = request.cookies.get(active_settings.workspace_cookie_name)
        if token is None:
            raise HTTPException(status_code=401, detail="Workspace cookie is required")
        return workspace_service.get(token)

    @app.post("/api/workspaces", status_code=201)
    def create_workspace(response: Response) -> dict[str, str]:
        workspace = workspace_service.create()
        response.set_cookie(
            key=active_settings.workspace_cookie_name,
            value=workspace.token,
            httponly=True,
            samesite="lax",
            secure=active_settings.workspace_cookie_secure,
        )
        return {"status": "created"}

    @app.post("/api/workspaces/reset")
    def reset_workspace(workspace: Workspace = Depends(require_workspace)) -> dict[str, str]:
        workspace_service.reset(workspace.token)
        return {"status": "reset"}

    @app.post("/api/drafts/preview")
    def preview_draft(
        draft: RequestDraft,
        workspace: Workspace = Depends(require_workspace),
    ) -> dict[str, object]:
        saved_draft = workspace_service.save_draft(workspace.token, draft).draft
        assert saved_draft is not None
        missing_fields = saved_draft.missing_fields()
        return {
            "draft": saved_draft.model_dump(mode="json"),
            "missing_fields": missing_fields,
            "is_complete": not missing_fields,
        }
```

Add `Depends` to the FastAPI import. Keep the existing health route and module-level `app`.

- [ ] **Step 4: 运行 API 测试确认 GREEN**

Run: `python -m pytest apps/api/tests/api -v`

Expected: `5 passed`（健康检查、创建 Cookie、缺失 Cookie、草稿预览、无效请求体）。

- [ ] **Step 5: 提交**

```bash
git add apps/api/src/accesspilot/main.py apps/api/tests/api
git commit -m "feat: 添加 Workspace Cookie API"
```

### Task 4: 非法 Token、重置隔离与完整验证

**Files:**

- Modify: `apps/api/tests/api/test_workspaces.py`
- Modify: `docs/superpowers/plans/2026-07-12-accesspilot-learning-schedule.md`

**Interfaces:**

- 未知 Workspace Token 请求重置返回 `404`。
- 重置当前 Workspace 保留 Cookie，并只删除该空间的草稿。

- [ ] **Step 1: 写失败的未知 Token 和重置隔离测试**

```python
def test_reset_with_unknown_workspace_token_returns_not_found() -> None:
    client = TestClient(create_app(), raise_server_exceptions=False)
    client.cookies.set("accesspilot_workspace", "does-not-exist")

    response = client.post("/api/workspaces/reset")

    assert response.status_code == 404


def test_reset_keeps_cookie_and_clears_current_workspace_draft() -> None:
    client = TestClient(create_app())
    client.post("/api/workspaces")
    token_before_reset = client.cookies.get("accesspilot_workspace")
    client.post(
        "/api/drafts/preview",
        json={"system_name": "InsightHub", "entitlement_name": "客户数据导出"},
    )

    response = client.post("/api/workspaces/reset")

    assert response.status_code == 200
    assert response.json() == {"status": "reset"}
    assert client.cookies.get("accesspilot_workspace") == token_before_reset
```

- [ ] **Step 2: 运行测试确认 RED**

Run: `python -m pytest apps/api/tests/api/test_workspaces.py -v`

Expected: 新的未知 Token 测试得到 `500` 而不是 `404`；重置测试通过或失败取决于已有实现。

- [ ] **Step 3: 只补足失败测试要求的最小实现**

在 `main.py` 导入 `UnknownWorkspaceError`，并用下面的实现替换 `require_workspace`：

```python
def require_workspace(request: Request) -> Workspace:
    token = request.cookies.get(active_settings.workspace_cookie_name)
    if token is None:
        raise HTTPException(status_code=401, detail="Workspace cookie is required")
    try:
        return workspace_service.get(token)
    except UnknownWorkspaceError as error:
        raise HTTPException(status_code=404, detail="Workspace not found") from error
```

`reset_workspace` 不调用 `set_cookie` 或 `delete_cookie`，因此浏览器保留现有 Cookie；`WorkspaceService.reset` 只清空该 Workspace 的 `draft` 和 `fault_mode`。

- [ ] **Step 4: 运行完整验证**

Run:

```bash
python -m pytest apps/api/tests -v
ruff check apps/api/src apps/api/tests
mypy apps/api/src
```

Expected: 全部 pytest 通过，Ruff 输出 `All checks passed!`，MyPy 输出 `Success: no issues found`。

- [ ] **Step 5: 更新学习进度并提交**

在学习计划中仅勾选已经有测试或运行证据的 Day 3 检查点，然后提交：

```bash
git add apps/api/tests/api/test_workspaces.py docs/superpowers/plans/2026-07-12-accesspilot-learning-schedule.md
git commit -m "test: 覆盖 Workspace 隔离边界"
```

## 计划自检

- 健康检查、配置、Workspace 创建与重置、Cookie 隔离、草稿预览、401/404/422 行为都有对应任务。
- Workspace Token 的生成、Cookie 属性、内存存储和 Day 4 替换边界均有明确约束。
- 所有生产实现前均有明确的失败测试与预期失败原因。
- 未包含 PostgreSQL、模型调用、LangGraph 或前端，符合 Day 3 范围。
