"""FastAPI 应用入口"""

from fastapi import Depends, FastAPI, HTTPException, Request, Response

from accesspilot.config import Settings
from accesspilot.domain.models import RequestDraft
from accesspilot.workspaces import (
    InMemoryWorkspaceStore,
    UnknownWorkspaceError,
    Workspace,
    WorkspaceService,
    WorkspaceStore,
)


def create_app(settings: Settings | None = None, store: WorkspaceStore | None = None) -> FastAPI:
    """创建一个可配置、可测试的 FastAPI 应用。"""
    active_settings = settings or Settings()
    # 创建workspace服务
    workspace_service = WorkspaceService(store or InMemoryWorkspaceStore())
    app = FastAPI(title=active_settings.app_name)

    def require_workspace(request: Request) -> Workspace:
        """从cookie中读取Token， 并取得当前Workspace"""
        token = request.cookies.get(active_settings.workspace_cookie_name)
        if token is None:
            # 浏览器从未创建演示空间，后端无法判断草稿归属哪个 Workspace。
            raise HTTPException(status_code=401, detail="Workspace cookie 是必须的")
        try:
            # 只有服务端 Store 中仍保存该 Token，才允许继续操作该 Workspace。
            return workspace_service.get(token)
        except UnknownWorkspaceError as error:
            # Cookie 存在但资源已失效（例如 API 重启后内存清空），
            # 将领域异常转换为浏览器可理解的 HTTP 404 响应。
            raise HTTPException(
                status_code=404,
                detail="Workspace not found",
            ) from error

    @app.get("/health")
    def health() -> dict[str, str]:
        """返回最小存活状态，不访问外部依赖"""
        return {"status": "ok"}

    @app.post("/api/workspaces", status_code=201)
    def create_workspace(response: Response) -> dict[str, str]:
        """创建当前浏览器的独立演示空间。"""

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
    def reset_workspace(
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> dict[str, str]:
        """只重置当前 Workspace 的可变数据。"""

        workspace_service.reset(workspace.token)
        return {"status": "reset"}

    @app.post("/api/drafts/preview")
    def preview_draft(
        draft: RequestDraft,
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> dict[str, object]:
        """保存草稿并返回仍需补充的字段。"""

        workspace_service.save_draft(workspace.token, draft)
        missing_fields = draft.missing_fields()
        return {
            "draft": draft.model_dump(mode="json"),
            "missing_fields": missing_fields,
            "is_complete": not missing_fields,
        }

    return app


app = create_app()
