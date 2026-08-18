"""Typed v1.3 production graph with deterministic read-only execution."""

from __future__ import annotations

import re
from collections.abc import Callable, Hashable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Annotated, Any, Literal, NotRequired, TypedDict, cast
from uuid import UUID

import httpx
from langchain_core.runnables.config import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
)
from sqlalchemy import select

from accesspilot.agent.routing import (
    ConversationIntent,
    IntentRoute,
    IntentRouter,
    IntentRoutingFailed,
    classify_policy_question,
    route_with_validation,
)
from accesspilot.agent.safety import redact_sensitive_content
from accesspilot.agent.state import (
    BoundedMessage,
    BusinessStatus,
    ConversationPhase,
    DraftPatch,
    GraphPhase,
    GraphState,
    InputKind,
    MissingField,
    SafeRecoverableError,
    SafeToolStatus,
    TurnReference,
)
from accesspilot.agent.step_operations import (
    AgentStepContext,
    AgentStepOperationService,
    StepExecutionRejected,
    StepOperationConflict,
)
from accesspilot.agent.structured_reply import (
    CORRECTION_PROMPT,
    MalformedStructuredOutputError,
)
from accesspilot.auth import Principal
from accesspilot.conversation import (
    SECURITY_MESSAGE,
    _explicit_confirmation_from_text,
    compose_tool_answer,
    entitlement_resolution_message,
    is_obvious_question,
    is_request_collection_follow_up,
    is_safe_justification_cursor_reply,
    merge_request_candidate,
    missing_field_question,
    normalize_request_candidate,
)
from accesspilot.db.models import AgentPendingInputRecord, EntitlementRecord
from accesspilot.domain.models import ConversationCursor, ParsedReply, RequestDraft
from accesspilot.events import ModelQuota, get_model_quota
from accesspilot.tools.catalog import ToolResult, validate_access_request
from accesspilot.tools.executor import (
    ReadOnlyToolCall,
    execute_read_only_tool,
    tool_call_for_intent,
)
from accesspilot.tools.policies import PolicyAnswer, PolicyService
from accesspilot.workspaces import (
    CursorConflictError,
    DraftRevisionConflictError,
    UnknownWorkspaceError,
    Workspace,
    WorkspaceService,
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class GraphInput(_StrictFrozenModel):
    """Only server-owned run references and one already-safe message enter a run."""

    schema_version: Literal[1]
    flow_version: Literal[2]
    workspace_ref: UUID
    graph_run_id: UUID
    input_seq: Annotated[StrictInt, Field(ge=0, le=9_223_372_036_854_775_807)]
    input_turn_id: TurnReference
    safe_user_text: Annotated[StrictStr, Field(min_length=1, max_length=10_000)]
    input_kind: InputKind

    @field_validator("safe_user_text", mode="before")
    @classmethod
    def redact_user_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return redact_sensitive_content(value.strip())


class _GraphNodeOutput(_StrictFrozenModel):
    """The checkpoint-safe fields validated when the final node returns."""

    assistant_message: BoundedMessage
    missing_fields: Annotated[list[MissingField], Field(max_length=3)]
    phase: GraphPhase
    business_status: BusinessStatus
    intent: ConversationIntent
    security_flagged: StrictBool
    committed_draft_revision: Annotated[StrictInt, Field(ge=0)] | None
    recoverable_error: SafeRecoverableError | None


class GraphOutput(_StrictFrozenModel):
    """A strict ``ConversationTurn``-compatible, non-checkpointed public result."""

    assistant_message: BoundedMessage
    draft: RequestDraft
    missing_fields: list[str]
    phase: ConversationPhase
    business_status: BusinessStatus
    quota: ModelQuota
    intent: ConversationIntent
    security_flagged: StrictBool
    tool_results: list[ToolResult]
    draft_revision: Annotated[StrictInt, Field(ge=0)]
    error_code: str | None = None


def _noop_success_finalizer() -> None:
    """Default post-terminal hook for outcomes with no Cursor projection."""


@dataclass(frozen=True)
class ProductionGraphRunResult:
    """Prepared graph turn plus its post-terminal Cursor projection."""

    turn: GraphOutput
    success_finalizer: Callable[[], None] = field(
        default=_noop_success_finalizer,
        repr=False,
        compare=False,
    )

    def finalize_success(self) -> None:
        self.success_finalizer()


@dataclass
class _InvocationFacts:
    """Ephemeral authoritative/raw facts that must never enter GraphState."""

    workspace: Workspace | None = None
    draft: RequestDraft | None = None
    cursor: ConversationCursor | None = None
    quota: ModelQuota | None = None
    tool_call: ReadOnlyToolCall | None = None
    tool_result: ToolResult | None = None
    policy_answer: PolicyAnswer | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class _AuthoritativeSnapshot:
    workspace: Workspace
    draft: RequestDraft
    cursor: ConversationCursor | None
    quota: ModelQuota | None


class DeterministicGraphRoute(_StrictFrozenModel):
    """Pure route/branch decision used by production and shadow comparison."""

    intent: ConversationIntent
    security_flagged: StrictBool
    selected_route: Literal[
        "security",
        "help",
        "unknown",
        "numeric_cursor",
        "read_only",
        "policy",
        "request_access",
    ]


class GraphRuntimeContext(TypedDict):
    """Request-scoped capabilities and secrets; no field is a state channel."""

    session_factory: object
    workspace_service: object
    policy_service: object
    structured_reply_model: object
    intent_router: object
    principal: object
    current_turn_id: str
    current_fence: int
    workspace_token: str
    auth_session_id: str
    cookie: str
    csrf_token: str
    api_key: str
    pending_input_id: NotRequired[str]
    current_input_seq: NotRequired[int]
    _invocation_facts: NotRequired[_InvocationFacts]


PRODUCTION_NODE_NAMES = (
    "hydrate_authoritative_snapshot",
    "route_intent",
    "compose_safe_answer",
    "handle_numeric_followup",
    "select_read_tool",
    "execute_read_tool",
    "retrieve_policy_pgvector",
    "grade_policy_evidence",
    "compose_grounded_answer",
    "compose_insufficient_answer",
    "compose_recoverable_answer",
    "parse_request_patch",
    "resolve_entitlement",
    "merge_candidate",
    "persist_draft_cas",
    "validate_draft",
    "ask_missing_field",
    "await_requester_confirmation",
    "rehydrate_resume_snapshot",
    "apply_confirmation_cas",
    "ready_to_submit",
    "finalize_public_outcome",
)

PRODUCTION_CONDITIONAL_PATHS: dict[str, dict[Hashable, str]] = {
    "route_intent": {
        "security": "compose_safe_answer",
        "help": "compose_safe_answer",
        "unknown": "compose_safe_answer",
        "numeric_cursor": "handle_numeric_followup",
        "read_only": "select_read_tool",
        "policy": "retrieve_policy_pgvector",
        "request_access": "parse_request_patch",
    },
    "grade_policy_evidence": {
        "grounded": "compose_grounded_answer",
        "insufficient": "compose_insufficient_answer",
        "unavailable": "compose_recoverable_answer",
    },
    "request_needs_entitlement_resolution": {
        "yes": "resolve_entitlement",
        "no": "merge_candidate",
    },
    "validate_draft": {
        "missing": "ask_missing_field",
        "invalid_or_conflict": "compose_recoverable_answer",
        "complete": "await_requester_confirmation",
    },
    "rehydrate_resume_snapshot": {
        "confirm": "apply_confirmation_cas",
        "non_confirm_input": "route_intent",
    },
    "apply_confirmation_cas": {
        "success": "ready_to_submit",
        "conflict": "compose_recoverable_answer",
    },
}

REPRESENTATIVE_PATHS = {
    "safe": (
        START,
        "hydrate_authoritative_snapshot",
        "route_intent",
        "compose_safe_answer",
        "finalize_public_outcome",
        END,
    ),
    "numeric": (
        START,
        "hydrate_authoritative_snapshot",
        "route_intent",
        "handle_numeric_followup",
        "finalize_public_outcome",
        END,
    ),
    "read_only": (
        START,
        "hydrate_authoritative_snapshot",
        "route_intent",
        "select_read_tool",
        "execute_read_tool",
        "compose_safe_answer",
        "finalize_public_outcome",
        END,
    ),
    "policy_grounded": (
        START,
        "hydrate_authoritative_snapshot",
        "route_intent",
        "retrieve_policy_pgvector",
        "grade_policy_evidence",
        "compose_grounded_answer",
        "finalize_public_outcome",
        END,
    ),
    "policy_insufficient": (
        START,
        "hydrate_authoritative_snapshot",
        "route_intent",
        "retrieve_policy_pgvector",
        "grade_policy_evidence",
        "compose_insufficient_answer",
        "finalize_public_outcome",
        END,
    ),
    "policy_unavailable": (
        START,
        "hydrate_authoritative_snapshot",
        "route_intent",
        "retrieve_policy_pgvector",
        "grade_policy_evidence",
        "compose_recoverable_answer",
        "finalize_public_outcome",
        END,
    ),
    "request_missing": (
        START,
        "hydrate_authoritative_snapshot",
        "route_intent",
        "parse_request_patch",
        "resolve_entitlement",
        "merge_candidate",
        "persist_draft_cas",
        "validate_draft",
        "ask_missing_field",
        "finalize_public_outcome",
        END,
    ),
    "request_invalid": (
        START,
        "hydrate_authoritative_snapshot",
        "route_intent",
        "parse_request_patch",
        "merge_candidate",
        "persist_draft_cas",
        "validate_draft",
        "compose_recoverable_answer",
        "finalize_public_outcome",
        END,
    ),
    "request_confirm": (
        START,
        "hydrate_authoritative_snapshot",
        "route_intent",
        "parse_request_patch",
        "merge_candidate",
        "persist_draft_cas",
        "validate_draft",
        "await_requester_confirmation",
    ),
}


StateUpdate = Mapping[str, object] | GraphState
StateStub = Callable[[GraphState, Runtime[GraphRuntimeContext]], StateUpdate]
ValidatedStateNode = Callable[[GraphState, Runtime[GraphRuntimeContext]], GraphState]


class UnsafeGraphStateUpdateError(RuntimeError):
    """A node returned data outside the durable state contract."""


class ConfirmationInterruptRaised(RuntimeError):
    """The graph stopped at the confirmation interrupt.

    Carries the plain JSON interrupt value; the caller must verify the exact
    candidate checkpoint and persist pending/Cursor/terminal atomically before
    any resume may proceed.
    """

    def __init__(self, payload: dict[str, object]) -> None:
        super().__init__("graph stopped at the confirmation interrupt")
        self.payload = payload


class GraphRuntimeContractError(RuntimeError):
    """Runtime capabilities or authoritative bindings failed closed."""


class GraphWritePathDeferredError(RuntimeError):
    """A path requires a T32 application write and cannot run in T31."""


def _execution_step_context(
    runtime: Runtime[GraphRuntimeContext],
    *,
    workspace_ref: UUID,
    graph_run_id: UUID,
    input_seq: int,
    input_turn_id: str,
) -> AgentStepContext:
    """Validate runtime-owned execution coordinates shared by nodes and finalizers."""

    principal = _context_value(runtime, "principal")
    current_turn_id = _context_value(runtime, "current_turn_id")
    current_fence = _context_value(runtime, "current_fence")
    auth_session_id = _context_value(runtime, "auth_session_id")
    if (
        not isinstance(principal, Principal)
        or not isinstance(current_turn_id, str)
        or current_turn_id != input_turn_id
        or type(current_fence) is not int
        or current_fence <= 0
        or not isinstance(auth_session_id, str)
    ):
        raise GraphRuntimeContractError("graph execution binding failed")
    try:
        auth_session_ref = UUID(auth_session_id)
    except ValueError:
        raise GraphRuntimeContractError("graph execution binding failed") from None
    return AgentStepContext(
        workspace_id=workspace_ref,
        graph_run_id=graph_run_id,
        input_seq=input_seq,
        input_turn_id=input_turn_id,
        actor_id=principal.employee_id,
        auth_session_ref=auth_session_ref,
        lease_fence=current_fence,
    )


def _step_context(
    state: GraphState,
    runtime: Runtime[GraphRuntimeContext],
) -> AgentStepContext:
    return _execution_step_context(
        runtime,
        workspace_ref=state.workspace_ref,
        graph_run_id=state.graph_run_id,
        input_seq=state.input_seq,
        input_turn_id=state.input_turn_id,
    )


def _step_service(
    runtime: Runtime[GraphRuntimeContext],
) -> AgentStepOperationService:
    session_factory = _context_value(runtime, "session_factory")
    if not callable(session_factory):
        raise GraphRuntimeContractError("database service is unavailable")
    return AgentStepOperationService(cast(Any, session_factory))


def validate_state_update(state: GraphState, update: StateUpdate) -> GraphState:
    """Validate a complete post-node state before LangGraph can persist updates."""

    update_values = (
        update.model_dump(mode="python") if isinstance(update, GraphState) else dict(update)
    )
    try:
        return GraphState.model_validate({**state.model_dump(mode="python"), **update_values})
    except ValidationError:
        # LangGraph may persist task errors. Never let Pydantic's input_value
        # repr copy a rejected provider payload or credential into that error.
        raise UnsafeGraphStateUpdateError("graph node produced an invalid state update") from None


def validated_state_node(stub: StateStub) -> ValidatedStateNode:
    """Wrap every replaceable node with the reusable pre-checkpoint state gate."""

    def validated(
        state: GraphState,
        runtime: Runtime[GraphRuntimeContext],
    ) -> GraphState:
        return validate_state_update(state, stub(state, runtime))

    validated.__name__ = stub.__name__
    return validated


def _context_value(
    runtime: Runtime[GraphRuntimeContext],
    key: str,
) -> object:
    value = runtime.context.get(key)
    if value is None:
        raise GraphRuntimeContractError("graph runtime context is incomplete")
    return value


def _invocation_facts(
    runtime: Runtime[GraphRuntimeContext],
) -> _InvocationFacts:
    value = _context_value(runtime, "_invocation_facts")
    if not isinstance(value, _InvocationFacts):
        raise GraphRuntimeContractError("graph invocation context is invalid")
    return value


def _optional_invocation_facts(
    runtime: Runtime[GraphRuntimeContext],
) -> _InvocationFacts | None:
    value = runtime.context.get("_invocation_facts")
    return value if isinstance(value, _InvocationFacts) else None


def _authoritative_draft(workspace: Workspace, principal: Principal) -> RequestDraft:
    draft = workspace.draft
    if (
        draft is not None
        and draft.employee_id is not None
        and draft.employee_id != principal.employee_id
    ):
        draft = None
    return draft or RequestDraft(employee_id=principal.employee_id)


def _draft_patch(draft: RequestDraft) -> DraftPatch | None:
    values: dict[str, object] = {
        "entitlement_id": draft.entitlement_id,
        "duration_days": draft.duration_days,
        "justification": draft.justification,
    }
    return (
        DraftPatch.model_validate(values)
        if any(value is not None for value in values.values())
        else None
    )


def _read_authoritative_snapshot(
    workspace_ref: UUID,
    runtime: Runtime[GraphRuntimeContext],
    *,
    include_quota: bool,
) -> _AuthoritativeSnapshot:
    """Read bound business facts without normalizing or persisting Workspace state."""

    workspace_service = _context_value(runtime, "workspace_service")
    session_factory = _context_value(runtime, "session_factory")
    principal = _context_value(runtime, "principal")
    workspace_token = _context_value(runtime, "workspace_token")
    auth_session_id = _context_value(runtime, "auth_session_id")
    if not isinstance(workspace_service, WorkspaceService):
        raise GraphRuntimeContractError("workspace service is unavailable")
    if not callable(session_factory):
        raise GraphRuntimeContractError("database service is unavailable")
    if not isinstance(principal, Principal):
        raise GraphRuntimeContractError("principal is unavailable")
    if not isinstance(workspace_token, str) or not isinstance(auth_session_id, str):
        raise GraphRuntimeContractError("workspace binding is unavailable")

    try:
        raw_workspace = workspace_service.peek(workspace_token)
    except UnknownWorkspaceError:
        raise GraphRuntimeContractError("authoritative workspace binding failed") from None
    if (
        raw_workspace.workspace_id is None
        or str(raw_workspace.workspace_id) != str(workspace_ref)
        or raw_workspace.actor_id != principal.employee_id
        or (
            raw_workspace.cursor is not None
            and raw_workspace.cursor.auth_session_id != auth_session_id
        )
    ):
        raise GraphRuntimeContractError("authoritative workspace binding failed")

    # Bind the request-scoped session only on this detached domain snapshot.
    # WorkspaceService.get() may normalize and save an invalid cursor, which is
    # forbidden for every T31 route, including authentication failures.
    workspace = replace(raw_workspace, auth_session_id=auth_session_id)
    draft = _authoritative_draft(workspace, principal)
    quota = None
    if include_quota:
        with session_factory() as session:
            quota = get_model_quota(session, workspace_token=workspace_token)
    return _AuthoritativeSnapshot(
        workspace=workspace,
        draft=draft,
        cursor=workspace.active_cursor(),
        quota=quota,
    )


def _hydrate_authoritative_snapshot(
    graph_input: GraphInput,
    runtime: Runtime[GraphRuntimeContext],
) -> GraphState:
    """Re-read Principal, Workspace, draft, Cursor, revision, and quota."""

    snapshot = _read_authoritative_snapshot(
        graph_input.workspace_ref,
        runtime,
        include_quota=True,
    )
    workspace = snapshot.workspace
    draft = snapshot.draft
    quota = snapshot.quota
    if quota is None:
        raise GraphRuntimeContractError("authoritative quota is unavailable")

    invocation = _invocation_facts(runtime)
    invocation.workspace = workspace
    invocation.draft = draft
    invocation.cursor = snapshot.cursor
    invocation.quota = quota
    return GraphState(
        **graph_input.model_dump(mode="python"),
        intent=None,
        security_flagged=False,
        selected_route="unknown",
        base_draft_revision=workspace.draft_revision,
        committed_draft_revision=None,
        # Candidate state belongs only to this logical input. The authoritative
        # prior draft remains runtime/DB data and is re-read by write nodes.
        draft_patch=None,
        missing_fields=cast(list[MissingField], draft.missing_fields()),
        phase="routing",
        tool_name=None,
        safe_tool_result=None,
        policy_status=None,
        policy_evidence_codes=[],
        policy_match_count=0,
        business_status="pending",
        assistant_message=None,
        recoverable_error=None,
        pending_input_id=None,
        pending_input_kind=None,
    )


def _passthrough_stub(
    state: GraphState,
    runtime: Runtime[GraphRuntimeContext],
) -> StateUpdate:
    del state, runtime
    return {}


_NUMERIC_INPUT_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")
_VALID_DURATION_RE = re.compile(r"[1-9][0-9]{0,3}")


def _is_numeric_input(content: str) -> bool:
    return _NUMERIC_INPUT_RE.fullmatch(content) is not None


def route_graph_input(
    content: str,
    *,
    router: IntentRouter,
    numeric_cursor_active: bool,
) -> DeterministicGraphRoute:
    """Select one graph branch without services, tools, models, or writes."""

    try:
        route = route_with_validation(content, router)
    except IntentRoutingFailed:
        raise GraphRuntimeContractError("intent routing failed") from None
    if _is_numeric_input(content):
        return DeterministicGraphRoute(
            intent="request_access" if numeric_cursor_active else "unknown",
            security_flagged=route.security_probe,
            selected_route="numeric_cursor",
        )
    selected_route: Literal[
        "security",
        "help",
        "unknown",
        "numeric_cursor",
        "read_only",
        "policy",
        "request_access",
    ]
    if route.intent == "security_probe":
        selected_route = "security"
    elif route.intent in {"help", "unknown"}:
        selected_route = "help" if route.intent == "help" else "unknown"
    elif route.intent == "policy_question":
        selected_route = "policy" if classify_policy_question(content) == "search" else "read_only"
    elif route.intent in {
        "discover_eligible_access",
        "list_active_access",
        "request_status",
    }:
        selected_route = "read_only"
    else:
        selected_route = "request_access"
    return DeterministicGraphRoute(
        intent=route.intent,
        security_flagged=route.security_probe,
        selected_route=selected_route,
    )


def _route_intent(
    state: GraphState,
    runtime: Runtime[GraphRuntimeContext],
) -> StateUpdate:
    router = _context_value(runtime, "intent_router")
    if not hasattr(router, "route"):
        raise GraphRuntimeContractError("intent router is unavailable")
    snapshot = _read_authoritative_snapshot(
        state.workspace_ref,
        runtime,
        include_quota=False,
    )
    decision = route_graph_input(
        state.safe_user_text,
        router=cast(IntentRouter, router),
        numeric_cursor_active=snapshot.cursor is not None,
    )
    cursor = snapshot.cursor
    if (
        cursor is not None
        and cursor.expected_field == "justification"
        and decision.intent in {"help", "request_access"}
        and is_obvious_question(state.safe_user_text)
    ):
        decision = DeterministicGraphRoute(
            intent="help",
            security_flagged=decision.security_flagged,
            selected_route="help",
        )
    elif decision.intent == "help" and is_request_collection_follow_up(
        state.safe_user_text,
        snapshot.draft.missing_fields(),
    ):
        decision = DeterministicGraphRoute(
            intent="request_access",
            security_flagged=decision.security_flagged,
            selected_route="request_access",
        )
    return {
        "intent": decision.intent,
        "security_flagged": decision.security_flagged,
        "selected_route": decision.selected_route,
        "tool_name": None,
        "safe_tool_result": None,
        "policy_status": None,
        "policy_evidence_codes": [],
        "policy_match_count": 0,
        "assistant_message": None,
        "recoverable_error": None,
        "business_status": "pending",
        "phase": "routing",
    }


def _candidate_patch(parsed: ParsedReply) -> DraftPatch | None:
    values: dict[str, object] = {
        "entitlement_id": parsed.entitlement_id,
        "duration_days": parsed.duration_days,
        "justification": parsed.justification,
    }
    if not any(value is not None for value in values.values()):
        return None
    return DraftPatch.model_validate(values)


def _call_model_attempt(
    state: GraphState,
    runtime: Runtime[GraphRuntimeContext],
    *,
    attempt: Literal[1, 2],
) -> tuple[Literal["parsed", "malformed", "unavailable"], ParsedReply | None]:
    model = _context_value(runtime, "structured_reply_model")
    if not hasattr(model, "parse_reply"):
        raise GraphRuntimeContractError("structured reply model is unavailable")
    workspace_token = _context_value(runtime, "workspace_token")
    if not isinstance(workspace_token, str):
        raise GraphRuntimeContractError("workspace binding is unavailable")
    service = _step_service(runtime)
    context = _step_context(state, runtime)
    reservation = service.reserve_model_attempt(
        context,
        workspace_token=workspace_token,
        attempt=attempt,
    )
    if reservation.status == "reserved":
        service.complete_model_attempt(
            context,
            workspace_token=workspace_token,
            attempt=attempt,
        )
    correction = CORRECTION_PROMPT if attempt == 2 else None
    try:
        raw_parsed = model.parse_reply(
            state.safe_user_text,
            correction=correction,
        )
        parsed = ParsedReply.model_validate(raw_parsed)
    except (MalformedStructuredOutputError, ValidationError):
        return "malformed", None
    except (httpx.HTTPError, TimeoutError):
        return "unavailable", None
    principal = _context_value(runtime, "principal")
    if not isinstance(principal, Principal):
        raise GraphRuntimeContractError("principal is unavailable")
    parsed = normalize_request_candidate(
        parsed,
        actor_id=principal.employee_id,
        content=state.safe_user_text,
        security_probe=state.security_flagged,
    )
    # Confirmation is a T34 input decision, never a T32 model candidate.
    parsed = parsed.model_copy(update={"confirmed": None})
    return "parsed", parsed


def _model_unavailable_update(state: GraphState) -> StateUpdate:
    message = "我暂时没能可靠理解这条消息，请稍后重试或换一种说法。"
    if state.security_flagged:
        message = f"{SECURITY_MESSAGE}\n\n{message}"
    return {
        "draft_patch": None,
        "assistant_message": message,
        "business_status": "recoverable_error",
        "phase": "recoverable_error",
        "recoverable_error": {
            "code": "MODEL_REPLY_UNAVAILABLE",
            "message": message,
        },
    }


def _parse_request_patch(
    state: GraphState,
    runtime: Runtime[GraphRuntimeContext],
) -> StateUpdate:
    workspace_token = _context_value(runtime, "workspace_token")
    if not isinstance(workspace_token, str):
        raise GraphRuntimeContractError("workspace binding is unavailable")
    context = _step_context(state, runtime)
    service = _step_service(runtime)
    # A replay that already committed its authoritative draft must not spend
    # model quota or depend on an at-least-once provider call to reconstruct it.
    for step_key in ("persist_draft_cas", "persist_justification_cursor"):
        if (
            service.completed_step(
                context,
                workspace_token=workspace_token,
                step_key=step_key,
            )
            is not None
        ):
            return {"draft_patch": None}
    snapshot = _read_authoritative_snapshot(
        state.workspace_ref,
        runtime,
        include_quota=False,
    )
    route = IntentRoute(
        intent="request_access",
        security_probe=state.security_flagged,
    )
    if (
        snapshot.cursor is not None
        and snapshot.cursor.expected_field == "justification"
        and is_safe_justification_cursor_reply(state.safe_user_text, route)
    ):
        cursor_parsed = ParsedReply(
            employee_id=snapshot.workspace.actor_id,
            justification=state.safe_user_text,
            confirmed=None,
        )
        return {"draft_patch": _candidate_patch(cursor_parsed)}

    primary_status, candidate = _call_model_attempt(
        state,
        runtime,
        attempt=1,
    )
    if primary_status == "unavailable":
        return _model_unavailable_update(state)
    if primary_status == "malformed":
        retry_status, candidate = _call_model_attempt(
            state,
            runtime,
            attempt=2,
        )
        if retry_status != "parsed":
            return _model_unavailable_update(state)
    if candidate is None:
        raise GraphRuntimeContractError("parsed request candidate is unavailable")
    if candidate.entitlement_id is not None and not candidate.entitlement_id.strip():
        message = "当前无法可靠解析权限，请检查演示身份后重试。"
        if state.security_flagged:
            message = f"{SECURITY_MESSAGE}\n\n{message}"
        return {
            "draft_patch": None,
            "assistant_message": message,
            "business_status": "resolution_unavailable",
            "phase": ("collecting" if snapshot.draft.missing_fields() else "awaiting_confirmation"),
        }
    return {"draft_patch": _candidate_patch(candidate)}


def _select_read_tool(
    state: GraphState,
    runtime: Runtime[GraphRuntimeContext],
) -> StateUpdate:
    if state.intent is None:
        raise GraphRuntimeContractError("read-only route has no intent")
    call = tool_call_for_intent(state.intent, content=state.safe_user_text)
    if call is None or call.tool == "search_policies":
        raise GraphRuntimeContractError("read-only tool selection failed")
    return {"tool_name": call.tool}


def _safe_tool_result(
    call: ReadOnlyToolCall,
    result: ToolResult,
    *,
    summary: str,
) -> dict[str, object]:
    entitlement_codes: list[str] = []
    if result.eligible_access is not None:
        entitlement_codes.extend(item.code for item in result.eligible_access)
    if result.active_access is not None:
        entitlement_codes.extend(item.code for item in result.active_access)
    if result.entitlement_resolution is not None:
        entitlement_codes.extend(item.code for item in result.entitlement_resolution.candidates)
        entitlement_codes.extend(
            item.code for item in result.entitlement_resolution.eligible_access
        )
    policy_codes: list[str] = []
    if result.policy_catalog is not None:
        policy_codes.extend(item.policy_code for item in result.policy_catalog)
    if result.policy_answer is not None:
        policy_codes.extend(item.policy_code for item in result.policy_answer.evidence)
    status = cast(SafeToolStatus, result.status)
    if result.entitlement_resolution is not None:
        status = cast(SafeToolStatus, result.entitlement_resolution.status)
    elif result.policy_answer is not None:
        status = cast(
            SafeToolStatus,
            {
                "grounded": "grounded",
                "insufficient_evidence": "insufficient",
                "retrieval_unavailable": "unavailable",
            }[result.policy_answer.status],
        )
    unique_entitlements = list(dict.fromkeys(entitlement_codes))[:20]
    unique_policies = list(dict.fromkeys(policy_codes))[:8]
    return {
        "tool": call.tool,
        "status": status,
        "entitlement_codes": unique_entitlements,
        "policy_codes": unique_policies,
        "match_count": max(len(unique_entitlements), len(unique_policies)),
        "summary": summary,
    }


def _execute_read_tool(
    state: GraphState,
    runtime: Runtime[GraphRuntimeContext],
) -> StateUpdate:
    if state.intent is None or state.tool_name is None:
        raise GraphRuntimeContractError("read-only tool call is unavailable")
    call = tool_call_for_intent(state.intent, content=state.safe_user_text)
    if call is None or call.tool == "search_policies" or call.tool != state.tool_name:
        raise GraphRuntimeContractError("read-only tool selection is invalid")
    session_factory = _context_value(runtime, "session_factory")
    workspace_token = _context_value(runtime, "workspace_token")
    policy_service = _context_value(runtime, "policy_service")
    if not callable(session_factory) or not isinstance(workspace_token, str):
        raise GraphRuntimeContractError("read-only services are unavailable")
    if not isinstance(policy_service, PolicyService):
        raise GraphRuntimeContractError("policy service is unavailable")
    with session_factory() as session:
        result = execute_read_only_tool(
            session,
            workspace_token=workspace_token,
            call=call,
            policy_service=policy_service,
        )
    invocation = _optional_invocation_facts(runtime)
    if invocation is not None:
        invocation.tool_call = call
        invocation.tool_result = result
    summary = compose_tool_answer(
        IntentRoute(intent=state.intent, security_probe=state.security_flagged),
        result,
    )
    return {
        "safe_tool_result": _safe_tool_result(
            call,
            result,
            summary=summary,
        )
    }


def _compose_safe_answer(
    state: GraphState,
    runtime: Runtime[GraphRuntimeContext],
) -> StateUpdate:
    del runtime
    if state.intent is None:
        raise GraphRuntimeContractError("safe answer has no intent")
    route = IntentRoute(
        intent=state.intent,
        security_probe=state.security_flagged,
    )
    if state.tool_name is not None:
        if state.safe_tool_result is None:
            raise GraphRuntimeContractError("safe tool answer is unavailable")
        message = state.safe_tool_result.summary
    else:
        message = compose_tool_answer(route, None)
    if state.security_flagged and state.intent != "security_probe":
        message = f"{SECURITY_MESSAGE}\n\n{message}"
    business_status = "needs_clarification" if state.intent == "unknown" else "answered"
    recoverable_error = None
    if state.intent == "unknown":
        recoverable_error = {
            "code": "NUMERIC_CONTEXT_REQUIRED",
            "message": message,
        }
    return {
        "assistant_message": message,
        "business_status": business_status,
        "phase": "answered",
        "recoverable_error": recoverable_error,
    }


def _numeric_error(
    runtime: Runtime[GraphRuntimeContext],
    *,
    intent: ConversationIntent,
    message: str,
    business_status: BusinessStatus,
    phase: GraphPhase,
    error_code: str,
) -> StateUpdate:
    del runtime
    return {
        "intent": intent,
        "assistant_message": message,
        "business_status": business_status,
        "phase": phase,
        "recoverable_error": {"code": error_code, "message": message},
    }


def _numeric_success_update(
    state: GraphState,
    runtime: Runtime[GraphRuntimeContext],
) -> StateUpdate:
    """Compose a numeric-CAS outcome only from the re-read authoritative draft."""

    snapshot = _read_authoritative_snapshot(
        state.workspace_ref,
        runtime,
        include_quota=False,
    )
    missing = cast(list[MissingField], snapshot.draft.missing_fields())
    if missing:
        message = missing_field_question(missing[0])
        business_status: BusinessStatus = "collecting"
        phase: GraphPhase = "collecting"
        recoverable_error = None
    else:
        session_factory = _context_value(runtime, "session_factory")
        if not callable(session_factory):
            raise GraphRuntimeContractError("database service is unavailable")
        with session_factory() as session:
            validation = validate_access_request(session, snapshot.draft)
        if validation.status != "success":
            message = "申请未通过目录校验，请检查员工、权限或期限。"
            business_status = "validation_failed"
            # Preserve the existing deterministic numeric outcome contract.
            phase = "awaiting_confirmation"
            recoverable_error = {
                "code": "BUSINESS_VALIDATION_FAILED",
                "message": message,
            }
        else:
            message = "申请信息已完整。请明确回复“确认提交”后再创建正式申请。"
            business_status = "awaiting_confirmation"
            phase = "awaiting_confirmation"
            recoverable_error = None
    if state.security_flagged:
        message = f"{SECURITY_MESSAGE}\n\n{message}"
    return {
        "intent": "request_access",
        "committed_draft_revision": snapshot.workspace.draft_revision,
        "missing_fields": missing,
        "assistant_message": message,
        "business_status": business_status,
        "phase": phase,
        "recoverable_error": recoverable_error,
    }


def _handle_numeric_followup(
    state: GraphState,
    runtime: Runtime[GraphRuntimeContext],
) -> StateUpdate:
    workspace_token = _context_value(runtime, "workspace_token")
    if not isinstance(workspace_token, str):
        raise GraphRuntimeContractError("workspace binding is unavailable")
    service = _step_service(runtime)
    context: AgentStepContext | None = None
    if _VALID_DURATION_RE.fullmatch(state.safe_user_text) is not None:
        try:
            context = _step_context(state, runtime)
        except GraphRuntimeContractError:
            # T31's non-writing numeric compatibility paths intentionally have
            # no execution binding. A legal T32 write still requires it below.
            context = None
        if context is not None:
            completed = service.completed_step(
                context,
                workspace_token=workspace_token,
                step_key="persist_numeric_duration",
            )
            if completed is not None:
                return _numeric_success_update(state, runtime)
    snapshot = _read_authoritative_snapshot(
        state.workspace_ref,
        runtime,
        include_quota=False,
    )
    cursor = snapshot.cursor
    draft = snapshot.draft
    if cursor is None:
        return _numeric_error(
            runtime,
            intent="unknown",
            message=("我需要更多上下文才能理解“111”：它是期限、权限编号，还是其他内容？"),
            business_status="needs_clarification",
            phase="collecting" if state.missing_fields else "awaiting_confirmation",
            error_code="NUMERIC_CONTEXT_REQUIRED",
        )
    if cursor.expected_field != "duration_days":
        questions = {
            "entitlement_id": "请提供权限名称或权限编号，例如 insighthub.customer_export。",
            "justification": "请说明申请这项权限的业务理由。",
            "confirmation": "请明确回复“确认提交”或继续修改申请信息。",
            "none": "请说明你要办理的权限业务。",
        }
        codes = {
            "entitlement_id": "ENTITLEMENT_REQUIRED",
            "justification": "JUSTIFICATION_REQUIRED",
            "confirmation": "CONFIRMATION_REQUIRED",
            "none": "NUMERIC_CONTEXT_REQUIRED",
        }
        return _numeric_error(
            runtime,
            intent="request_access",
            message=questions[cursor.expected_field],
            business_status=(
                "awaiting_confirmation" if cursor.expected_field == "confirmation" else "collecting"
            ),
            phase=(
                "awaiting_confirmation" if cursor.expected_field == "confirmation" else "collecting"
            ),
            error_code=codes[cursor.expected_field],
        )
    if _VALID_DURATION_RE.fullmatch(state.safe_user_text) is None:
        return _numeric_error(
            runtime,
            intent="request_access",
            message="申请期限必须是 1–9999 天的正整数，请重新输入期限。",
            business_status="collecting",
            phase="collecting",
            error_code="INVALID_DURATION_DAYS",
        )
    candidate = int(state.safe_user_text)
    maximum: int | None = None
    if draft.entitlement_id is not None:
        session_factory = _context_value(runtime, "session_factory")
        if not callable(session_factory):
            raise GraphRuntimeContractError("database service is unavailable")
        with session_factory() as session:
            entitlement = session.get(EntitlementRecord, draft.entitlement_id)
        maximum = entitlement.max_duration_days if entitlement is not None else None
    if maximum is not None and candidate > maximum:
        return _numeric_error(
            runtime,
            intent="request_access",
            message=(f"该权限最长只能申请 {maximum} 天，请重新输入不超过上限的期限。"),
            business_status="collecting",
            phase="collecting",
            error_code="DURATION_EXCEEDS_MAXIMUM",
        )
    proposed = draft.model_copy(update={"duration_days": candidate, "confirmed": False})
    if context is None:
        context = _step_context(state, runtime)
    try:
        service.persist_numeric_duration(
            context,
            workspace_token=workspace_token,
            expected_revision=cursor.draft_revision,
            draft=proposed,
        )
    except CursorConflictError:
        return _numeric_error(
            runtime,
            intent="unknown",
            message="这条期限上下文已经变化，请重新说明申请内容。",
            business_status="needs_clarification",
            phase="collecting",
            error_code="CURSOR_STALE",
        )
    return _numeric_success_update(state, runtime)


def _unavailable_policy_answer() -> PolicyAnswer:
    return PolicyAnswer(
        status="retrieval_unavailable",
        answer="政策检索暂时不可用，当前无法提供可靠依据。",
        evidence=[],
        next_step="请稍后重试；如问题紧急，请联系人工安全流程。",
    )


def _retrieve_policy_pgvector(
    state: GraphState,
    runtime: Runtime[GraphRuntimeContext],
) -> StateUpdate:
    policy_service = _context_value(runtime, "policy_service")
    session_factory = _context_value(runtime, "session_factory")
    if not isinstance(policy_service, PolicyService) or not callable(session_factory):
        raise GraphRuntimeContractError("policy retrieval service is unavailable")
    try:
        with session_factory() as session:
            answer = policy_service.query(session, state.safe_user_text)
    except Exception:
        answer = _unavailable_policy_answer()
    invocation = _optional_invocation_facts(runtime)
    if invocation is not None:
        invocation.policy_answer = answer
        invocation.tool_call = ReadOnlyToolCall(
            tool="search_policies",
            query=state.safe_user_text,
        )
        invocation.tool_result = ToolResult(status="success", policy_answer=answer)
    evidence_codes = list(dict.fromkeys(item.policy_code for item in answer.evidence))[:8]
    status = {
        "grounded": "grounded",
        "insufficient_evidence": "insufficient",
        "retrieval_unavailable": "unavailable",
    }[answer.status]
    summary = redact_sensitive_content(answer.answer.strip())
    return {
        "tool_name": "search_policies",
        "safe_tool_result": {
            "tool": "search_policies",
            "status": status,
            "entitlement_codes": [],
            "policy_codes": evidence_codes,
            "match_count": len(evidence_codes),
            "summary": summary,
        },
        "policy_evidence_codes": evidence_codes,
        "policy_match_count": len(evidence_codes),
    }


def _grade_policy_evidence(
    state: GraphState,
    runtime: Runtime[GraphRuntimeContext],
) -> StateUpdate:
    del runtime
    safe_result = state.safe_tool_result
    if safe_result is None or safe_result.tool != "search_policies":
        return {"policy_status": "unavailable"}
    if safe_result.status == "grounded" and state.policy_match_count > 0:
        policy_status = "grounded"
    elif safe_result.status in {"grounded", "insufficient"}:
        policy_status = "insufficient"
        if safe_result.status == "grounded":
            safe_result = safe_result.model_copy(
                update={
                    "status": "insufficient",
                    "policy_codes": [],
                    "match_count": 0,
                    "summary": "当前证据不足，无法可靠回答这个政策问题。",
                }
            )
    else:
        policy_status = "unavailable"
    return {
        "policy_status": policy_status,
        "safe_tool_result": safe_result,
    }


def _compose_policy_answer(
    state: GraphState,
    runtime: Runtime[GraphRuntimeContext],
    *,
    expected_status: str,
) -> StateUpdate:
    del runtime
    safe_result = state.safe_tool_result
    if safe_result is None or safe_result.tool != "search_policies":
        raise GraphRuntimeContractError("policy public answer is unavailable")
    message = safe_result.summary
    if state.security_flagged:
        message = f"{SECURITY_MESSAGE}\n\n{message}"
    return {
        "assistant_message": message,
        # Legacy policy failures are typed retrieval states but still an
        # answered conversation outcome; retain that public compatibility.
        "business_status": "answered",
        "phase": "answered",
        "recoverable_error": None,
        "policy_status": expected_status,
    }


def _compose_grounded_answer(
    state: GraphState,
    runtime: Runtime[GraphRuntimeContext],
) -> StateUpdate:
    return _compose_policy_answer(
        state,
        runtime,
        expected_status="grounded",
    )


def _compose_insufficient_answer(
    state: GraphState,
    runtime: Runtime[GraphRuntimeContext],
) -> StateUpdate:
    return _compose_policy_answer(
        state,
        runtime,
        expected_status="insufficient",
    )


def _compose_recoverable_answer(
    state: GraphState,
    runtime: Runtime[GraphRuntimeContext],
) -> StateUpdate:
    if state.selected_route == "policy":
        return _compose_policy_answer(
            state,
            runtime,
            expected_status="unavailable",
        )
    if state.assistant_message is not None and state.business_status != "pending":
        return {}
    return {
        "assistant_message": "当前请求暂时无法安全完成，请稍后重试。",
        "business_status": "recoverable_error",
        "phase": "recoverable_error",
        "recoverable_error": {
            "code": "GRAPH_STEP_UNAVAILABLE",
            "message": "当前请求暂时无法安全完成，请稍后重试。",
        },
    }


def _resolve_entitlement(
    state: GraphState,
    runtime: Runtime[GraphRuntimeContext],
) -> StateUpdate:
    if state.recoverable_error is not None:
        return {}
    if state.draft_patch is None or state.draft_patch.entitlement_id is None:
        raise GraphRuntimeContractError("entitlement candidate is unavailable")
    session_factory = _context_value(runtime, "session_factory")
    workspace_token = _context_value(runtime, "workspace_token")
    if not callable(session_factory) or not isinstance(workspace_token, str):
        raise GraphRuntimeContractError("entitlement resolver is unavailable")
    call = ReadOnlyToolCall(
        tool="resolve_entitlement",
        query=state.draft_patch.entitlement_id,
    )
    try:
        with session_factory() as session:
            result = execute_read_only_tool(
                session,
                workspace_token=workspace_token,
                call=call,
            )
    except Exception:
        result = ToolResult(status="workspace_not_found")
    invocation = _optional_invocation_facts(runtime)
    if invocation is not None:
        invocation.tool_call = call
        invocation.tool_result = result
    summary = entitlement_resolution_message(result)
    safe_result = _safe_tool_result(call, result, summary=summary)
    resolution = result.entitlement_resolution
    matched = (
        result.status == "success"
        and resolution is not None
        and resolution.status == "matched"
        and len(resolution.candidates) == 1
    )
    if matched:
        assert resolution is not None
        return {
            "draft_patch": state.draft_patch.model_copy(
                update={"entitlement_id": resolution.candidates[0].code}
            ),
            "tool_name": "resolve_entitlement",
            "safe_tool_result": safe_result,
        }

    resolution_status = resolution.status if resolution is not None else None
    business_status: BusinessStatus = (
        cast(BusinessStatus, f"entitlement_{resolution_status}")
        if resolution_status in {"ambiguous", "no_match"}
        else "resolution_unavailable"
    )
    snapshot = _read_authoritative_snapshot(
        state.workspace_ref,
        runtime,
        include_quota=False,
    )
    message = summary
    if state.security_flagged:
        message = f"{SECURITY_MESSAGE}\n\n{message}"
    return {
        "tool_name": "resolve_entitlement",
        "safe_tool_result": safe_result,
        "assistant_message": message,
        "business_status": business_status,
        "phase": ("collecting" if snapshot.draft.missing_fields() else "awaiting_confirmation"),
    }


def _merged_draft(
    state: GraphState,
    runtime: Runtime[GraphRuntimeContext],
) -> tuple[_AuthoritativeSnapshot, RequestDraft]:
    snapshot = _read_authoritative_snapshot(
        state.workspace_ref,
        runtime,
        include_quota=False,
    )
    patch = state.draft_patch
    candidate = ParsedReply(
        employee_id=snapshot.workspace.actor_id,
        entitlement_id=patch.entitlement_id if patch is not None else None,
        duration_days=patch.duration_days if patch is not None else None,
        justification=patch.justification if patch is not None else None,
        confirmed=None,
    )
    return snapshot, merge_request_candidate(snapshot.draft, candidate)


def _merge_candidate(
    state: GraphState,
    runtime: Runtime[GraphRuntimeContext],
) -> StateUpdate:
    if state.recoverable_error is not None or state.business_status in {
        "entitlement_ambiguous",
        "entitlement_no_match",
        "resolution_unavailable",
    }:
        return {}
    _, merged = _merged_draft(state, runtime)
    missing = cast(list[MissingField], merged.missing_fields())
    return {
        "missing_fields": missing,
        "phase": "collecting" if missing else "awaiting_confirmation",
    }


def _draft_conflict_update(
    state: GraphState,
    runtime: Runtime[GraphRuntimeContext],
) -> StateUpdate:
    snapshot = _read_authoritative_snapshot(
        state.workspace_ref,
        runtime,
        include_quota=False,
    )
    message = "申请草稿刚刚被另一轮更新，请基于最新草稿继续。"
    return {
        "committed_draft_revision": snapshot.workspace.draft_revision,
        "missing_fields": cast(list[MissingField], snapshot.draft.missing_fields()),
        "assistant_message": message,
        "business_status": "recoverable_error",
        "phase": "recoverable_error",
        "recoverable_error": {
            "code": "DRAFT_REVISION_CONFLICT",
            "message": message,
        },
    }


def _persist_draft_cas(
    state: GraphState,
    runtime: Runtime[GraphRuntimeContext],
) -> StateUpdate:
    if state.recoverable_error is not None or state.business_status in {
        "entitlement_ambiguous",
        "entitlement_no_match",
        "resolution_unavailable",
    }:
        return {}
    snapshot, merged = _merged_draft(state, runtime)
    workspace_token = _context_value(runtime, "workspace_token")
    if not isinstance(workspace_token, str):
        raise GraphRuntimeContractError("workspace binding is unavailable")
    service = _step_service(runtime)
    context = _step_context(state, runtime)
    cursor = snapshot.cursor
    route = IntentRoute(
        intent="request_access",
        security_probe=state.security_flagged,
    )
    try:
        if (
            cursor is not None
            and cursor.expected_field == "justification"
            and is_safe_justification_cursor_reply(state.safe_user_text, route)
        ):
            completed = service.persist_justification_cursor(
                context,
                workspace_token=workspace_token,
                expected_revision=state.base_draft_revision,
                draft=merged,
            )
        else:
            completed = service.persist_draft(
                context,
                workspace_token=workspace_token,
                expected_revision=state.base_draft_revision,
                draft=merged,
            )
    except (CursorConflictError, DraftRevisionConflictError):
        return _draft_conflict_update(state, runtime)
    authoritative = _read_authoritative_snapshot(
        state.workspace_ref,
        runtime,
        include_quota=False,
    )
    return {
        "committed_draft_revision": completed.committed_revision,
        "missing_fields": cast(list[MissingField], authoritative.draft.missing_fields()),
    }


def _validate_draft(
    state: GraphState,
    runtime: Runtime[GraphRuntimeContext],
) -> StateUpdate:
    if state.recoverable_error is not None or state.business_status in {
        "entitlement_ambiguous",
        "entitlement_no_match",
        "resolution_unavailable",
    }:
        return {}
    snapshot = _read_authoritative_snapshot(
        state.workspace_ref,
        runtime,
        include_quota=False,
    )
    missing = cast(list[MissingField], snapshot.draft.missing_fields())
    if missing:
        return {
            "missing_fields": missing,
            "business_status": "collecting",
            "phase": "collecting",
        }
    session_factory = _context_value(runtime, "session_factory")
    if not callable(session_factory):
        raise GraphRuntimeContractError("database service is unavailable")
    with session_factory() as session:
        result = validate_access_request(session, snapshot.draft)
    invocation = _optional_invocation_facts(runtime)
    if invocation is not None:
        invocation.tool_result = result
    summary = (
        "申请字段与目录校验通过"
        if result.status == "success"
        else "申请未通过目录校验，请检查员工、权限或期限"
    )
    safe_result: dict[str, object] = {
        "tool": "validate_access_request",
        "status": result.status,
        "entitlement_codes": [],
        "policy_codes": [],
        "match_count": 0,
        "summary": summary,
    }
    if result.status != "success":
        return {
            "tool_name": "validate_access_request",
            "safe_tool_result": safe_result,
            "assistant_message": summary,
            "business_status": "validation_failed",
            "phase": "recoverable_error",
        }
    return {
        "tool_name": "validate_access_request",
        "safe_tool_result": safe_result,
        "business_status": "awaiting_confirmation",
        "phase": "awaiting_confirmation",
    }


def _ask_missing_field(
    state: GraphState,
    runtime: Runtime[GraphRuntimeContext],
) -> StateUpdate:
    del runtime
    if not state.missing_fields:
        raise GraphRuntimeContractError("missing-field branch has no missing field")
    message = missing_field_question(state.missing_fields[0])
    if state.security_flagged:
        message = f"{SECURITY_MESSAGE}\n\n{message}"
    return {
        "assistant_message": message,
        "business_status": "collecting",
        "phase": "collecting",
    }


def _await_requester_confirmation(
    state: GraphState,
    runtime: Runtime[GraphRuntimeContext],
) -> Command[Any]:
    """T34: the single P0 dynamic confirmation interrupt.

    This node itself performs no business writes: no pending row, no Cursor, no
    quota, no model/tool calls and no formal request creation.  The caller
    pre-allocates ``pending_input_id`` into the runtime context so the interrupt
    payload (which must be plain JSON data) can reference it; the fenced
    application transaction that persists the pending projection runs later in
    the caller.

    On resume the node re-runs and ``interrupt()`` returns the value of the
    single ``Command(resume=...)``; the node then routes to rehydration instead
    of ending the graph, so the new input is processed inside this same call.
    """
    if state.pending_input_id is None:
        raw = runtime.context.get("pending_input_id")
        if not isinstance(raw, str):
            raise GraphRuntimeContractError("confirmation pending id is unavailable")
        try:
            pending_input_id = UUID(raw)
        except ValueError:
            raise GraphRuntimeContractError(
                "confirmation pending id is unavailable"
            ) from None
    else:
        pending_input_id = state.pending_input_id
    draft_revision = state.committed_draft_revision
    if draft_revision is None:
        draft_revision = state.base_draft_revision
    payload: dict[str, object] = {
        "kind": "confirmation",
        "pending_input_id": str(pending_input_id),
        "draft_revision": draft_revision,
        "summary": "申请信息已完整。请明确回复“确认提交”后再创建正式申请。",
        "allowed_decisions": ["confirm", "route_new_input"],
    }
    resume_value = interrupt(payload)
    if not isinstance(resume_value, dict):
        raise GraphRuntimeContractError("confirmation resume payload is invalid")
    decision = resume_value.get("decision")
    safe_user_text = resume_value.get("safe_user_text")
    if not isinstance(safe_user_text, str) or not safe_user_text.strip():
        raise GraphRuntimeContractError("confirmation resume input is empty")
    selected_route = (
        "resume_confirm" if decision == "confirm" else "resume_new_input"
    )
    resume_update: dict[str, object] = {
        "selected_route": selected_route,
        "safe_user_text": safe_user_text,
        "pending_input_id": pending_input_id,
        "pending_input_kind": "confirmation",
    }
    # The resume turn owns a new HTTP turn/input_seq (allocated by the
    # accept-resume transaction); refresh the durable state so downstream
    # step facts belong to the current execution, not the first input.
    current_turn = runtime.context.get("current_turn_id")
    if isinstance(current_turn, str) and current_turn:
        resume_update["input_turn_id"] = current_turn
    current_seq = runtime.context.get("current_input_seq")
    if type(current_seq) is int and current_seq >= 0:
        resume_update["input_seq"] = current_seq
    update = validate_state_update(state, resume_update)
    return Command(
        update=update.model_dump(mode="python"),
        goto="rehydrate_resume_snapshot",
    )


def _confirmation_conflict_update(
    state: GraphState,
    runtime: Runtime[GraphRuntimeContext],
) -> StateUpdate:
    """Stable recoverable close for any confirmation binding mismatch."""
    snapshot = _read_authoritative_snapshot(
        state.workspace_ref,
        runtime,
        include_quota=False,
    )
    message = "确认状态已变化，请核对当前申请信息后重新确认。"
    return {
        "committed_draft_revision": snapshot.workspace.draft_revision,
        "missing_fields": cast(list[MissingField], snapshot.draft.missing_fields()),
        "assistant_message": message,
        "business_status": "recoverable_error",
        "phase": "recoverable_error",
        "recoverable_error": {
            "code": "CONFIRMATION_CONFLICT",
            "message": message,
        },
    }


def _rehydrate_resume_snapshot(
    state: GraphState,
    runtime: Runtime[GraphRuntimeContext],
) -> StateUpdate:
    """T34: re-authorize and re-read authoritative facts after every resume.

    The checkpoint's derived fields belong to the previous input; only the
    server-side pending/Cursor/workspace facts may decide the resume outcome.
    Any Principal/Session/pending/Cursor/revision mismatch closes safely with a
    recoverable conflict instead of confirming a stale draft.
    """
    snapshot = _read_authoritative_snapshot(
        state.workspace_ref,
        runtime,
        include_quota=False,
    )
    session_factory = _context_value(runtime, "session_factory")
    auth_session_id = _context_value(runtime, "auth_session_id")
    principal = _context_value(runtime, "principal")
    if not callable(session_factory) or not isinstance(principal, Principal):
        raise GraphRuntimeContractError("database service is unavailable")
    pending_input_id = state.pending_input_id
    if pending_input_id is None:
        raise GraphRuntimeContractError("confirmation pending id is missing")
    try:
        auth_session_ref = UUID(str(auth_session_id))
    except ValueError:
        raise GraphRuntimeContractError(
            "graph execution binding failed"
        ) from None
    with session_factory() as session:
        pending = session.scalar(
            select(AgentPendingInputRecord).where(
                AgentPendingInputRecord.workspace_id == state.workspace_ref,
                AgentPendingInputRecord.pending_input_id == pending_input_id,
                AgentPendingInputRecord.status.in_(("active", "resuming")),
            )
        )
    cursor = snapshot.cursor
    if (
        pending is None
        or pending.auth_session_ref != auth_session_ref
        or pending.actor_id != principal.employee_id
        or cursor is None
        or cursor.expected_field != "confirmation"
        or cursor.auth_session_id != auth_session_id
        or snapshot.workspace.draft_revision != state.base_draft_revision
    ):
        return _confirmation_conflict_update(state, runtime)
    confirmed = _explicit_confirmation_from_text(state.safe_user_text)
    return {
        "selected_route": (
            "resume_confirm" if confirmed is True else "resume_new_input"
        ),
        "base_draft_revision": snapshot.workspace.draft_revision,
        "committed_draft_revision": None,
        "draft_patch": _draft_patch(snapshot.draft),
        "missing_fields": cast(list[MissingField], snapshot.draft.missing_fields()),
        "phase": "routing",
        "tool_name": None,
        "safe_tool_result": None,
        "policy_status": None,
        "policy_evidence_codes": [],
        "policy_match_count": 0,
        "business_status": "pending",
        "assistant_message": None,
        "recoverable_error": None,
    }


def _apply_confirmation_cas(
    state: GraphState,
    runtime: Runtime[GraphRuntimeContext],
) -> StateUpdate:
    """T34: the one confirmation CAS, keyed by the stable pending_input_id.

    Only the existing confirmation function (checked in rehydration) reaches
    this node; the CAS service is idempotent across HTTP retries and closes
    safely on revision/pending mismatches.
    """
    if (
        state.recoverable_error is not None
        or state.business_status == "recoverable_error"
    ):
        return {}
    if state.pending_input_id is None:
        raise GraphRuntimeContractError("confirmation pending id is missing")
    service = _step_service(runtime)
    context = _step_context(state, runtime)
    workspace_token = _context_value(runtime, "workspace_token")
    if not isinstance(workspace_token, str):
        raise GraphRuntimeContractError("workspace binding is unavailable")
    try:
        completed = service.confirm_draft(
            context,
            workspace_token=workspace_token,
            pending_input_id=state.pending_input_id,
            expected_revision=state.base_draft_revision,
        )
    except (DraftRevisionConflictError, StepExecutionRejected, StepOperationConflict):
        return _confirmation_conflict_update(state, runtime)
    return {
        "committed_draft_revision": completed.committed_revision,
        "business_status": "ready_to_submit",
        "phase": "ready_to_submit",
        "assistant_message": "已确认。申请草稿已就绪，可以提交正式申请。",
        "tool_name": None,
        "safe_tool_result": None,
    }


def _route_confirmation_result(state: GraphState) -> str:
    if (
        state.recoverable_error is not None
        or state.phase == "recoverable_error"
        or state.business_status == "recoverable_error"
    ):
        return "conflict"
    return "success"


def _finalize_public_outcome(
    state: GraphState,
    runtime: Runtime[GraphRuntimeContext],
) -> StateUpdate:
    del runtime
    if state.intent is None or state.assistant_message is None:
        raise GraphRuntimeContractError("graph public outcome is incomplete")
    if state.selected_route == "policy":
        if (
            state.tool_name != "search_policies"
            or state.policy_status is None
            or state.safe_tool_result is None
        ):
            raise GraphRuntimeContractError("policy outcome is incomplete")
    elif state.policy_status is not None or state.policy_evidence_codes:
        raise GraphRuntimeContractError("non-RAG outcome contains RAG state")
    return {}


def _route_after_intent(state: GraphState) -> str:
    return state.selected_route


def _route_policy_evidence(state: GraphState) -> str:
    return state.policy_status or "unavailable"


def _route_entitlement_resolution(state: GraphState) -> str:
    return (
        "yes"
        if state.draft_patch is not None and state.draft_patch.entitlement_id is not None
        else "no"
    )


def _route_draft_validation(state: GraphState) -> str:
    if (
        state.recoverable_error is not None
        or state.phase == "recoverable_error"
        or state.business_status
        in {
            "entitlement_ambiguous",
            "entitlement_no_match",
            "resolution_unavailable",
            "validation_failed",
        }
    ):
        return "invalid_or_conflict"
    if state.missing_fields:
        return "missing"
    return "complete"


def _route_resume(state: GraphState) -> str:
    return "confirm" if state.selected_route == "resume_confirm" else "non_confirm_input"


Checkpointer = BaseCheckpointSaver[Any] | Literal[False] | None


def _activate_t32_missing_cursor(
    *,
    service: AgentStepOperationService,
    context: AgentStepContext,
    workspace_token: str,
    turn: GraphOutput,
) -> None:
    """Project only the next missing-field Cursor after a successful terminal."""

    if (
        turn.intent != "request_access"
        or turn.business_status != "collecting"
        or not turn.missing_fields
    ):
        return
    expected_field = turn.missing_fields[0]
    if expected_field not in {"entitlement_id", "duration_days", "justification"}:
        raise GraphRuntimeContractError("cursor projection field is unsupported")
    service.activate_missing_cursor(
        context,
        workspace_token=workspace_token,
        expected_revision=turn.draft_revision,
        expected_field=expected_field,
    )


def _compile_production_graph(
    checkpointer: Checkpointer,
) -> CompiledStateGraph[
    GraphState,
    GraphRuntimeContext,
    GraphInput,
    _GraphNodeOutput,
]:
    builder = StateGraph(
        GraphState,
        context_schema=GraphRuntimeContext,
        input_schema=GraphInput,
        output_schema=_GraphNodeOutput,
    )
    builder.add_node(
        "hydrate_authoritative_snapshot",
        cast(Any, _hydrate_authoritative_snapshot),
        input_schema=GraphInput,
    )
    builder.add_node(
        "route_intent",
        cast(Any, validated_state_node(_route_intent)),
    )
    builder.add_node(
        "compose_safe_answer",
        cast(Any, validated_state_node(_compose_safe_answer)),
    )
    implemented_nodes: dict[str, StateStub] = {
        "handle_numeric_followup": _handle_numeric_followup,
        "select_read_tool": _select_read_tool,
        "execute_read_tool": _execute_read_tool,
        "retrieve_policy_pgvector": _retrieve_policy_pgvector,
        "grade_policy_evidence": _grade_policy_evidence,
        "compose_grounded_answer": _compose_grounded_answer,
        "compose_insufficient_answer": _compose_insufficient_answer,
        "compose_recoverable_answer": _compose_recoverable_answer,
        "parse_request_patch": _parse_request_patch,
        "resolve_entitlement": _resolve_entitlement,
        "merge_candidate": _merge_candidate,
        "persist_draft_cas": _persist_draft_cas,
        "validate_draft": _validate_draft,
        "ask_missing_field": _ask_missing_field,
        "rehydrate_resume_snapshot": _rehydrate_resume_snapshot,
        "apply_confirmation_cas": _apply_confirmation_cas,
        "finalize_public_outcome": _finalize_public_outcome,
    }
    for node_name in PRODUCTION_NODE_NAMES:
        if node_name in {
            "hydrate_authoritative_snapshot",
            "route_intent",
            "compose_safe_answer",
            "await_requester_confirmation",
        }:
            continue
        builder.add_node(
            node_name,
            cast(
                Any,
                validated_state_node(implemented_nodes.get(node_name, _passthrough_stub)),
            ),
        )
    # The confirmation interrupt node is registered unwrapped: on first run it
    # stops the graph via interrupt() (no business write), on resume it returns
    # Command(update, goto=rehydrate) whose update is validated inside the node.
    builder.add_node(
        "await_requester_confirmation",
        cast(Any, _await_requester_confirmation),
    )

    builder.add_edge(START, "hydrate_authoritative_snapshot")
    builder.add_edge("hydrate_authoritative_snapshot", "route_intent")
    builder.add_conditional_edges(
        "route_intent",
        _route_after_intent,
        PRODUCTION_CONDITIONAL_PATHS["route_intent"],
    )
    builder.add_edge("select_read_tool", "execute_read_tool")
    builder.add_edge("execute_read_tool", "compose_safe_answer")
    builder.add_edge("retrieve_policy_pgvector", "grade_policy_evidence")
    builder.add_conditional_edges(
        "grade_policy_evidence",
        _route_policy_evidence,
        PRODUCTION_CONDITIONAL_PATHS["grade_policy_evidence"],
    )
    builder.add_conditional_edges(
        "parse_request_patch",
        _route_entitlement_resolution,
        PRODUCTION_CONDITIONAL_PATHS["request_needs_entitlement_resolution"],
    )
    builder.add_edge("resolve_entitlement", "merge_candidate")
    builder.add_edge("merge_candidate", "persist_draft_cas")
    builder.add_edge("persist_draft_cas", "validate_draft")
    builder.add_conditional_edges(
        "validate_draft",
        _route_draft_validation,
        PRODUCTION_CONDITIONAL_PATHS["validate_draft"],
    )
    # T34: the confirmation interrupt node has no static outgoing edge; the
    # graph stops at interrupt() and resume re-enters the node, which routes to
    # rehydrate_resume_snapshot via Command(goto=...).
    builder.add_conditional_edges(
        "rehydrate_resume_snapshot",
        _route_resume,
        PRODUCTION_CONDITIONAL_PATHS["rehydrate_resume_snapshot"],
    )
    builder.add_conditional_edges(
        "apply_confirmation_cas",
        _route_confirmation_result,
        PRODUCTION_CONDITIONAL_PATHS["apply_confirmation_cas"],
    )
    for node_name in (
        "compose_safe_answer",
        "handle_numeric_followup",
        "compose_grounded_answer",
        "compose_insufficient_answer",
        "compose_recoverable_answer",
        "ask_missing_field",
        "ready_to_submit",
    ):
        builder.add_edge(node_name, "finalize_public_outcome")
    builder.add_edge("finalize_public_outcome", END)
    return builder.compile(checkpointer=checkpointer)


@dataclass(frozen=True)
class ProductionGraph:
    """The only supported invoke boundary for the compiled production graph."""

    compiled: CompiledStateGraph[
        GraphState,
        GraphRuntimeContext,
        GraphInput,
        _GraphNodeOutput,
    ]

    @staticmethod
    def _prepare_context(
        context: GraphRuntimeContext,
    ) -> tuple[GraphRuntimeContext, _InvocationFacts]:
        invocation = _InvocationFacts()
        prepared = dict(context)
        prepared["_invocation_facts"] = invocation
        return cast(GraphRuntimeContext, prepared), invocation

    @staticmethod
    def _interrupt_payload(result: object) -> dict[str, object] | None:
        """Extract the confirmation interrupt value from a stopped run result.

        LangGraph 1.2.11 keeps the dynamic interrupt inside the returned state
        as ``__interrupt__`` (a tuple/list of Interrupt objects) instead of
        raising; the value must be plain JSON-serializable data.
        """
        if not isinstance(result, dict):
            return None
        interrupts = result.get("__interrupt__")
        if not interrupts:
            return None
        value = interrupts[0].value
        if not isinstance(value, dict):
            raise GraphRuntimeContractError("confirmation interrupt payload is invalid")
        return value

    def invoke(
        self,
        graph_input: GraphInput | Command[Any] | Mapping[str, object],
        config: RunnableConfig | None = None,
        *,
        context: GraphRuntimeContext,
        **kwargs: Any,
    ) -> GraphOutput:
        """Validate raw input before the compiled graph can call its checkpointer.

        ``Command(resume=...)`` inputs bypass the GraphInput contract: their
        authoritative input facts were already persisted by the accept-resume
        application transaction, and the graph continues from the exact
        checkpoint seeded into the execution.  When the graph stops at the
        confirmation interrupt the caller receives
        :class:`ConfirmationInterruptRaised` carrying the plain JSON payload.
        """
        is_resume = isinstance(graph_input, Command)
        if is_resume:
            validated_input: GraphInput | None = None
            resume_command = graph_input
        else:
            validated_input = GraphInput.model_validate(graph_input)
            resume_command = None
        prepared_context, invocation = self._prepare_context(context)
        invoke_input = cast(
            GraphInput | Command[Any] | None,
            resume_command if is_resume else validated_input,
        )
        result = cast(
            Any,
            self.compiled.invoke(
                invoke_input,
                config,
                context=prepared_context,
                **kwargs,
            ),
        )
        payload = self._interrupt_payload(result)
        if payload is not None:
            raise ConfirmationInterruptRaised(payload)
        node_output = _GraphNodeOutput.model_validate(result)
        if is_resume:
            # Resume continues from the seeded checkpoint; its durable state
            # carries the workspace reference (GraphInput was not re-supplied).
            if config is None:
                raise GraphRuntimeContractError("resume requires an exact config")
            resumed = self.compiled.get_state(config)
            workspace_ref = resumed.values.get("workspace_ref")
            if not isinstance(workspace_ref, UUID):
                raise GraphRuntimeContractError(
                    "resume state lacks the workspace reference"
                )
        else:
            assert validated_input is not None
            workspace_ref = validated_input.workspace_ref
        snapshot = _read_authoritative_snapshot(
            workspace_ref,
            Runtime(context=prepared_context),
            include_quota=True,
        )
        if snapshot.quota is None:
            raise GraphRuntimeContractError("authoritative output snapshot is unavailable")
        if node_output.phase == "recoverable_error":
            public_phase = ConversationPhase.RECOVERABLE_ERROR
        elif node_output.phase == "collecting":
            public_phase = ConversationPhase.COLLECTING
        elif node_output.phase in {"awaiting_confirmation", "ready_to_submit"}:
            public_phase = ConversationPhase.AWAITING_CONFIRMATION
        else:
            public_phase = (
                ConversationPhase.COLLECTING
                if snapshot.draft.missing_fields()
                else ConversationPhase.AWAITING_CONFIRMATION
            )
        return GraphOutput(
            assistant_message=node_output.assistant_message,
            draft=snapshot.draft,
            missing_fields=snapshot.draft.missing_fields(),
            phase=public_phase,
            business_status=node_output.business_status,
            quota=snapshot.quota,
            intent=node_output.intent,
            security_flagged=node_output.security_flagged,
            tool_results=([invocation.tool_result] if invocation.tool_result is not None else []),
            draft_revision=snapshot.workspace.draft_revision,
            error_code=(
                invocation.error_code
                or (
                    node_output.recoverable_error.code
                    if node_output.recoverable_error is not None
                    else None
                )
            ),
        )

    def prepare(
        self,
        graph_input: GraphInput | Mapping[str, object],
        config: RunnableConfig | None = None,
        *,
        context: GraphRuntimeContext,
        **kwargs: Any,
    ) -> ProductionGraphRunResult:
        """Prepare one turn and defer the allowed Cursor write until terminal success."""

        validated_input = GraphInput.model_validate(graph_input)
        turn = self.invoke(
            validated_input,
            config,
            context=context,
            **kwargs,
        )
        workspace_token = context.get("workspace_token")
        if not isinstance(workspace_token, str):
            raise GraphRuntimeContractError("cursor finalizer context is unavailable")
        prepared_context, _ = self._prepare_context(context)
        runtime = Runtime(context=prepared_context)
        step_context = _execution_step_context(
            runtime,
            workspace_ref=validated_input.workspace_ref,
            graph_run_id=validated_input.graph_run_id,
            input_seq=validated_input.input_seq,
            input_turn_id=validated_input.input_turn_id,
        )
        service = _step_service(runtime)

        def finalize() -> None:
            _activate_t32_missing_cursor(
                service=service,
                context=step_context,
                workspace_token=workspace_token,
                turn=turn,
            )

        return ProductionGraphRunResult(turn=turn, success_finalizer=finalize)

    def stream(
        self,
        graph_input: GraphInput | Command[Any] | Mapping[str, object],
        config: RunnableConfig | None = None,
        *,
        context: GraphRuntimeContext,
        **kwargs: Any,
    ) -> Iterator[object]:
        """Preflight input, then expose the compiled graph's real stream.

        ``Command(resume=...)`` inputs bypass the GraphInput contract exactly
        like :meth:`invoke`; the graph continues from the seeded checkpoint.
        """

        is_resume = isinstance(graph_input, Command)
        validated_input = (
            None if is_resume else GraphInput.model_validate(graph_input)
        )
        prepared_context, _ = self._prepare_context(context)
        stream_input = cast(
            GraphInput | Command[Any] | None,
            graph_input if is_resume else validated_input,
        )
        yield from self.compiled.stream(
            stream_input,
            config,
            context=prepared_context,
            **kwargs,
        )

    def get_state(
        self,
        config: RunnableConfig,
        *,
        subgraphs: bool = False,
    ) -> Any:
        """Expose the compiled graph's exact state for a given locator."""

        return self.compiled.get_state(config, subgraphs=subgraphs)


def build_production_graph(*, checkpointer: Checkpointer) -> ProductionGraph:
    """Build the reusable graph; callers must inject the T29 saver explicitly."""

    return ProductionGraph(compiled=_compile_production_graph(checkpointer))
