"""FastAPI 应用入口"""

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from typing import Literal
from urllib.parse import parse_qs
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
)
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.access_overview import (
    AccessOverviewNotFoundError,
    get_access_overview,
)
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
    DecisionPacketRequiredError,
    decide_approval,
    require_approval_startable,
    start_approval_case,
)
from accesspilot.auth import (
    AuthContext,
    CsrfMismatchError,
    InvalidAuthSessionError,
    LoginAccountError,
    create_login_session,
    load_auth_context,
    revoke_session,
    rotate_csrf,
    verify_csrf,
)
from accesspilot.config import Settings
from accesspilot.conversation import (
    ConversationInputError,
    DeterministicStructuredReplyModel,
    _redact_sensitive_content,
    apply_cursor_transition,
    contains_protected_internal_content,
    handle_chat_message,
    is_suspicious_protected_prefix,
    normalized_outcome,
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
from accesspilot.decision_packets import (
    DecisionAdvisoryModel,
    DecisionPacketGenerationError,
    DecisionPacketNotFoundError,
    DecisionPacketStateError,
    decision_packet_payload,
    generate_decision_packet,
)
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
    get_latest_request_detail_for_principal,
    get_request_detail_for_principal,
    list_approval_inbox,
    list_provisioning_tasks,
    list_requests_for_principal,
)
from accesspilot.provisioning import (
    ApprovalRequiredError,
    IamProvisioner,
    IdempotencyConflictError,
    ProvisioningAttemptNotFoundError,
    ProvisioningForbiddenError,
    ProvisioningNotFoundError,
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
from accesspilot.risk.decision_packet import DeepSeekDecisionAdvisoryModel
from accesspilot.risk.review import RiskReviewModel
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


class LoginBody(BaseModel):
    """账号选择式 Mock Login；不接受密码、角色或其他身份字段。"""

    model_config = ConfigDict(extra="forbid")

    account_id: StrictStr


class DraftPreviewBody(BaseModel):
    """Client-editable draft fields; employee identity is server-owned."""

    model_config = ConfigDict(extra="forbid")

    entitlement_id: str | None = None
    duration_days: StrictInt | None = None
    justification: str | None = None
    confirmed: StrictBool = False

    @field_validator("duration_days")
    @classmethod
    def require_positive_duration(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("申请期限必须是正整数")
        return value

    @field_validator("entitlement_id", "justification")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class ApprovalDecisionBody(BaseModel):
    """人工审批 API 唯一允许接收的决定字段。"""

    model_config = ConfigDict(extra="forbid")

    approval_step_id: UUID
    decision: Literal["approve", "reject"]
    comment: str | None = None

    @field_validator("comment")
    @classmethod
    def normalize_comment(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @classmethod
    def validate_rejection_reason(
        cls,
        decision: Literal["approve", "reject"],
        comment: str | None,
    ) -> None:
        if decision == "reject" and comment is None:
            raise HTTPException(status_code=422, detail="驳回必须填写原因")


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


class EntitlementResolveBody(BaseModel):
    """权限名称解析只接收一个严格的非空查询词。"""

    model_config = ConfigDict(extra="forbid")

    query: StrictStr = Field(min_length=1, max_length=2_000)

    @field_validator("query")
    @classmethod
    def reject_blank_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("权限查询不能为空")
        return normalized


def create_app(
    settings: Settings | None = None,
    store: WorkspaceStore | None = None,
    session_factory: sessionmaker[Session] | None = None,
    embedding_model: EmbeddingModel | None = None,
    risk_review_model: RiskReviewModel | None = None,
    decision_advisory_model: DecisionAdvisoryModel | None = None,
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
    # Kept as a constructor seam for v1.1 callers; T21 never consults this
    # model from the approval endpoint. Decision advice has its own bounded
    # schema and an honest no-key unavailable mode.
    del risk_review_model
    if decision_advisory_model is None and active_settings.deepseek_api_key is not None:
        decision_advisory_model = DeepSeekDecisionAdvisoryModel(
            api_key=active_settings.deepseek_api_key.get_secret_value(),
            model_name=active_settings.deepseek_model,
            base_url=active_settings.deepseek_base_url,
            timeout_seconds=active_settings.decision_packet_timeout_seconds,
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

    # v1.2 closes the anonymous Workspace/Demo surface at the application
    # boundary.  Keeping this guard ahead of route matching also prevents a
    # stale browser from receiving a misleading 401/405 from a legacy route.
    _closed_prefixes = ("/api/workspaces", "/api/demo")
    _public_paths = {"/health", "/ready", "/api/auth/login"}
    _reserved_identity_fields = {
        "account_id",
        "employee",
        "employee_id",
        "actor",
        "actor_id",
        "role",
        "roles",
        "organization",
        "organization_code",
        "workspace",
        "workspace_id",
        "workspace_token",
        "workspace_cookie",
        "session",
        "session_id",
        "session_token",
        "auth_session",
        "auth_session_id",
        "auth_token",
        "principal",
        "tenant",
        "tenant_id",
        "organization_id",
    }
    _compact_reserved_identity_fields = {
        field.replace("_", "") for field in _reserved_identity_fields
    }

    def _normalize_identity_key(key: str) -> str:
        """Canonicalize snake, kebab, camel, and ASGI-lowercased names."""

        with_camel_boundaries = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key.strip())
        normalized = with_camel_boundaries.lower().replace("-", "_")
        return normalized.removeprefix("x_")

    def _is_reserved_identity_key(key: str) -> bool:
        normalized = _normalize_identity_key(key)
        return normalized in _reserved_identity_fields or (
            normalized.replace("_", "") in _compact_reserved_identity_fields
        )

    def _has_reserved_identity_key(key: str) -> bool:
        """Match direct and bracket/dot-qualified form/query field names."""

        normalized = _normalize_identity_key(key)
        candidates = [normalized, *re.split(r"[\[\].]+", normalized)]
        return any(
            candidate and _is_reserved_identity_key(candidate)
            for candidate in candidates
        )

    def _find_reserved_identity_keys(
        value: object,
        *,
        allow_login_account: bool,
        depth: int = 0,
    ) -> set[str]:
        """Recursively find identity-looking keys in a decoded JSON value.

        ``account_id`` is the sole exception: only a top-level login body may
        carry it.  Traversing lists as well as dictionaries prevents an
        attacker from hiding identity input under an arbitrary nested object.
        """

        found: set[str] = set()
        if isinstance(value, dict):
            for raw_key, nested in value.items():
                key = str(raw_key)
                normalized = _normalize_identity_key(key)
                if _has_reserved_identity_key(key) and not (
                    allow_login_account
                    and depth == 0
                    and normalized == "account_id"
                    and key.strip() == "account_id"
                ):
                    found.add(normalized)
                found.update(
                    _find_reserved_identity_keys(
                        nested,
                        allow_login_account=allow_login_account,
                        depth=depth + 1,
                    )
                )
        elif isinstance(value, list):
            for nested in value:
                found.update(
                    _find_reserved_identity_keys(
                        nested,
                        allow_login_account=allow_login_account,
                        depth=depth + 1,
                    )
                )
        return found

    def _multipart_field_names(body: bytes, content_type: str) -> set[str]:
        """Extract multipart field *names* from part headers only.

        This intentionally never searches multipart values.  Parsing the
        ``Content-Disposition`` header is enough to apply the identity-field
        guard without adding a multipart dependency (the API does not consume
        multipart business payloads).
        """

        boundary_match = re.search(
            r"(?:^|;)\s*boundary\s*=\s*(?:\"([^\"]+)\"|([^;\s]+))",
            content_type,
            flags=re.IGNORECASE,
        )
        if boundary_match is None:
            return set()
        boundary_text = boundary_match.group(1) or boundary_match.group(2)
        if not boundary_text:
            return set()
        delimiter = b"--" + boundary_text.encode("utf-8", errors="ignore")
        if delimiter == b"--":
            return set()

        names: set[str] = set()
        for part in body.split(delimiter):
            if part.startswith(b"--"):
                continue
            header_end = part.find(b"\r\n\r\n")
            if header_end < 0:
                header_end = part.find(b"\n\n")
            if header_end < 0:
                continue
            header_block = part[:header_end]
            for raw_line in re.split(br"\r?\n", header_block):
                line = raw_line.decode("latin-1", errors="ignore")
                if not line.lower().startswith("content-disposition:"):
                    continue
                name_match = re.search(
                    r";\s*name\s*=\s*(?:\"([^\"]*)\"|([^;\s]+))",
                    line,
                    flags=re.IGNORECASE,
                )
                if name_match is not None:
                    name = name_match.group(1) or name_match.group(2)
                    if name:
                        names.add(name)
        return names

    def _form_identity_keys(body: bytes, content_type: str) -> set[str]:
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type == "application/x-www-form-urlencoded":
            decoded = body.decode("utf-8", errors="replace")
            return {
                key
                for key in parse_qs(decoded, keep_blank_values=True)
                if _has_reserved_identity_key(key)
            }
        if media_type == "multipart/form-data":
            return {
                key
                for key in _multipart_field_names(body, content_type)
                if _has_reserved_identity_key(key)
            }
        return set()

    @app.middleware("http")
    async def auth_boundary(request: Request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        if any(path == prefix or path.startswith(f"{prefix}/") for prefix in _closed_prefixes):
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        if not path.startswith("/api/"):
            return await call_next(request)

        # Read the small body once to reject identity injection before Pydantic
        # or any business side effect runs. JSON is decoded recursively; form
        # names are inspected without touching their values. Unsupported body
        # media types are rejected after the required auth check.
        # Starlette's
        # BaseHTTPMiddleware wraps this Request in CachedRequest; its body
        # cache is replayed to downstream handlers while preserving the
        # original disconnect signal.  Do not replace ``request._receive``:
        # doing so turns the second receive into a duplicate http.request and
        # breaks streaming/SSE handlers that check for disconnects.
        body = await request.body()
        keys: set[str] = set()
        keys.update(key for key in request.query_params if _has_reserved_identity_key(key))
        for header_name in request.headers:
            normalized_header = header_name.lower()
            if _has_reserved_identity_key(normalized_header):
                return JSONResponse(
                    status_code=422,
                    content={"detail": "请求不能注入身份字段"},
                )
        raw_content_type = request.headers.get("content-type", "")
        content_type = raw_content_type.lower()
        media_type = content_type.split(";", 1)[0].strip()
        is_json = media_type == "application/json" or media_type.endswith("+json")
        is_form = media_type in {
            "application/x-www-form-urlencoded",
            "multipart/form-data",
        }
        if body and is_json:
            try:
                decoded = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                decoded = None
            if decoded is not None:
                keys.update(
                    _find_reserved_identity_keys(
                        decoded,
                        allow_login_account=path == "/api/auth/login",
                    )
                )
        elif body and is_form:
            keys.update(_form_identity_keys(body, raw_content_type))
        if keys:
            return JSONResponse(
                status_code=422,
                content={"detail": "请求不能注入身份字段"},
            )

        if path in _public_paths and path != "/api/auth/login":
            return await call_next(request)

        # Login is the only API path that is not session-bound; it still must
        # carry the exact configured Origin for non-safe methods.
        if path == "/api/auth/login":
            if request.method not in {"GET", "HEAD"} and request.headers.get(
                "origin"
            ) != active_settings.web_origin:
                return JSONResponse(status_code=403, content={"detail": "Origin 不被允许"})
            if body and not is_json:
                return JSONResponse(
                    status_code=422,
                    content={"detail": "请求体必须使用 JSON"},
                )
            return await call_next(request)

        # Login is the only API path that is not session-bound; it still went
        # through the identity scanner above. Every other API path is
        # session-bound.
        is_write = request.method not in {"GET", "HEAD", "OPTIONS"}

        try:
            context = load_auth_context(
                active_session_factory,
                token=request.cookies.get(active_settings.auth_cookie_name),
            )
        except InvalidAuthSessionError:
            return JSONResponse(status_code=401, content={"detail": "登录会话无效或已过期"})
        request.state.auth_context = context
        if is_write:
            if request.headers.get("origin") != active_settings.web_origin:
                return JSONResponse(status_code=403, content={"detail": "Origin 不被允许"})
            try:
                verify_csrf(
                    active_session_factory,
                    context=context,
                    token=request.headers.get("X-CSRF-Token"),
                )
            except (CsrfMismatchError, InvalidAuthSessionError):
                return JSONResponse(status_code=403, content={"detail": "CSRF 校验失败"})
        if body and not is_json:
            return JSONResponse(
                status_code=422,
                content={"detail": "请求体必须使用 JSON"},
            )
        return await call_next(request)

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
        """Resolve Workspace directly from AuthSession, never from a locator cookie."""

        del response
        context: AuthContext | None = getattr(request.state, "auth_context", None)
        if context is None:
            raise HTTPException(status_code=401, detail="登录会话无效或已过期")
        try:
            workspace = workspace_service.get(
                context.token,
                auth_session_id=str(context.session_id),
            )
        except UnknownWorkspaceError as error:
            raise HTTPException(status_code=401, detail="登录会话无效或已过期") from error
        if workspace.workspace_id is None or str(workspace.workspace_id) != str(
            context.workspace_id
        ):
            raise HTTPException(status_code=401, detail="登录会话绑定无效")
        # This is trusted request state, not a client body/header value.  It
        # lets T18 Cursor CAS bind each active cursor to the current Session.
        workspace.auth_session_id = str(context.session_id)
        workspace.actor_id = context.principal.employee_id
        return workspace

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

    def _set_auth_cookies(response: Response, *, token: str, csrf_token: str) -> None:
        response.set_cookie(
            key=active_settings.auth_cookie_name,
            value=token,
            httponly=True,
            secure=active_settings.auth_cookie_secure,
            samesite="lax",
            max_age=active_settings.auth_session_ttl_seconds,
            path="/",
        )
        # The CSRF value is intentionally readable by the browser.  The
        # server stores only its hash and still requires the explicit header
        # on every write.
        response.set_cookie(
            key=active_settings.csrf_cookie_name,
            value=csrf_token,
            httponly=False,
            secure=active_settings.auth_cookie_secure,
            samesite="lax",
            max_age=active_settings.auth_session_ttl_seconds,
            path="/",
        )

    def _clear_auth_cookies(response: Response) -> None:
        response.delete_cookie(active_settings.auth_cookie_name, path="/")
        response.delete_cookie(active_settings.csrf_cookie_name, path="/")

    @app.post("/api/auth/login")
    def login(body: LoginBody, response: Response) -> dict[str, object]:
        """Select one fixed fictional account and atomically create a Session."""

        try:
            context, token, csrf_token = create_login_session(
                active_session_factory,
                account_id=body.account_id,
                ttl_seconds=active_settings.auth_session_ttl_seconds,
            )
        except LoginAccountError as error:
            raise HTTPException(status_code=422, detail="不支持该 Mock 账号") from error
        _set_auth_cookies(response, token=token, csrf_token=csrf_token)
        return {
            "csrf_token": csrf_token,
            "principal": context.principal.as_payload(),
            "expires_at": context.expires_at.isoformat(),
        }

    @app.get("/api/auth/session")
    def read_auth_session(request: Request, response: Response) -> dict[str, object]:
        """Refresh the Principal and rotate CSRF after a page reload."""

        context: AuthContext | None = getattr(request.state, "auth_context", None)
        if context is None:
            raise HTTPException(status_code=401, detail="登录会话无效或已过期")
        try:
            csrf_token = rotate_csrf(active_session_factory, context=context)
        except InvalidAuthSessionError as error:
            raise HTTPException(status_code=401, detail="登录会话无效或已过期") from error
        response.set_cookie(
            key=active_settings.csrf_cookie_name,
            value=csrf_token,
            httponly=False,
            secure=active_settings.auth_cookie_secure,
            samesite="lax",
            max_age=active_settings.auth_session_ttl_seconds,
            path="/",
        )
        return {
            "csrf_token": csrf_token,
            "principal": context.principal.as_payload(),
            "expires_at": context.expires_at.isoformat(),
        }

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response) -> dict[str, str]:
        """Revoke the current Session without deleting its Workspace."""

        context: AuthContext | None = getattr(request.state, "auth_context", None)
        if context is None:
            raise HTTPException(status_code=401, detail="登录会话无效或已过期")
        try:
            revoke_session(active_session_factory, context=context)
        except InvalidAuthSessionError as error:
            raise HTTPException(status_code=401, detail="登录会话无效或已过期") from error
        _clear_auth_cookies(response)
        return {"status": "logged_out"}

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

    @app.get("/api/access-overview")
    def read_access_overview(
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> dict[str, object]:
        """读取当前员工的权限生命周期卡片事实。"""

        try:
            with active_session_factory() as session:
                overview = get_access_overview(
                    session,
                    workspace_token=workspace.token,
                )
        except AccessOverviewNotFoundError as error:
            raise HTTPException(status_code=404, detail="权限事实不存在") from error
        except Exception as error:
            # 不向浏览器回显 SQL、连接串或其他内部异常。
            raise HTTPException(
                status_code=503,
                detail="权限事实暂时不可用，请稍后重试",
            ) from error
        return overview.model_dump(mode="json")

    @app.post("/api/entitlements/resolve")
    def resolve_entitlement(
        body: EntitlementResolveBody,
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> dict[str, object]:
        """在当前员工可申请目录内解析权限名称，不修改草稿。"""

        try:
            with active_session_factory() as session:
                result = execute_read_only_tool(
                    session,
                    workspace_token=workspace.token,
                    call=ReadOnlyToolCall(
                        tool="resolve_entitlement",
                        query=body.query,
                    ),
                )
        except Exception as error:
            # 解析是只读能力；数据库或执行器故障只能安全映射为重试。
            raise HTTPException(
                status_code=503,
                detail="权限目录暂时不可用，请稍后重试",
            ) from error
        if result.status == "workspace_not_found":
            raise HTTPException(status_code=404, detail="Workspace not found")
        if result.status == "employee_not_found":
            raise HTTPException(status_code=404, detail="当前员工不存在")
        if result.entitlement_resolution is None:
            raise HTTPException(
                status_code=503,
                detail="权限目录暂时不可用，请稍后重试",
            )
        return result.entitlement_resolution.model_dump(mode="json")

    @app.get("/api/events")
    async def replay_events(
        request: Request,
        follow: bool = True,
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> StreamingResponse:
        """回放安全事件；默认持续跟随，follow=false 只返回有限历史。"""

        initial_context: AuthContext | None = getattr(
            request.state,
            "auth_context",
            None,
        )
        if initial_context is None:
            raise HTTPException(status_code=401, detail="登录会话无效或已过期")

        def stream_session_is_active() -> bool:
            """Revalidate a long-lived stream instead of trusting its handshake."""

            try:
                current = load_auth_context(
                    active_session_factory,
                    token=initial_context.token,
                )
            except InvalidAuthSessionError:
                return False
            return (
                current.session_id == initial_context.session_id
                and current.workspace_id == initial_context.workspace_id
                and current.principal.employee_id
                == initial_context.principal.employee_id
            )

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
                if not stream_session_is_active():
                    return
                with active_session_factory() as session:
                    events = list_workspace_events(
                        session,
                        workspace_token=workspace.token,
                        after_id=cursor,
                    )
                if events:
                    for event in events:
                        if not stream_session_is_active():
                            return
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

    @app.get("/api/demo/model-quota", include_in_schema=False)
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
            auth_session_id = workspace.auth_session_id
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
                    auth_session_id=auth_session_id,
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
                outcome = normalized_outcome(prepared)
                event = persist_terminal(
                    "error.recoverable",
                    {
                        "turn_id": turn_id,
                        "code": "BUSINESS_VALIDATION_FAILED",
                        "message": message,
                        "intent": outcome["intent"],
                        "business_status": outcome["business_status"],
                        "draft_revision": outcome["draft_revision"],
                        "draft": outcome["draft"],
                        "assistant_message": message,
                        "error_code": outcome.get(
                            "error_code", "BUSINESS_VALIDATION_FAILED"
                        ),
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
            outcome = normalized_outcome(prepared)
            outcome["assistant_message"] = content
            event = persist_terminal(
                "message.completed",
                {
                    "turn_id": turn_id,
                    "message_id": str(uuid4()),
                    "content": content,
                    **outcome,
                },
            )
            if event is None:
                return
            apply_cursor_transition(
                workspace_service,
                workspace_token=workspace.token,
                turn=prepared,
                auth_session_id=auth_session_id,
            )
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
                auth_session_id=workspace.auth_session_id,
            )
        except ModelQuotaExceededError as error:
            raise HTTPException(
                status_code=429,
                detail="模型调用额度已用尽，当前为只读回放模式",
            ) from error
        except ConversationInputError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return turn.model_dump(mode="json", exclude={"quota"})

    @app.post("/api/workspaces", include_in_schema=False)
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

    @app.post("/api/workspaces/ensure", include_in_schema=False)
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
    @app.post("/api/demo/reset", include_in_schema=False)
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

    @app.get("/api/workspaces/identity", include_in_schema=False)
    def read_workspace_identity(
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> dict[str, object]:
        """读取当前 Workspace 的后端演示身份。"""

        return identity_payload(workspace)

    @app.post("/api/demo/session", include_in_schema=False)
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
            # A replacement Demo Workspace must not leave a product Cursor that
            # can be replayed after the product backup is restored.
            _set_product_backup_cookie(response, workspace.token)
            replacement = workspace_service.create()
            updated = workspace_service.enter_demo(replacement.token, body.employee_id)
        _set_workspace_cookie(response, updated.token)
        return {**identity_payload(updated), **demo_session_payload(updated)}

    @app.get("/api/demo/session", include_in_schema=False)
    def read_demo_session(
        workspace: Workspace = Depends(require_demo_feature),  # noqa: B008
    ) -> dict[str, object]:
        return demo_session_payload(workspace)

    @app.post("/api/demo/session/exit", include_in_schema=False)
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

    @app.post("/api/demo/fault-mode", include_in_schema=False)
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
        draft: DraftPreviewBody,
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

        # employee_id is a Principal-bound fact; it is intentionally absent
        # from DraftPreviewBody so clients cannot even submit that field.
        bound_draft = RequestDraft(
            employee_id=workspace.actor_id,
            entitlement_id=draft.entitlement_id,
            duration_days=draft.duration_days,
            justification=draft.justification,
            confirmed=draft.confirmed,
        )
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

        workspace_service.save_draft(
            workspace.token,
            bound_draft,
            auth_session_id=workspace.auth_session_id,
        )
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

        if workspace.auth_session_id is None:
            raise HTTPException(status_code=401, detail="登录会话无效或已过期")
        workspace_service.clear_cursor(
            workspace.token,
            auth_session_id=workspace.auth_session_id,
        )
        return {
            "request_id": str(request.id),
            "request_status": request.request_status,
        }

    @app.post("/api/requests/{request_id}/decision-packet", status_code=201)
    async def create_decision_packet(
        request: Request,
        response: Response,
        request_id: UUID,
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> dict[str, object]:
        """为 requester 的已提交 Case 创建或返回唯一决策材料。"""

        if await request.body():
            raise HTTPException(status_code=422, detail="决策材料接口不接受业务输入")
        context: AuthContext | None = getattr(request.state, "auth_context", None)
        if context is None:
            raise HTTPException(status_code=401, detail="登录会话无效或已过期")
        with active_session_factory() as session:
            try:
                packet, created = generate_decision_packet(
                    session,
                    request_id=request_id,
                    actor_id=context.principal.employee_id,
                    embedding_model=embedding_model,
                    advisory_model=decision_advisory_model,
                )
            except DecisionPacketNotFoundError as error:
                raise HTTPException(status_code=404, detail="申请不存在") from error
            except DecisionPacketStateError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            except (DecisionPacketGenerationError, ApprovalRoutingError) as error:
                raise HTTPException(
                    status_code=503,
                    detail="决策材料暂时无法生成，请重试",
                ) from error
        del workspace
        response.status_code = 201 if created else 200
        return decision_packet_payload(packet)

    @app.post("/api/requests/{request_id}/approval-case", status_code=201)
    def create_approval_case(
        request: Request,
        request_id: UUID,
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> dict[str, object]:
        """只根据已冻结 Packet 和目录创建人工审批路线。"""

        context: AuthContext | None = getattr(request.state, "auth_context", None)
        if context is None:
            raise HTTPException(status_code=401, detail="登录会话无效或已过期")
        with active_session_factory() as session:
            try:
                require_approval_startable(
                    session,
                    request_id=request_id,
                    actor_id=context.principal.employee_id,
                )
                case = start_approval_case(
                    session,
                    request_id=request_id,
                    actor_id=context.principal.employee_id,
                )
            except ApprovalNotFoundError as error:
                raise HTTPException(status_code=404, detail="申请不存在") from error
            except ApprovalWorkspaceMismatchError as error:
                raise HTTPException(status_code=404, detail="申请不存在") from error
            except ApprovalAlreadyStartedError as error:
                raise HTTPException(status_code=409, detail="审批流已经创建") from error
            except DecisionPacketRequiredError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            except ApprovalRoutingError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
            del workspace
            return approval_payload(session, case)

    @app.get("/api/approval-inbox")
    def read_approval_inbox(
        request: Request,
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> dict[str, object]:
        """读取当前 Principal 真正轮到处理的审批步骤。"""

        context: AuthContext | None = getattr(request.state, "auth_context", None)
        if context is None:
            raise HTTPException(status_code=401, detail="登录会话无效或已过期")
        if not {"manager", "data_owner"}.intersection(context.principal.roles):
            raise HTTPException(status_code=403, detail="当前账号没有审批职责")

        with active_session_factory() as session:
            try:
                return list_approval_inbox(
                    session,
                    actor_id=workspace.actor_id,
                    roles=context.principal.roles,
                )
            except OperationsNotFoundError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error

    @app.get("/api/provisioning-tasks")
    def read_provisioning_tasks(
        request: Request,
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> dict[str, object]:
        """返回固定权限管理员可执行的已批准 Case 任务。"""

        context: AuthContext | None = getattr(request.state, "auth_context", None)
        if context is None:
            raise HTTPException(status_code=401, detail="登录会话无效或已过期")
        if (
            context.principal.employee_id != "EMP-004"
            or "permissions_admin" not in context.principal.roles
        ):
            raise HTTPException(status_code=403, detail="当前账号没有权限开通职责")
        with active_session_factory() as session:
            try:
                payload = list_provisioning_tasks(
                    session,
                    actor_id=context.principal.employee_id,
                    roles=context.principal.roles,
                )
            except OperationsNotFoundError as error:
                raise HTTPException(status_code=403, detail=str(error)) from error
        del workspace
        return payload

    @app.get("/api/requests/latest")
    def read_latest_request(
        request: Request,
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> dict[str, object]:
        """读取当前 Principal 最新可见正式申请。"""

        context: AuthContext | None = getattr(request.state, "auth_context", None)
        if context is None:
            raise HTTPException(status_code=401, detail="登录会话无效或已过期")

        try:
            with active_session_factory() as session:
                detail = get_latest_request_detail_for_principal(
                    session,
                    actor_id=workspace.actor_id,
                    roles=context.principal.roles,
                    requester_only=True,
                )
        except OperationsNotFoundError as error:
            raise HTTPException(status_code=404, detail="申请不存在") from error
        except Exception as error:
            # 不能把数据库异常或请求内部字段泄露给浏览器。
            raise HTTPException(
                status_code=503,
                detail="申请事实暂时不可用，请稍后重试",
            ) from error
        return {"request": detail}

    @app.get("/api/requests/mine")
    def read_my_requests(
        request: Request,
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> dict[str, object]:
        """List all formal Cases submitted by the current Principal."""

        context: AuthContext | None = getattr(request.state, "auth_context", None)
        if context is None:
            raise HTTPException(status_code=401, detail="登录会话无效或已过期")
        with active_session_factory() as session:
            items = list_requests_for_principal(
                session,
                actor_id=workspace.actor_id,
                roles=context.principal.roles,
                requester_only=True,
            )
        return {"items": items}

    @app.get("/api/requests/accessible")
    def read_accessible_requests(
        request: Request,
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> dict[str, object]:
        """List all formal Cases related to the current Principal."""

        context: AuthContext | None = getattr(request.state, "auth_context", None)
        if context is None:
            raise HTTPException(status_code=401, detail="登录会话无效或已过期")
        with active_session_factory() as session:
            items = list_requests_for_principal(
                session,
                actor_id=workspace.actor_id,
                roles=context.principal.roles,
            )
        return {"items": items}

    @app.get("/api/requests/{request_id}")
    def read_request_detail(
        request: Request,
        request_id: UUID,
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> dict[str, object]:
        """读取有资源关系的 Case、审批、开通与审计事实。"""

        context: AuthContext | None = getattr(request.state, "auth_context", None)
        if context is None:
            raise HTTPException(status_code=401, detail="登录会话无效或已过期")

        with active_session_factory() as session:
            try:
                return get_request_detail_for_principal(
                    session,
                    request_id=request_id,
                    actor_id=workspace.actor_id,
                    roles=context.principal.roles,
                )
            except OperationsNotFoundError as error:
                raise HTTPException(status_code=404, detail="申请不存在") from error

    @app.post("/api/approval-cases/{case_id}/decisions")
    def decide_approval_step(
        request: Request,
        case_id: UUID,
        body: ApprovalDecisionBody,
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> dict[str, object]:
        """由当前指定审批人批准或驳回一个审批步骤。"""

        context: AuthContext | None = getattr(request.state, "auth_context", None)
        if context is None:
            raise HTTPException(status_code=401, detail="登录会话无效或已过期")
        ApprovalDecisionBody.validate_rejection_reason(body.decision, body.comment)
        with active_session_factory() as session:
            try:
                case = decide_approval(
                    session,
                    case_id=case_id,
                    expected_step_id=body.approval_step_id,
                    actor_id=context.principal.employee_id,
                    roles=context.principal.roles,
                    decision=body.decision,
                    comment=body.comment,
                )
            except ApprovalNotFoundError as error:
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
            del workspace
            return approval_payload(session, case)

    @app.post("/api/requests/{request_id}/provision")
    async def provision_request(
        request: Request,
        request_id: UUID,
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> dict[str, object]:
        """由固定权限管理员使用服务端幂等键执行开通。"""

        if await request.body():
            raise HTTPException(status_code=422, detail="开通接口不接受业务输入")
        context: AuthContext | None = getattr(request.state, "auth_context", None)
        if context is None:
            raise HTTPException(status_code=401, detail="登录会话无效或已过期")
        with active_session_factory() as session:
            try:
                attempt = provision_access(
                    session,
                    request_id=request_id,
                    actor_id=context.principal.employee_id,
                    roles=context.principal.roles,
                    fault_mode=None,
                    iam=iam_provisioner,
                )
            except ProvisioningNotFoundError as error:
                raise HTTPException(status_code=404, detail="申请不存在") from error
            except ProvisioningForbiddenError as error:
                raise HTTPException(status_code=403, detail=str(error)) from error
            except ApprovalRequiredError as error:
                raise HTTPException(
                    status_code=409,
                    detail="人工审批尚未全部通过",
                ) from error
            except IdempotencyConflictError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            del workspace
            return provisioning_payload(session, attempt)

    @app.post("/api/requests/{request_id}/provision/recover")
    async def recover_request_provisioning(
        request: Request,
        request_id: UUID,
        workspace: Workspace = Depends(require_workspace),  # noqa: B008
    ) -> dict[str, object]:
        """查询原幂等操作并恢复未知开通结果。"""

        if await request.body():
            raise HTTPException(status_code=422, detail="开通恢复接口不接受业务输入")
        context: AuthContext | None = getattr(request.state, "auth_context", None)
        if context is None:
            raise HTTPException(status_code=401, detail="登录会话无效或已过期")
        with active_session_factory() as session:
            try:
                attempt = recover_provisioning(
                    session,
                    request_id=request_id,
                    actor_id=context.principal.employee_id,
                    roles=context.principal.roles,
                    iam=iam_provisioner,
                )
            except ProvisioningNotFoundError as error:
                raise HTTPException(status_code=404, detail="申请不存在") from error
            except ProvisioningForbiddenError as error:
                raise HTTPException(status_code=403, detail=str(error)) from error
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
            del workspace
            return provisioning_payload(session, attempt)

    return app


app = create_app()
