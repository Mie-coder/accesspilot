"""FastAPI 应用入口"""

from fastapi import Depends, FastAPI, HTTPException, Request, Response

from accesspilot.config import Settings
from accesspilot.db.session import build_engine, build_session_factory
from accesspilot.db.workspace_store import SqlAlchemyWorkspaceStore
from accesspilot.domain.models import RequestDraft
from accesspilot.workspaces import (
    UnknownWorkspaceError,
    Workspace,
    WorkspaceService,
    WorkspaceStore,
)


def create_app(settings: Settings | None = None, store: WorkspaceStore | None = None) -> FastAPI:
    """创建一个可配置、可测试的 FastAPI 应用。"""
    active_settings = settings or Settings()
    if store is None:
        # 实际启动时默认用 PostgreSQL，所以 API 重启不会丢失 Workspace。
        # 测试需要纯内存存储时，会通过 store= 显式注入。
        store = SqlAlchemyWorkspaceStore(
            build_session_factory(build_engine(active_settings.database_url))
        )
    workspace_service = WorkspaceService(store)
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
            # 这里只返回送审资格，不创建审批记录，也不代表权限已经开通。
            "can_enter_approval": draft.can_enter_approval(),
        }

    @app.get("/api/drafts/current")
    def get_current_draft(
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> dict[str, object]:
        """读取当前浏览器 Workspace 中已保存的申请草稿。

        复用 require_workspace 让 Cookie 检查、过期 Token 处理和
        Workspace 查询保持一致，不从 URL 接收敏感 Token。
        """

        return {
            "draft": (
                workspace.draft.model_dump(mode="json")
                if workspace.draft is not None
                else None
            )
        }

    return app


app = create_app()
