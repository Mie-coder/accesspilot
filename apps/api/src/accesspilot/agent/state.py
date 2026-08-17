"""Legacy collection state and the durable v1.3 production graph schemas."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Literal, Self, TypedDict
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from accesspilot.agent.routing import ConversationIntent
from accesspilot.agent.safety import redact_sensitive_content
from accesspilot.domain.models import RequestDraft


class ConversationPhase(StrEnum):
    """Legacy v1.2 conversation phase retained during graph migration."""

    COLLECTING = "collecting"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    RECOVERABLE_ERROR = "recoverable_error"


class RecoverableError(TypedDict):
    """Legacy recoverable error shape."""

    code: str
    message: str


class CollectionGraphState(TypedDict):
    """The pre-v1.3 teaching graph state; never used by the production graph."""

    draft: RequestDraft
    missing_fields: list[str]
    tool_summaries: list[str]
    phase: ConversationPhase
    recoverable_error: RecoverableError | None
    next_question: str | None


def create_initial_state(draft: RequestDraft) -> CollectionGraphState:
    """Create the legacy teaching graph's initial collection state."""

    return {
        "draft": draft,
        "missing_fields": draft.missing_fields(),
        "tool_summaries": [],
        "phase": ConversationPhase.COLLECTING,
        "recoverable_error": None,
        "next_question": None,
    }


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


BoundedReference = Annotated[
    StrictStr,
    Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9:._-]+$"),
]
TurnReference = Annotated[
    StrictStr,
    Field(
        min_length=1,
        max_length=120,
        pattern=(
            r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}|turn-[A-Za-z0-9._-]{1,115})$"
        ),
    ),
]
BoundedMessage = Annotated[StrictStr, Field(min_length=1, max_length=4_000)]
PolicyCode = Annotated[
    StrictStr,
    Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._-]+$"),
]
EntitlementCode = Annotated[
    StrictStr,
    Field(
        min_length=1,
        max_length=160,
        pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+$",
    ),
]
EntitlementCandidateText = Annotated[
    StrictStr,
    Field(min_length=1, max_length=160, pattern=r"^[^\x00\r\n]+$"),
]
MissingField = Literal["entitlement_id", "duration_days", "justification"]
InputKind = Literal["new_input", "resume"]
SelectedRoute = Literal[
    "security",
    "help",
    "unknown",
    "numeric_cursor",
    "read_only",
    "policy",
    "request_access",
    "resume_confirm",
    "resume_new_input",
]
GraphPhase = Literal[
    "routing",
    "collecting",
    "awaiting_confirmation",
    "ready_to_submit",
    "answered",
    "recoverable_error",
]
PolicyStatus = Literal["grounded", "insufficient", "unavailable"]
PendingInputKind = Literal["confirmation"]
SafeToolName = Literal[
    "list_eligible_access",
    "list_active_access",
    "get_latest_request_status",
    "resolve_entitlement",
    "list_policy_catalog",
    "get_self_approval_policy",
    "search_policies",
    "validate_access_request",
]
SafeToolStatus = Literal[
    "success",
    "invalid_argument",
    "employee_not_found",
    "system_not_found",
    "entitlement_not_found",
    "validation_failed",
    "request_not_found",
    "workspace_not_found",
    "matched",
    "ambiguous",
    "no_match",
    "grounded",
    "insufficient",
    "unavailable",
]
BusinessStatus = Literal[
    "pending",
    "answered",
    "needs_clarification",
    "collecting",
    "awaiting_confirmation",
    "ready_to_submit",
    "validation_failed",
    "recoverable_error",
    "entitlement_ambiguous",
    "entitlement_no_match",
    "resolution_unavailable",
]


def _redact_bounded_text(value: object) -> object:
    if not isinstance(value, str):
        return value
    return redact_sensitive_content(value.strip())


class DraftPatch(_StrictFrozenModel):
    """Only normalized request candidate fields may enter a checkpoint."""

    entitlement_id: EntitlementCandidateText | None = None
    duration_days: Annotated[StrictInt, Field(ge=1, le=9_999)] | None = None
    justification: (
        Annotated[StrictStr, Field(min_length=1, max_length=2_000)] | None
    ) = None

    @field_validator("entitlement_id", mode="before")
    @classmethod
    def normalize_entitlement(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return redact_sensitive_content(value.strip().casefold())

    @field_validator("justification", mode="before")
    @classmethod
    def redact_justification(cls, value: object) -> object:
        return _redact_bounded_text(value)


class SafeToolResult(_StrictFrozenModel):
    """A bounded summary of a whitelist tool result, never its raw payload."""

    tool: SafeToolName
    status: SafeToolStatus
    entitlement_codes: Annotated[list[EntitlementCode], Field(max_length=20)]
    policy_codes: Annotated[list[PolicyCode], Field(max_length=8)]
    match_count: Annotated[StrictInt, Field(ge=0, le=100)]
    summary: Annotated[StrictStr, Field(min_length=1, max_length=1_000)]

    @field_validator("summary", mode="before")
    @classmethod
    def redact_summary(cls, value: object) -> object:
        return _redact_bounded_text(value)

    @model_validator(mode="after")
    def require_unique_codes(self) -> Self:
        if len(set(self.entitlement_codes)) != len(self.entitlement_codes):
            raise ValueError("entitlement_codes must be unique")
        if len(set(self.policy_codes)) != len(self.policy_codes):
            raise ValueError("policy_codes must be unique")
        return self


class SafeRecoverableError(_StrictFrozenModel):
    """Stable public failure facts with no exception or provider material."""

    code: Annotated[
        StrictStr,
        Field(min_length=1, max_length=80, pattern=r"^[A-Z][A-Z0-9_]*$"),
    ]
    message: Annotated[StrictStr, Field(min_length=1, max_length=1_000)]

    @field_validator("message", mode="before")
    @classmethod
    def redact_message(cls, value: object) -> object:
        return _redact_bounded_text(value)


class GraphState(_StrictFrozenModel):
    """The complete and exclusive durable state contract for flow v2."""

    schema_version: Literal[1]
    flow_version: Literal[2]
    workspace_ref: UUID
    graph_run_id: UUID
    input_seq: Annotated[StrictInt, Field(ge=0, le=9_223_372_036_854_775_807)]
    input_turn_id: TurnReference
    safe_user_text: Annotated[StrictStr, Field(min_length=1, max_length=10_000)]
    input_kind: InputKind
    intent: ConversationIntent | None
    security_flagged: StrictBool
    selected_route: SelectedRoute
    base_draft_revision: Annotated[StrictInt, Field(ge=0)]
    committed_draft_revision: Annotated[StrictInt, Field(ge=0)] | None
    draft_patch: DraftPatch | None
    missing_fields: Annotated[list[MissingField], Field(max_length=3)]
    phase: GraphPhase
    tool_name: SafeToolName | None
    safe_tool_result: SafeToolResult | None
    policy_status: PolicyStatus | None
    policy_evidence_codes: Annotated[list[PolicyCode], Field(max_length=8)]
    policy_match_count: Annotated[StrictInt, Field(ge=0, le=100)]
    business_status: BusinessStatus
    assistant_message: BoundedMessage | None
    recoverable_error: SafeRecoverableError | None
    pending_input_id: UUID | None
    pending_input_kind: PendingInputKind | None

    @field_validator("safe_user_text", mode="before")
    @classmethod
    def redact_user_text(cls, value: object) -> object:
        return _redact_bounded_text(value)

    @model_validator(mode="after")
    def require_unique_bounded_lists(self) -> Self:
        if len(set(self.missing_fields)) != len(self.missing_fields):
            raise ValueError("missing_fields must be unique")
        if len(set(self.policy_evidence_codes)) != len(self.policy_evidence_codes):
            raise ValueError("policy_evidence_codes must be unique")
        return self


def is_canonical_entitlement_code(value: str) -> bool:
    """Distinguish a resolved catalog code from a bounded natural-language candidate."""

    return re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+", value) is not None
