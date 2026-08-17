"""Typed, side-effect-free v1.3 production topology skeleton."""

from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict, cast
from uuid import UUID

from langchain_core.runnables.config import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
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

from accesspilot.agent.routing import ConversationIntent
from accesspilot.agent.safety import redact_sensitive_content
from accesspilot.agent.state import (
    BoundedMessage,
    BusinessStatus,
    GraphPhase,
    GraphState,
    InputKind,
    MissingField,
    SafeRecoverableError,
    TurnReference,
    is_canonical_entitlement_code,
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


class GraphOutput(_StrictFrozenModel):
    """The bounded public facts exposed after the final graph node."""

    assistant_message: BoundedMessage
    missing_fields: Annotated[list[MissingField], Field(max_length=3)]
    phase: GraphPhase
    business_status: BusinessStatus
    intent: ConversationIntent
    security_flagged: StrictBool
    committed_draft_revision: Annotated[StrictInt, Field(ge=0)] | None
    recoverable_error: SafeRecoverableError | None


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
    cookie: str
    csrf_token: str
    api_key: str


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
        "rehydrate_resume_snapshot",
        "apply_confirmation_cas",
        "ready_to_submit",
        "finalize_public_outcome",
        END,
    ),
    "request_resume_new_input": (
        "await_requester_confirmation",
        "rehydrate_resume_snapshot",
        "route_intent",
        "compose_safe_answer",
        "finalize_public_outcome",
        END,
    ),
}


StateUpdate = Mapping[str, object] | GraphState
StateStub = Callable[[GraphState, Runtime[GraphRuntimeContext]], StateUpdate]
ValidatedStateNode = Callable[[GraphState, Runtime[GraphRuntimeContext]], GraphState]


class UnsafeGraphStateUpdateError(RuntimeError):
    """A node returned data outside the durable state contract."""


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


def _hydrate_authoritative_snapshot(
    graph_input: GraphInput,
    runtime: Runtime[GraphRuntimeContext],
) -> GraphState:
    """T30 stub: shape safe input without touching runtime dependencies."""

    del runtime
    return GraphState(
        **graph_input.model_dump(mode="python"),
        intent=None,
        security_flagged=False,
        selected_route="unknown",
        base_draft_revision=0,
        committed_draft_revision=None,
        draft_patch=None,
        missing_fields=[],
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


def _route_intent_stub(
    state: GraphState,
    runtime: Runtime[GraphRuntimeContext],
) -> StateUpdate:
    del state, runtime
    return {"intent": "unknown", "selected_route": "unknown"}


def _compose_safe_answer_stub(
    state: GraphState,
    runtime: Runtime[GraphRuntimeContext],
) -> StateUpdate:
    del state, runtime
    return {
        "assistant_message": "生产图骨架已完成安全路由；业务节点尚未接入。",
        "business_status": "answered",
        "phase": "answered",
    }


def _route_after_intent(state: GraphState) -> str:
    return state.selected_route


def _route_policy_evidence(state: GraphState) -> str:
    return state.policy_status or "unavailable"


def _route_entitlement_resolution(state: GraphState) -> str:
    if (
        state.draft_patch is None
        or state.draft_patch.entitlement_id is None
        or not is_canonical_entitlement_code(state.draft_patch.entitlement_id)
    ):
        return "yes"
    return "no"


def _route_draft_validation(state: GraphState) -> str:
    if state.recoverable_error is not None or state.phase == "recoverable_error":
        return "invalid_or_conflict"
    if state.missing_fields:
        return "missing"
    return "complete"


def _route_resume(state: GraphState) -> str:
    return "confirm" if state.selected_route == "resume_confirm" else "non_confirm_input"


Checkpointer = BaseCheckpointSaver[Any] | Literal[False] | None


def _compile_production_graph(
    checkpointer: Checkpointer,
) -> CompiledStateGraph[
    GraphState,
    GraphRuntimeContext,
    GraphInput,
    GraphOutput,
]:
    builder = StateGraph(
        GraphState,
        context_schema=GraphRuntimeContext,
        input_schema=GraphInput,
        output_schema=GraphOutput,
    )
    builder.add_node(
        "hydrate_authoritative_snapshot",
        cast(Any, _hydrate_authoritative_snapshot),
        input_schema=GraphInput,
    )
    builder.add_node(
        "route_intent",
        cast(Any, validated_state_node(_route_intent_stub)),
    )
    builder.add_node(
        "compose_safe_answer",
        cast(Any, validated_state_node(_compose_safe_answer_stub)),
    )
    for node_name in PRODUCTION_NODE_NAMES:
        if node_name in {
            "hydrate_authoritative_snapshot",
            "route_intent",
            "compose_safe_answer",
        }:
            continue
        builder.add_node(
            node_name,
            cast(Any, validated_state_node(_passthrough_stub)),
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
    # T30 deliberately contains no interrupt(). This edge is the replaceable
    # T34 resume contract and keeps rehydration visible in the production DAG.
    builder.add_edge("await_requester_confirmation", "rehydrate_resume_snapshot")
    builder.add_conditional_edges(
        "rehydrate_resume_snapshot",
        _route_resume,
        PRODUCTION_CONDITIONAL_PATHS["rehydrate_resume_snapshot"],
    )
    builder.add_edge("apply_confirmation_cas", "ready_to_submit")
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
        GraphOutput,
    ]

    def invoke(
        self,
        graph_input: GraphInput | Mapping[str, object],
        config: RunnableConfig | None = None,
        *,
        context: GraphRuntimeContext,
        **kwargs: Any,
    ) -> GraphOutput:
        """Validate raw input before the compiled graph can call its checkpointer."""

        validated_input = GraphInput.model_validate(graph_input)
        result = cast(
            Any,
            self.compiled.invoke(
                validated_input,
                config,
                context=context,
                **kwargs,
            ),
        )
        return GraphOutput.model_validate(result)


def build_production_graph(*, checkpointer: Checkpointer) -> ProductionGraph:
    """Build the reusable graph; callers must inject the T29 saver explicitly."""

    return ProductionGraph(compiled=_compile_production_graph(checkpointer))
