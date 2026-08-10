"""FastAPI 应用入口"""

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator
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
    _redact_sensitive_content,
    contains_protected_internal_content,
    handle_chat_message,
    is_suspicious_protected_prefix,
    prepare_chat_message,
    split_safe_model_output_prefix,
)
from accesspilot.db.models import (
    AccessGrantRecord,
    ApprovalCaseRecord,
    ApprovalStepRecord,
    EmployeeRecord,
    ProvisioningAttemptRecord,
    WorkspaceEventRecord,
)
from accesspilot.db.session import build_engine, build_session_factory
from accesspilot.db.workspace_store import SqlAlchemyWorkspaceStore
from accesspilot.domain.models import RequestDraft
from accesspilot.events import (
    ModelQuotaExceededError,
    TurnInProgressError,
    append_turn_started,
    append_turn_terminal,
    format_sse_event,
    get_model_quota,
    list_turn_events,
    list_workspace_events,
    validate_event_payload,
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
from accesspilot.streaming import (
    AnswerStreamModel,
    DeterministicAnswerStreamModel,
    SafeStreamingResponse,
    encode_persisted_frame,
    encode_sse_frame,
)
from accesspilot.tools.catalog import ToolResult, validate_access_request
from accesspilot.tools.executor import ReadOnlyToolCall, execute_read_only_tool
from accesspilot.tools.policies import PolicyAnswer, PolicyService
from accesspilot.workspaces import (
    InvalidDemoActorError,
    UnknownWorkspaceError,
    Workspace,
    WorkspaceService,
    WorkspaceStore,
)


class DemoSessionBody(BaseModel):
    """显式进入虚构演示场景时选择的预置身份。"""

    model_config = ConfigDict(extra="forbid")

    employee_id: str


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


class PolicyQueryBody(BaseModel):
    """政策检索入口只接收用户可见问题，不接受身份或工具参数。"""

    model_config = ConfigDict(extra="forbid")

    query: StrictStr = Field(min_length=1, max_length=2_000)

    @field_validator("query")
    @classmethod
    def reject_blank_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("政策问题不能为空")
        return normalized


def create_app(
    settings: Settings | None = None,
    store: WorkspaceStore | None = None,
    session_factory: sessionmaker[Session] | None = None,
    embedding_model: EmbeddingModel | None = None,
    risk_review_model: RiskReviewModel | None = None,
    iam_provisioner: IamProvisioner | None = None,
    structured_reply_model: StructuredReplyModel | None = None,
    answer_stream_model: AnswerStreamModel | None = None,
    policy_service: PolicyService | None = None,
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
    assert embedding_model is not None
    active_policy_service = policy_service or PolicyService(
        embedding_model=embedding_model,
        similarity_threshold=active_settings.policy_similarity_threshold,
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
    active_answer_stream_model: AnswerStreamModel = (
        answer_stream_model or DeterministicAnswerStreamModel()
    )
    workspace_service = WorkspaceService(
        store,
        product_actor_id=active_settings.product_actor_id,
        demo_mode_enabled=active_settings.demo_mode_enabled,
    )
    app = FastAPI(title=active_settings.app_name)

    def _set_workspace_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            key=active_settings.workspace_cookie_name,
            value=token,
            httponly=True,
            samesite="lax",
            secure=active_settings.workspace_cookie_secure,
        )

    def _set_product_backup_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            key=active_settings.product_workspace_cookie_name,
            value=token,
            httponly=True,
            samesite="lax",
            secure=active_settings.workspace_cookie_secure,
        )

    def _clear_product_backup_cookie(response: Response) -> None:
        response.delete_cookie(active_settings.product_workspace_cookie_name)

    def _resolve_workspace(request: Request, response: Response) -> Workspace:
        """解析主 Cookie；关闭 Demo 时永不把旧 Demo 空间当产品空间。"""

        token = request.cookies.get(active_settings.workspace_cookie_name)
        if token is None:
            raise HTTPException(status_code=401, detail="Workspace cookie 是必须的")
        try:
            raw_workspace = workspace_service.peek(token)
        except UnknownWorkspaceError as error:
            raise HTTPException(status_code=404, detail="Workspace not found") from error

        backup_token = request.cookies.get(active_settings.product_workspace_cookie_name)
        if active_settings.demo_mode_enabled or not raw_workspace.demo_session_active:
            workspace = workspace_service.get(token)
            if not active_settings.demo_mode_enabled and backup_token is not None:
                _clear_product_backup_cookie(response)
            return workspace

        if backup_token is not None:
            try:
                product_raw = workspace_service.peek(backup_token)
            except UnknownWorkspaceError:
                product_raw = None
            if product_raw is not None and not product_raw.demo_session_active:
                product_workspace = workspace_service.get(backup_token)
                workspace_service.exit_demo(token)
                _set_workspace_cookie(response, product_workspace.token)
                _clear_product_backup_cookie(response)
                return product_workspace

        workspace_service.get(token)
        clean_workspace = workspace_service.create()
        _set_workspace_cookie(response, clean_workspace.token)
        _clear_product_backup_cookie(response)
        return clean_workspace

    def require_workspace(
        request: Request,
        response: Response,
    ) -> Workspace:
        """读取当前有效 Workspace；必要时恢复产品 Cookie。"""

        return _resolve_workspace(request, response)

    def require_active_demo(
        request: Request,
        response: Response,
    ) -> Workspace:
        """Demo 控制 API 只有在功能开启且显式进入场景后可用。"""

        if not active_settings.demo_mode_enabled:
            raise HTTPException(status_code=404, detail="Demo 控制台未启用")
        workspace = _resolve_workspace(request, response)
        if not workspace.demo_session_active or workspace.demo_actor_id is None:
            raise HTTPException(status_code=404, detail="尚未进入 Demo 场景")
        return workspace

    def require_demo_feature(
        request: Request,
        response: Response,
    ) -> Workspace:
        """Demo API 先检查功能开关，再解析 Cookie。"""

        if not active_settings.demo_mode_enabled:
            raise HTTPException(status_code=404, detail="Demo 控制台未启用")
        return _resolve_workspace(request, response)

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


    def demo_session_payload(workspace: Workspace) -> dict[str, object]:
        """返回 Demo 控制台需要的会话状态，不混入普通产品身份响应。"""

        payload: dict[str, object] = {
            "demo_mode_enabled": active_settings.demo_mode_enabled,
            "demo_session_active": workspace.demo_session_active,
        }
        payload["fault_mode"] = workspace.fault_mode if workspace.demo_session_active else None
        if workspace.demo_session_active and workspace.demo_actor_id:
            payload["employee_id"] = workspace.demo_actor_id
        return payload
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

    @app.get("/api/policies")
    def read_policy_catalog() -> dict[str, object]:
        """返回完整政策目录；政策目录是公开只读事实，不依赖 Workspace。"""

        try:
            with active_session_factory() as session:
                result = execute_read_only_tool(
                    session,
                    workspace_token="policy-catalog-api",
                    call=ReadOnlyToolCall(tool="list_policy_catalog"),
                    policy_service=active_policy_service,
                )
        except Exception:
            return {"policies": [], "status": "retrieval_unavailable"}
        if result.policy_catalog is None:
            return {"policies": [], "status": "retrieval_unavailable"}
        return {
            "policies": [item.model_dump(mode="json") for item in result.policy_catalog]
        }

    @app.post("/api/policies/query")
    def query_policy(body: PolicyQueryBody) -> dict[str, object]:
        """返回带证据和三态状态的政策答案，绝不回显供应商异常。"""

        safe_query = _redact_sensitive_content(body.query)
        if not safe_query.strip():
            raise HTTPException(status_code=422, detail="政策问题不能为空")
        try:
            with active_session_factory() as session:
                result = execute_read_only_tool(
                    session,
                    workspace_token="policy-query-api",
                    call=ReadOnlyToolCall(tool="search_policies", query=safe_query),
                    policy_service=active_policy_service,
                )
        except Exception:
            result = None
        if result is None or result.policy_answer is None:
            # 执行器异常也要闭合为稳定业务状态，而不是 HTTP 500 或空文本。
            return PolicyAnswer(
                status="retrieval_unavailable",
                answer="政策检索暂时不可用，当前无法提供可靠依据。",
                evidence=[],
                next_step="请稍后重试；如问题紧急，请联系人工安全流程。",
            ).model_dump(mode="json")
        return result.policy_answer.model_dump(mode="json")

    @app.get("/api/events")
    async def replay_events(
        request: Request,
        follow: bool = True,
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> StreamingResponse:
        """回放安全事件；默认持续跟随，follow=false 只返回有限历史。"""

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

        async def replay_generator() -> AsyncIterator[str]:
            cursor = after_id
            last_heartbeat = time.monotonic()
            while True:
                with active_session_factory() as session:
                    events = list_workspace_events(
                        session,
                        workspace_token=workspace.token,
                        after_id=cursor,
                    )
                if events:
                    for event in events:
                        cursor = max(cursor, event.id)
                        # 历史回放保留旧 message.assistant 合同；当前轮客户端
                        # 使用 /stream 的 v1 envelope 与 turn:seq 游标。
                        yield format_sse_event(event)
                if not follow:
                    return
                if await request.is_disconnected():
                    return
                now = time.monotonic()
                if now - last_heartbeat >= 15:
                    yield ": heartbeat\n\n"
                    last_heartbeat = now
                await asyncio.sleep(0.5)

        return StreamingResponse(
            replay_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/demo/model-quota")
    def read_model_quota(
        workspace: Workspace = Depends(require_active_demo),  # noqa: B008
    ) -> dict[str, int]:
        """读取当前配额；只读回放不会消耗模型次数。"""

        with active_session_factory() as session:
            quota = get_model_quota(
                session,
                workspace_token=workspace.token,
            )
        return quota.model_dump()

    @app.post("/api/chat/messages/stream")
    async def create_chat_message_stream(
        body: ChatMessageBody,
        request: Request,
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> SafeStreamingResponse:
        """当前轮真实 SSE：先发 started，再在线程池执行同步 prepare。"""

        turn_id = str(uuid4())
        try:
            with active_session_factory() as session:
                started_event = append_turn_started(
                    session,
                    workspace_token=workspace.token,
                    turn_id=turn_id,
                )
        except TurnInProgressError as error:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "TURN_IN_PROGRESS",
                    "message": "当前 Workspace 已有进行中的对话轮",
                },
            ) from error

        def persist_terminal(
            event_type: str,
            payload: dict[str, object],
        ) -> WorkspaceEventRecord | None:
            with active_session_factory() as session:
                return append_turn_terminal(
                    session,
                    workspace_token=workspace.token,
                    turn_id=turn_id,
                    event_type=event_type,
                    payload=payload,
                )

        async def persist_interrupted() -> None:
            persist_terminal(
                "turn.interrupted",
                {
                    "turn_id": turn_id,
                    "reason": "client_cancelled",
                    "retryable": True,
                },
            )

        async def stream_generator() -> AsyncIterator[str]:
            seq = 1
            # 首帧只依赖已提交的 started 事实，不等待同步结构化提取。
            yield encode_persisted_frame(
                started_event,
                turn_id=turn_id,
                seq=seq,
            )
            seq += 1
            prepared = None
            try:
                prepared = await asyncio.to_thread(
                    prepare_chat_message,
                    active_session_factory,
                    workspace_service=workspace_service,
                    workspace_token=workspace.token,
                    content=body.content,
                    model=structured_reply_model,
                    turn_id=turn_id,
                    policy_service=active_policy_service,
                )
            except ModelQuotaExceededError:
                message = "模型调用额度已用尽，当前为只读回放模式"
                event = persist_terminal(
                    "error.recoverable",
                    {
                        "turn_id": turn_id,
                        "code": "MODEL_QUOTA_EXCEEDED",
                        "message": message,
                    },
                )
                if event is not None:
                    yield encode_persisted_frame(event, turn_id=turn_id, seq=seq)
                return
            except ConversationInputError:
                message = "我暂时没能可靠理解这条消息，请稍后重试或换一种说法。"
                event = persist_terminal(
                    "error.recoverable",
                    {
                        "turn_id": turn_id,
                        "code": "INVALID_CONVERSATION_INPUT",
                        "message": message,
                    },
                )
                if event is not None:
                    yield encode_persisted_frame(event, turn_id=turn_id, seq=seq)
                return
            except Exception:
                # 流式边界不泄漏供应商异常、请求头、Key 或配额细节。
                message = "我暂时没能可靠理解这条消息，请稍后重试或换一种说法。"
                event = persist_terminal(
                    "error.recoverable",
                    {
                        "turn_id": turn_id,
                        "code": "MODEL_REPLY_UNAVAILABLE",
                        "message": message,
                    },
                )
                if event is not None:
                    yield encode_persisted_frame(event, turn_id=turn_id, seq=seq)
                return

            assert prepared is not None
            with active_session_factory() as session:
                facts = list_turn_events(
                    session,
                    workspace_token=workspace.token,
                    turn_id=turn_id,
                )
            for event in facts:
                if event.id == started_event.id:
                    continue
                if event.event_type not in {
                    "intent.detected",
                    "tool.summary",
                    "draft.updated",
                    "business.status",
                }:
                    # message.user/security.notice/message.assistant 等审计事实仍
                    # 保留在 Workspace event log，但不进入 current v1 UI contract。
                    continue
                if event.event_type == "tool.summary":
                    tool = str(event.payload.get("tool", "read_only_tool"))
                    tool_call_id = str(
                        uuid5(NAMESPACE_URL, f"accesspilot:{turn_id}:{event.id}")
                    )
                    started_payload: dict[str, object] = {
                        "turn_id": turn_id,
                        "tool": tool,
                        "tool_call_id": tool_call_id,
                    }
                    yield encode_sse_frame(
                        event_type="tool.started",
                        turn_id=turn_id,
                        seq=seq,
                        payload=started_payload,
                    )
                    seq += 1
                    completed_payload = dict(event.payload)
                    completed_payload["turn_id"] = turn_id
                    completed_payload["tool_call_id"] = tool_call_id
                    yield encode_sse_frame(
                        event_type="tool.completed",
                        turn_id=turn_id,
                        seq=seq,
                        payload=completed_payload,
                        occurred_at=event.created_at,
                    )
                    seq += 1
                    continue
                yield encode_persisted_frame(event, turn_id=turn_id, seq=seq)
                seq += 1

            if prepared.business_status in {
                "recoverable_error",
                "validation_failed",
                "resolution_unavailable",
            }:
                message = prepared.assistant_message
                event = persist_terminal(
                    "error.recoverable",
                    {
                        "turn_id": turn_id,
                        "code": "BUSINESS_VALIDATION_FAILED",
                        "message": message,
                    },
                )
                if event is not None:
                    yield encode_persisted_frame(event, turn_id=turn_id, seq=seq)
                return

            chunks: list[str] = []
            pending = ""
            full_content = ""
            try:
                async for delta in active_answer_stream_model.stream_answer(
                    assistant_message=prepared.assistant_message,
                    turn_id=turn_id,
                ):
                    if await request.is_disconnected():
                        raise asyncio.CancelledError
                    if not isinstance(delta, str):
                        raise ValueError("回答增量类型不安全")
                    if not delta:
                        continue
                    candidate = full_content + delta
                    # 校验累计文本后再发送，跨 chunk 拼成凭证时阻断后续片段；
                    # message.delta 从不落库，错误只闭合为安全 recoverable terminal。
                    if contains_protected_internal_content(candidate):
                        raise ValueError("回答增量包含不可展示的内部内容")
                    validate_event_payload(
                        "message.assistant",
                        {"turn_id": turn_id, "content": candidate},
                    )
                    full_content = candidate
                    pending += delta
                    safe_delta, pending = split_safe_model_output_prefix(pending)
                    if safe_delta:
                        chunks.append(safe_delta)
                        yield encode_sse_frame(
                            event_type="message.delta",
                            turn_id=turn_id,
                            seq=seq,
                            payload={"text": safe_delta},
                        )
                        seq += 1
            except asyncio.CancelledError:
                event = persist_terminal(
                    "turn.interrupted",
                    {
                        "turn_id": turn_id,
                        "reason": "client_cancelled",
                        "retryable": True,
                    },
                )
                if event is not None:
                    yield encode_persisted_frame(event, turn_id=turn_id, seq=seq)
                return
            except Exception:
                event = persist_terminal(
                    "error.recoverable",
                    {
                        "turn_id": turn_id,
                        "code": "ANSWER_STREAM_UNAVAILABLE",
                        "message": "回答流暂时不可用，请稍后重试。",
                    },
                )
                if event is not None:
                    yield encode_persisted_frame(event, turn_id=turn_id, seq=seq)
                return

            if pending and is_suspicious_protected_prefix(pending):
                event = persist_terminal(
                    "error.recoverable",
                    {
                        "turn_id": turn_id,
                        "code": "ANSWER_STREAM_UNAVAILABLE",
                        "message": "回答流暂时不可用，请稍后重试。",
                    },
                )
                if event is not None:
                    yield encode_persisted_frame(event, turn_id=turn_id, seq=seq)
                return

            if pending:
                chunks.append(pending)
                yield encode_sse_frame(
                    event_type="message.delta",
                    turn_id=turn_id,
                    seq=seq,
                    payload={"text": pending},
                )
                seq += 1
            content = "".join(chunks) or prepared.assistant_message
            event = persist_terminal(
                "message.completed",
                {
                    "turn_id": turn_id,
                    "message_id": str(uuid4()),
                    "content": content,
                },
            )
            if event is None:
                return
            payload = dict(event.payload)
            payload["persisted_event_id"] = event.id
            yield encode_persisted_frame(
                event,
                turn_id=turn_id,
                seq=seq,
                payload_override=payload,
            )

        return SafeStreamingResponse(
            stream_generator(),
            media_type="text/event-stream",
            on_disconnect=persist_interrupted,
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

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
                policy_service=active_policy_service,
            )
        except ModelQuotaExceededError as error:
            raise HTTPException(
                status_code=429,
                detail="模型调用额度已用尽，当前为只读回放模式",
            ) from error
        except ConversationInputError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return turn.model_dump(mode="json", exclude={"quota"})

    @app.post("/api/workspaces")
    def create_workspace(request: Request, response: Response) -> dict[str, str]:
        """首次创建 Workspace；已有有效 Cookie 时保持原空间。"""

        token = request.cookies.get(active_settings.workspace_cookie_name)
        if token is not None:
            try:
                workspace_service.peek(token)
            except UnknownWorkspaceError:
                pass
            else:
                _resolve_workspace(request, response)
                response.status_code = 200
                return {"status": "existing"}
        workspace = workspace_service.create()
        _set_workspace_cookie(response, workspace.token)
        response.status_code = 201
        return {"status": "created"}

    @app.post("/api/workspaces/ensure")
    def ensure_workspace(request: Request, response: Response) -> dict[str, str]:
        """复用有效 Workspace；首次或陈旧 Cookie 才创建新空间。"""

        token = request.cookies.get(active_settings.workspace_cookie_name)
        if token is not None:
            try:
                workspace_service.peek(token)
            except UnknownWorkspaceError:
                pass
            else:
                _resolve_workspace(request, response)
                return {"status": "existing"}

        backup_token = request.cookies.get(active_settings.product_workspace_cookie_name)
        if not active_settings.demo_mode_enabled and backup_token is not None:
            try:
                product_raw = workspace_service.peek(backup_token)
            except UnknownWorkspaceError:
                product_raw = None
            if product_raw is not None and not product_raw.demo_session_active:
                _set_workspace_cookie(response, backup_token)
                _clear_product_backup_cookie(response)
                return {"status": "existing"}

        workspace = workspace_service.create()
        _set_workspace_cookie(response, workspace.token)

        return {"status": "created"}
    @app.post("/api/demo/reset")
    def reset_workspace(
        response: Response,
        workspace: Workspace = Depends(require_active_demo),  # noqa: B008
    ) -> dict[str, str]:
        """只重置当前 Workspace 的可变数据。"""

        replacement = workspace_service.create()
        if workspace.demo_actor_id is None:
            raise HTTPException(status_code=409, detail="当前没有激活的演示场景")
        replacement = workspace_service.enter_demo(
            replacement.token,
            workspace.demo_actor_id,
        )
        # Keep old Demo facts, but clear its session override and fault mode.
        workspace_service.exit_demo(workspace.token)
        _set_workspace_cookie(response, replacement.token)
        return {"status": "reset"}

    @app.get("/api/workspaces/identity")
    def read_workspace_identity(
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> dict[str, object]:
        """读取当前 Workspace 的后端演示身份。"""

        return identity_payload(workspace)

    @app.post("/api/demo/session")
    def switch_workspace_identity(
        body: DemoSessionBody,
        response: Response,
        workspace: Workspace = Depends(require_demo_feature),  # noqa: B008
    ) -> dict[str, object]:
        """为每个 Demo 场景创建独立 Workspace，保留产品空间备份。"""

        try:
            with active_session_factory() as session:
                employee = session.get(EmployeeRecord, body.employee_id)
            if employee is None:
                raise InvalidDemoActorError(body.employee_id)
        except InvalidDemoActorError as error:
            raise HTTPException(status_code=422, detail="不支持该演示身份") from error

        if workspace.demo_session_active:
            # Keep facts in one Demo Workspace when switching actors.
            updated = workspace_service.enter_demo(workspace.token, body.employee_id)
        else:
            _set_product_backup_cookie(response, workspace.token)
            replacement = workspace_service.create()
            updated = workspace_service.enter_demo(replacement.token, body.employee_id)
        _set_workspace_cookie(response, updated.token)
        return {**identity_payload(updated), **demo_session_payload(updated)}

    @app.get("/api/demo/session")
    def read_demo_session(
        workspace: Workspace = Depends(require_demo_feature),  # noqa: B008
    ) -> dict[str, object]:
        return demo_session_payload(workspace)

    @app.post("/api/demo/session/exit")
    def exit_demo_session(
        request: Request,
        response: Response,
        workspace: Workspace = Depends(require_active_demo),  # noqa: B008
    ) -> dict[str, object]:
        workspace_service.exit_demo(workspace.token)
        backup_token = request.cookies.get(active_settings.product_workspace_cookie_name)
        restored: Workspace | None = None
        if backup_token is not None:
            try:
                backup = workspace_service.peek(backup_token)
            except UnknownWorkspaceError:
                backup = None
            if backup is not None and not backup.demo_session_active:
                restored = workspace_service.get(backup_token)
        if restored is None:
            restored = workspace_service.create()
        _set_workspace_cookie(response, restored.token)
        _clear_product_backup_cookie(response)
        return {**identity_payload(restored), **demo_session_payload(restored)}

    @app.post("/api/demo/fault-mode")
    def set_workspace_fault_mode(
        body: FaultModeBody,
        workspace: Workspace = Depends(require_active_demo),  # noqa: B008
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
        """解析权限、预校验草稿并返回仍需补充的字段。"""

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
        resolution_result: ToolResult | None = None
        resolution = None
        if bound_draft.entitlement_id is not None:
            with active_session_factory() as session:
                resolution_result = execute_read_only_tool(
                    session,
                    workspace_token=workspace.token,
                    call=ReadOnlyToolCall(
                        tool="resolve_entitlement",
                        query=bound_draft.entitlement_id,
                    ),
                )
            resolution = resolution_result.entitlement_resolution
            matched = (
                resolution_result.status == "success"
                and resolution is not None
                and resolution.status == "matched"
                and len(resolution.candidates) == 1
            )
            if not matched:
                current_draft = workspace.draft or RequestDraft(
                    employee_id=workspace.actor_id
                )
                return {
                    "draft": (
                        workspace.draft.model_dump(mode="json")
                        if workspace.draft is not None
                        else None
                    ),
                    "missing_fields": current_draft.missing_fields(),
                    "is_complete": not current_draft.missing_fields(),
                    "can_enter_approval": False,
                    "entitlement_resolution": (
                        resolution.model_dump(mode="json")
                        if resolution is not None
                        else None
                    ),
                    "issues": [],
                }
            assert resolution is not None
            bound_draft = bound_draft.model_copy(
                update={"entitlement_id": resolution.candidates[0].code}
            )

        existing_draft = workspace.draft
        business_fields = ("entitlement_id", "duration_days", "justification")
        if existing_draft is not None and any(
            getattr(existing_draft, field) != getattr(bound_draft, field)
            for field in business_fields
        ):
            # 任一业务字段变化后，旧确认不能沿用到新业务事实。
            bound_draft = bound_draft.model_copy(update={"confirmed": False})

        with active_session_factory() as session:
            validation_result = validate_access_request(session, bound_draft)
        issues = validation_result.issues or []
        has_non_missing_issue = any(
            not issue.code.startswith("missing_fields:") for issue in issues
        )
        if validation_result.status != "success" and (
            not issues or has_non_missing_issue
        ):
            # 任何资格、目录或期限问题都必须重新确认；纯缺字段仍保留
            # 旧 preview 的渐进式收集行为。
            bound_draft = bound_draft.model_copy(update={"confirmed": False})

        workspace_service.save_draft(workspace.token, bound_draft)
        missing_fields = bound_draft.missing_fields()
        return {
            "draft": bound_draft.model_dump(mode="json"),
            "missing_fields": missing_fields,
            "is_complete": not missing_fields,
            "can_enter_approval": (
                validation_result.status == "success"
                and bound_draft.can_enter_approval()
            ),
            "entitlement_resolution": (
                resolution.model_dump(mode="json")
                if resolution is not None
                else None
            ),
            "issues": [issue.model_dump(mode="json") for issue in issues],
        }

    @app.get("/api/drafts/current")
    def get_current_draft(
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> dict[str, object]:
        """读取当前浏览器 Workspace 中已保存的申请草稿。

        复用 require_workspace 让 Cookie 检查、过期 Token 处理和
        Workspace 查询保持一致，不从 URL 接收敏感 Token。
        """

        draft = workspace.draft
        if (
            draft is not None
            and draft.employee_id is not None
            and draft.employee_id != workspace.actor_id
        ):
            draft = None
        return {"draft": draft.model_dump(mode="json") if draft is not None else None}

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
                    fault_mode=workspace.effective_fault_mode(active_settings.demo_mode_enabled),
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
