"""FastAPI 应用入口"""

from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.embeddings import (
    DashScopeEmbeddingModel,
    DeterministicEmbeddingModel,
    EmbeddingModel,
)
from accesspilot.approvals import (
    ApprovalActorMismatchError,
    ApprovalAlreadyStartedError,
    ApprovalNotFoundError,
    ApprovalOutOfOrderError,
    ApprovalRoutingError,
    ApprovalStepAlreadyDecidedError,
    ApprovalTerminalError,
    ApprovalWorkspaceMismatchError,
    decide_approval,
    require_approval_startable,
    start_approval_case,
)
from accesspilot.config import Settings
from accesspilot.db.models import (
    AccessGrantRecord,
    ApprovalCaseRecord,
    ApprovalStepRecord,
    ProvisioningAttemptRecord,
)
from accesspilot.db.session import build_engine, build_session_factory
from accesspilot.db.workspace_store import SqlAlchemyWorkspaceStore
from accesspilot.domain.models import RequestDraft
from accesspilot.provisioning import (
    ApprovalRequiredError,
    IamProvisioner,
    IdempotencyConflictError,
    ProvisioningAttemptNotFoundError,
    ProvisioningNotFoundError,
    ProvisioningWorkspaceMismatchError,
    SimulatedIamProvisioner,
    provision_access,
    recover_provisioning,
)
from accesspilot.requests import (
    RequestNotReadyError,
    RequestValidationError,
    RequestWorkspaceNotFoundError,
    submit_access_request,
)
from accesspilot.risk.deepseek import DeepSeekRiskReviewModel
from accesspilot.risk.review import (
    DeterministicRiskReviewModel,
    RiskReviewFailed,
    RiskReviewModel,
    RiskReviewRequestNotFoundError,
    review_request_risk,
)
from accesspilot.workspaces import (
    UnknownWorkspaceError,
    Workspace,
    WorkspaceService,
    WorkspaceStore,
)


class ApprovalDecisionBody(BaseModel):
    """人工审批 API 唯一允许接收的决定字段。"""

    model_config = ConfigDict(extra="forbid")

    actor_id: str
    decision: Literal["approve", "reject"]
    comment: str | None = None


class ProvisionAccessBody(BaseModel):
    """开通 API 只接收调用方生成并稳定复用的幂等键。"""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str


class FaultModeBody(BaseModel):
    """Demo Workspace 可选择的可控 IAM 故障。"""

    model_config = ConfigDict(extra="forbid")

    fault_mode: Literal["iam_failure", "iam_timeout"] | None


def create_app(
    settings: Settings | None = None,
    store: WorkspaceStore | None = None,
    session_factory: sessionmaker[Session] | None = None,
    embedding_model: EmbeddingModel | None = None,
    risk_review_model: RiskReviewModel | None = None,
    iam_provisioner: IamProvisioner | None = None,
) -> FastAPI:
    """创建一个可配置、可测试的 FastAPI 应用。"""
    active_settings = settings or Settings()
    active_session_factory = session_factory or build_session_factory(
        build_engine(active_settings.database_url)
    )
    if store is None:
        # 实际启动时默认用 PostgreSQL，所以 API 重启不会丢失 Workspace。
        # 测试需要纯内存存储时，会通过 store= 显式注入。
        store = SqlAlchemyWorkspaceStore(active_session_factory)
    if embedding_model is None:
        if active_settings.dashscope_api_key is None:
            embedding_model = DeterministicEmbeddingModel()
        else:
            embedding_model = DashScopeEmbeddingModel(
                api_key=active_settings.dashscope_api_key.get_secret_value(),
                model_name=active_settings.dashscope_embedding_model,
                base_url=active_settings.dashscope_base_url,
            )
    if risk_review_model is None:
        if active_settings.deepseek_api_key is None:
            risk_review_model = DeterministicRiskReviewModel()
        else:
            risk_review_model = DeepSeekRiskReviewModel(
                api_key=active_settings.deepseek_api_key.get_secret_value(),
                model_name=active_settings.deepseek_model,
                base_url=active_settings.deepseek_base_url,
            )
    if iam_provisioner is None:
        iam_provisioner = SimulatedIamProvisioner()
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

    def approval_payload(
        session: Session,
        case: ApprovalCaseRecord,
    ) -> dict[str, object]:
        """返回前端展示所需的审批事实，不推导或伪造状态。"""

        steps = session.query(ApprovalStepRecord).filter_by(
            approval_case_id=case.id
        ).order_by(ApprovalStepRecord.step_order).all()
        return {
            "approval_case_id": str(case.id),
            "approval_status": case.approval_status,
            "steps": [
                {
                    "step_id": str(step.id),
                    "step_order": step.step_order,
                    "approver_id": step.approver_id,
                    "approver_role": step.approver_role,
                    "step_status": step.step_status,
                    "comment": step.comment,
                }
                for step in steps
            ],
        }

    def provisioning_payload(
        session: Session,
        attempt: ProvisioningAttemptRecord,
    ) -> dict[str, object]:
        """从数据库事实返回开通状态，不把审批通过冒充为已授权。"""

        grant = session.scalar(
            select(AccessGrantRecord).where(
                AccessGrantRecord.request_id == attempt.request_id
            )
        )
        return {
            "provisioning_attempt_id": str(attempt.id),
            "provisioning_status": attempt.provisioning_status,
            "attempt_count": attempt.attempt_count,
            "last_error": attempt.last_error,
            "access_granted": grant is not None,
            "grant_id": str(grant.id) if grant is not None else None,
        }

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

    @app.post("/api/workspaces/fault-mode")
    def set_workspace_fault_mode(
        body: FaultModeBody,
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> dict[str, str | None]:
        """为当前 Workspace 设置或清除可控 IAM 故障。"""

        updated = workspace_service.set_fault_mode(
            workspace.token,
            body.fault_mode,
        )
        return {"fault_mode": updated.fault_mode}

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

    @app.post("/api/requests", status_code=201)
    def submit_request(
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> dict[str, str]:
        """把当前 Workspace 中已明确确认的草稿冻结为正式申请。"""

        if workspace.draft is None or workspace.draft.missing_fields():
            raise HTTPException(status_code=409, detail="申请草稿尚未完成")

        with active_session_factory() as session:
            try:
                request = submit_access_request(
                    session,
                    workspace_token=workspace.token,
                    draft=workspace.draft,
                )
            except RequestNotReadyError as error:
                raise HTTPException(
                    status_code=409,
                    detail="申请草稿尚未明确确认",
                ) from error
            except RequestWorkspaceNotFoundError as error:
                raise HTTPException(
                    status_code=404,
                    detail="Workspace not found",
                ) from error
            except RequestValidationError as error:
                raise HTTPException(
                    status_code=422,
                    detail="申请未通过目录校验",
                ) from error

        return {
            "request_id": str(request.id),
            "request_status": request.request_status,
        }

    @app.post("/api/requests/{request_id}/approval-case", status_code=201)
    def create_approval_case(
        request_id: UUID,
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> dict[str, object]:
        """执行只读风险审查后，为正式申请创建人工审批路线。"""

        with active_session_factory() as session:
            try:
                # 先做本地所有权和重复检查，避免越权请求触发外部模型调用。
                require_approval_startable(
                    session,
                    workspace_token=workspace.token,
                    request_id=request_id,
                )
                review = review_request_risk(
                    session,
                    request_id=request_id,
                    embedding_model=embedding_model,
                    review_model=risk_review_model,
                )
                case = start_approval_case(
                    session,
                    workspace_token=workspace.token,
                    request_id=request_id,
                    review=review,
                )
            except (RiskReviewRequestNotFoundError, ApprovalNotFoundError) as error:
                raise HTTPException(status_code=404, detail="申请不存在") from error
            except ApprovalWorkspaceMismatchError as error:
                raise HTTPException(status_code=404, detail="申请不存在") from error
            except ApprovalAlreadyStartedError as error:
                raise HTTPException(status_code=409, detail="审批流已经创建") from error
            except ApprovalRoutingError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
            except RiskReviewFailed as error:
                raise HTTPException(
                    status_code=503,
                    detail="风险审查暂时不可用，请稍后重试",
                ) from error
            return approval_payload(session, case)

    @app.post("/api/approval-cases/{case_id}/decisions")
    def decide_approval_step(
        case_id: UUID,
        body: ApprovalDecisionBody,
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> dict[str, object]:
        """由当前指定审批人批准或驳回一个审批步骤。"""

        with active_session_factory() as session:
            try:
                case = decide_approval(
                    session,
                    workspace_token=workspace.token,
                    case_id=case_id,
                    actor_id=body.actor_id,
                    decision=body.decision,
                    comment=body.comment,
                )
            except (ApprovalNotFoundError, ApprovalWorkspaceMismatchError) as error:
                raise HTTPException(status_code=404, detail="审批流不存在") from error
            except ApprovalActorMismatchError as error:
                raise HTTPException(
                    status_code=403,
                    detail="当前员工不是指定审批人",
                ) from error
            except ApprovalOutOfOrderError as error:
                raise HTTPException(status_code=409, detail="前序审批尚未完成") from error
            except ApprovalStepAlreadyDecidedError as error:
                raise HTTPException(status_code=409, detail="该审批步骤已经决定") from error
            except ApprovalTerminalError as error:
                raise HTTPException(status_code=409, detail="审批流已经结束") from error
            return approval_payload(session, case)

    @app.post("/api/requests/{request_id}/provision")
    def provision_request(
        request_id: UUID,
        body: ProvisionAccessBody,
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> dict[str, object]:
        """用稳定幂等键开通已完成审批的权限。"""

        with active_session_factory() as session:
            try:
                attempt = provision_access(
                    session,
                    workspace_token=workspace.token,
                    request_id=request_id,
                    idempotency_key=body.idempotency_key,
                    iam=iam_provisioner,
                )
            except (ProvisioningNotFoundError, ProvisioningWorkspaceMismatchError) as error:
                raise HTTPException(status_code=404, detail="申请不存在") from error
            except ApprovalRequiredError as error:
                raise HTTPException(
                    status_code=409,
                    detail="人工审批尚未全部通过",
                ) from error
            except IdempotencyConflictError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            return provisioning_payload(session, attempt)

    @app.post("/api/requests/{request_id}/provision/recover")
    def recover_request_provisioning(
        request_id: UUID,
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> dict[str, object]:
        """查询原幂等操作并恢复未知开通结果。"""

        with active_session_factory() as session:
            try:
                attempt = recover_provisioning(
                    session,
                    workspace_token=workspace.token,
                    request_id=request_id,
                    iam=iam_provisioner,
                )
            except (ProvisioningNotFoundError, ProvisioningWorkspaceMismatchError) as error:
                raise HTTPException(status_code=404, detail="申请不存在") from error
            except ApprovalRequiredError as error:
                raise HTTPException(
                    status_code=409,
                    detail="人工审批尚未全部通过",
                ) from error
            except ProvisioningAttemptNotFoundError as error:
                raise HTTPException(
                    status_code=409,
                    detail="还没有可恢复的开通尝试",
                ) from error
            return provisioning_payload(session, attempt)

    return app


app = create_app()
