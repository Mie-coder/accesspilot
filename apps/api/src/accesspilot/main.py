"""FastAPI 应用入口"""

from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.deepseek import DeepSeekStructuredReplyModel
from accesspilot.agent.embeddings import (
    DashScopeEmbeddingModel,
    DeterministicEmbeddingModel,
    EmbeddingModel,
)
from accesspilot.agent.structured_reply import StructuredReplyModel
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
from accesspilot.conversation import (
    ConversationInputError,
    DeterministicStructuredReplyModel,
    handle_chat_message,
)
from accesspilot.db.models import (
    AccessGrantRecord,
    ApprovalCaseRecord,
    ApprovalStepRecord,
    EmployeeRecord,
    ProvisioningAttemptRecord,
)
from accesspilot.db.session import build_engine, build_session_factory
from accesspilot.db.workspace_store import SqlAlchemyWorkspaceStore
from accesspilot.domain.models import RequestDraft
from accesspilot.events import (
    ModelQuotaExceededError,
    format_sse_event,
    get_model_quota,
    list_workspace_events,
)
from accesspilot.operations import (
    OperationsNotFoundError,
    get_request_detail,
    list_approval_inbox,
)
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
    RequestActorMismatchError,
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
    InvalidDemoActorError,
    UnknownWorkspaceError,
    Workspace,
    WorkspaceService,
    WorkspaceStore,
)


class ApprovalDecisionBody(BaseModel):
    """人工审批 API 唯一允许接收的决定字段。"""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    comment: str | None = None


class WorkspaceIdentityBody(BaseModel):
    """前端只能选择预置演示员工，不能自由构造业务身份。"""

    model_config = ConfigDict(extra="forbid")

    employee_id: str


class ProvisionAccessBody(BaseModel):
    """开通 API 只接收调用方生成并稳定复用的幂等键。"""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str


class FaultModeBody(BaseModel):
    """Demo Workspace 可选择的可控 IAM 故障。"""

    model_config = ConfigDict(extra="forbid")

    fault_mode: Literal["iam_failure", "iam_timeout"] | None


class ChatMessageBody(BaseModel):
    """对话入口只接收一条用户可见文本。"""

    model_config = ConfigDict(extra="forbid")

    # 在进入业务逻辑前限制长度，避免无效消息先消耗模型额度。
    content: str = Field(max_length=10_000)


def create_app(
    settings: Settings | None = None,
    store: WorkspaceStore | None = None,
    session_factory: sessionmaker[Session] | None = None,
    embedding_model: EmbeddingModel | None = None,
    risk_review_model: RiskReviewModel | None = None,
    iam_provisioner: IamProvisioner | None = None,
    structured_reply_model: StructuredReplyModel | None = None,
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
    if structured_reply_model is None:
        if active_settings.deepseek_api_key is None:
            structured_reply_model = DeterministicStructuredReplyModel()
        else:
            structured_reply_model = DeepSeekStructuredReplyModel(
                api_key=active_settings.deepseek_api_key.get_secret_value(),
                model_name=active_settings.deepseek_model,
                base_url=active_settings.deepseek_base_url,
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

    def identity_payload(workspace: Workspace) -> dict[str, object]:
        """从目录返回后端绑定的演示员工事实。"""

        with active_session_factory() as session:
            employee = session.get(EmployeeRecord, workspace.actor_id)
        if employee is None:
            raise HTTPException(status_code=422, detail="演示员工不存在")
        return {
            "employee_id": employee.employee_id,
            "name": employee.name,
            "department": employee.department,
            "roles": employee.roles,
        }

    @app.get("/health")
    def health() -> dict[str, str]:
        """返回最小存活状态，不访问外部依赖"""
        return {"status": "ok"}

    @app.get("/ready")
    def readiness() -> dict[str, str]:
        """确认 API 能读取数据库；不调用 DeepSeek、百炼或 IAM。"""

        try:
            with active_session_factory() as session:
                session.execute(text("SELECT 1"))
        except SQLAlchemyError as error:
            raise HTTPException(status_code=503, detail="数据库暂不可用") from error
        return {"status": "ready"}

    @app.get("/api/events")
    def replay_events(
        request: Request,
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> StreamingResponse:
        """按 Last-Event-ID 回放当前 Workspace 尚未收到的安全事件。"""

        raw_last_event_id = request.headers.get("Last-Event-ID", "0")
        try:
            after_id = int(raw_last_event_id)
        except ValueError as error:
            raise HTTPException(
                status_code=400,
                detail="Last-Event-ID 必须是非负整数",
            ) from error
        if after_id < 0:
            raise HTTPException(
                status_code=400,
                detail="Last-Event-ID 必须是非负整数",
            )
        with active_session_factory() as session:
            chunks = [
                format_sse_event(event)
                for event in list_workspace_events(
                    session,
                    workspace_token=workspace.token,
                    after_id=after_id,
                )
            ]
        return StreamingResponse(
            iter(chunks),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/model-quota")
    def read_model_quota(
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> dict[str, int]:
        """读取当前配额；只读回放不会消耗模型次数。"""

        with active_session_factory() as session:
            quota = get_model_quota(
                session,
                workspace_token=workspace.token,
            )
        return quota.model_dump()

    @app.post("/api/chat/messages")
    def create_chat_message(
        body: ChatMessageBody,
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> dict[str, object]:
        """处理一轮申请对话，并持久化前端可回放的安全事件。"""

        try:
            turn = handle_chat_message(
                active_session_factory,
                workspace_service=workspace_service,
                workspace_token=workspace.token,
                content=body.content,
                model=structured_reply_model,
            )
        except ModelQuotaExceededError as error:
            raise HTTPException(
                status_code=429,
                detail="模型调用额度已用尽，当前为只读回放模式",
            ) from error
        except ConversationInputError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return turn.model_dump(mode="json")

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

    @app.post("/api/workspaces/ensure")
    def ensure_workspace(request: Request, response: Response) -> dict[str, str]:
        """复用有效 Workspace；首次访问或陈旧 Cookie 才创建新空间。"""

        token = request.cookies.get(active_settings.workspace_cookie_name)
        if token is not None:
            try:
                workspace_service.get(token)
                return {"status": "existing"}
            except UnknownWorkspaceError:
                # 陈旧 Cookie 不应让首次加载先产生 404，再由前端猜测恢复方式。
                pass
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

    @app.get("/api/workspaces/identity")
    def read_workspace_identity(
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> dict[str, object]:
        """读取当前 Workspace 的后端演示身份。"""

        return identity_payload(workspace)

    @app.post("/api/workspaces/identity")
    def switch_workspace_identity(
        body: WorkspaceIdentityBody,
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> dict[str, object]:
        """仅在预置虚构员工之间切换，旧草稿保持原样。"""

        try:
            with active_session_factory() as session:
                employee = session.get(EmployeeRecord, body.employee_id)
            if employee is None:
                raise InvalidDemoActorError(body.employee_id)
            updated = workspace_service.set_actor(workspace.token, body.employee_id)
        except InvalidDemoActorError as error:
            raise HTTPException(status_code=422, detail="不支持该演示身份") from error
        return identity_payload(updated)

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

        if (
            workspace.draft is not None
            and workspace.draft.employee_id is not None
            and workspace.draft.employee_id != workspace.actor_id
        ):
            # 角色切换后旧草稿仍属于原申请人，不允许该入口静默覆盖。
            raise HTTPException(
                status_code=409,
                detail="当前草稿属于另一演示身份，请先切回原身份",
            )
        # employee_id 是后端事实；请求体中的同名字段只为兼容旧表单。
        bound_draft = draft.model_copy(update={"employee_id": workspace.actor_id})
        workspace_service.save_draft(workspace.token, bound_draft)
        missing_fields = bound_draft.missing_fields()
        return {
            "draft": bound_draft.model_dump(mode="json"),
            "missing_fields": missing_fields,
            "is_complete": not missing_fields,
            # 这里只返回送审资格，不创建审批记录，也不代表权限已经开通。
            "can_enter_approval": bound_draft.can_enter_approval(),
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
                    actor_id=workspace.actor_id,
                )
            except RequestActorMismatchError as error:
                raise HTTPException(
                    status_code=409,
                    detail="当前草稿属于另一演示身份，请切回原身份后提交",
                ) from error
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

    @app.get("/api/approval-inbox")
    def read_approval_inbox(
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> dict[str, object]:
        """读取当前演示身份真正轮到处理的审批步骤。"""

        with active_session_factory() as session:
            try:
                return list_approval_inbox(
                    session,
                    workspace_token=workspace.token,
                    actor_id=workspace.actor_id,
                )
            except OperationsNotFoundError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error

    @app.get("/api/requests/{request_id}")
    def read_request_detail(
        request_id: UUID,
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> dict[str, object]:
        """读取申请、审批、开通与只追加审计事实。"""

        with active_session_factory() as session:
            try:
                return get_request_detail(
                    session,
                    workspace_token=workspace.token,
                    request_id=request_id,
                )
            except OperationsNotFoundError as error:
                raise HTTPException(status_code=404, detail="申请不存在") from error

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
                    actor_id=workspace.actor_id,
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
