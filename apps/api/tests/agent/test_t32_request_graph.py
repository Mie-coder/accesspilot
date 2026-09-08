"""T32 request collection graph parity and safe-stop contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.embeddings import DeterministicEmbeddingModel
from accesspilot.agent.production_graph import (
    ConfirmationInterruptRaised,
    GraphInput,
    GraphRuntimeContext,
    ProductionGraph,
    ProductionGraphRunResult,
    build_production_graph,
)
from accesspilot.agent.routing import DeterministicIntentRouter
from accesspilot.agent.state import ConversationPhase
from accesspilot.agent.step_operations import StepExecutionRejected
from accesspilot.agent.structured_reply import (
    CORRECTION_PROMPT,
    MalformedStructuredOutputError,
)
from accesspilot.auth import Principal
from accesspilot.conversation import LegacyConversationOrchestrator, normalized_outcome
from accesspilot.db.models import (
    AccessGrantRecord,
    AccessRequestRecord,
    AgentPendingInputRecord,
    AgentStepExecutionRecord,
    AgentTurnExecutionRecord,
    ApprovalCaseRecord,
    ApprovalStepRecord,
    AuthSessionRecord,
    DecisionPacketRecord,
    ProvisioningAttemptRecord,
    WorkspaceEventRecord,
    WorkspaceRecord,
)
from accesspilot.db.seed import seed_catalog
from accesspilot.db.workspace_store import SqlAlchemyWorkspaceStore
from accesspilot.domain.models import ParsedReply, RequestDraft
from accesspilot.events import ModelQuotaExceededError
from accesspilot.tools.catalog import ToolResult
from accesspilot.tools.policies import PolicyService
from accesspilot.workspaces import WorkspaceService


class StaticReplyModel:
    def __init__(self, reply: ParsedReply) -> None:
        self.reply = reply
        self.calls = 0

    def parse_reply(
        self,
        user_reply: str,
        correction: str | None = None,
    ) -> ParsedReply:
        del user_reply, correction
        self.calls += 1
        return self.reply


class SequencedReplyModel:
    def __init__(self, results: list[ParsedReply | Exception]) -> None:
        self.results = list(results)
        self.calls = 0
        self.corrections: list[str | None] = []

    def parse_reply(
        self,
        user_reply: str,
        correction: str | None = None,
    ) -> ParsedReply:
        del user_reply
        self.calls += 1
        self.corrections.append(correction)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class CallbackReplyModel:
    def __init__(self, callback: object, reply: ParsedReply) -> None:
        self.callback = callback
        self.reply = reply
        self.calls = 0

    def parse_reply(
        self,
        user_reply: str,
        correction: str | None = None,
    ) -> ParsedReply:
        del user_reply, correction
        self.calls += 1
        assert callable(self.callback)
        self.callback()
        return self.reply


@dataclass(frozen=True)
class RequestGraphFixture:
    token: str
    workspace_id: UUID
    graph_input: GraphInput
    context: GraphRuntimeContext
    workspace_service: WorkspaceService


def _request_graph_fixture(
    factory: sessionmaker[Session],
    *,
    model: object,
    draft: RequestDraft | None = None,
    expected_field: str | None = None,
    model_call_limit: int = 20,
    model_calls_used: int = 0,
) -> RequestGraphFixture:
    token = f"t32-graph-{uuid4()}"
    graph_run_id = uuid4()
    input_turn_id = f"turn-{uuid4()}"
    auth_session_id = uuid4()
    with factory() as session:
        seed_catalog(session)
        workspace = WorkspaceRecord(
            token_hash=sha256(token.encode()).hexdigest(),
            actor_id="EMP-001",
            flow_version=2,
            lease_fence=1,
            draft=draft.model_dump(mode="json") if draft is not None else None,
            draft_revision=1 if draft is not None else 0,
            cursor_actor_id="EMP-001" if expected_field is not None else None,
            cursor_auth_session_id=(str(auth_session_id) if expected_field is not None else None),
            cursor_expected_field=expected_field,
            cursor_last_question_kind=expected_field,
            cursor_issued_at=(datetime.now(UTC) if expected_field is not None else None),
            model_call_limit=model_call_limit,
            model_calls_used=model_calls_used,
        )
        session.add(workspace)
        session.flush()
        session.add(
            AuthSessionRecord(
                id=auth_session_id,
                token_hash=sha256(f"auth-{auth_session_id}".encode()).hexdigest(),
                csrf_hash=sha256(f"csrf-{auth_session_id}".encode()).hexdigest(),
                employee_id="EMP-001",
                workspace_id=workspace.id,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        input_event = WorkspaceEventRecord(
            workspace_id=workspace.id,
            event_type="message.user",
            payload={"content": "T32 fixture", "turn_id": input_turn_id},
        )
        session.add(input_event)
        session.flush()
        session.add(
            AgentTurnExecutionRecord(
                workspace_id=workspace.id,
                graph_run_id=graph_run_id,
                checkpoint_thread_id=f"accesspilot:v1.3:{graph_run_id}",
                input_seq=0,
                input_turn_id=input_turn_id,
                input_event_id=input_event.id,
                auth_session_ref=auth_session_id,
                actor_id="EMP-001",
                engine="langgraph",
                attempt=1,
                lease_fence=1,
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                status="running",
                checkpoint_ns="",
            )
        )
        session.commit()
        workspace_id = workspace.id
    workspace_service = WorkspaceService(SqlAlchemyWorkspaceStore(factory))
    graph_input = GraphInput(
        schema_version=1,
        flow_version=2,
        workspace_ref=workspace_id,
        graph_run_id=graph_run_id,
        input_seq=0,
        input_turn_id=input_turn_id,
        safe_user_text="placeholder",
        input_kind="new_input",
    )
    context: GraphRuntimeContext = {
        "session_factory": factory,
        "workspace_service": workspace_service,
        "policy_service": PolicyService(embedding_model=DeterministicEmbeddingModel()),
        "structured_reply_model": model,
        "intent_router": DeterministicIntentRouter(),
        "principal": Principal(
            employee_id="EMP-001",
            name="林晓",
            department="数据平台部",
            roles=("analyst",),
        ),
        "current_turn_id": input_turn_id,
        "current_fence": 1,
        "workspace_token": token,
        "auth_session_id": str(auth_session_id),
        "cookie": "runtime-cookie",
        "csrf_token": "runtime-csrf",
        "api_key": "runtime-key",
        # T34: the caller pre-allocates the confirmation pending id into the
        # runtime context; the interrupt node only reads it.
        "pending_input_id": str(uuid4()),
    }
    return RequestGraphFixture(
        token,
        workspace_id,
        graph_input,
        context,
        workspace_service,
    )


def _legacy_fixture(
    factory: sessionmaker[Session],
    *,
    model: object,
    draft: RequestDraft | None = None,
    expected_field: str | None = None,
    model_call_limit: int = 20,
    model_calls_used: int = 0,
) -> tuple[str, LegacyConversationOrchestrator]:
    token = f"t32-legacy-{uuid4()}"
    with factory() as session:
        seed_catalog(session)
        workspace = WorkspaceRecord(
            token_hash=sha256(token.encode()).hexdigest(),
            actor_id="EMP-001",
            draft=draft.model_dump(mode="json") if draft is not None else None,
            draft_revision=1 if draft is not None else 0,
            cursor_actor_id="EMP-001" if expected_field is not None else None,
            cursor_auth_session_id=("t32-legacy-auth" if expected_field is not None else None),
            cursor_expected_field=expected_field,
            cursor_last_question_kind=expected_field,
            cursor_issued_at=(datetime.now(UTC) if expected_field is not None else None),
            model_call_limit=model_call_limit,
            model_calls_used=model_calls_used,
        )
        session.add(workspace)
        session.commit()
    service = WorkspaceService(SqlAlchemyWorkspaceStore(factory))
    return token, LegacyConversationOrchestrator(
        session_factory=factory,
        workspace_service=service,
        model=model,
    )


def _stream_path(
    graph: ProductionGraph,
    graph_input: GraphInput,
    context: GraphRuntimeContext,
) -> list[str]:
    path: list[str] = []
    for update in graph.stream(graph_input, context=context, stream_mode="updates"):
        assert isinstance(update, dict) and len(update) == 1
        name = next(iter(update))
        # T34: a stopped confirmation interrupt surfaces as __interrupt__ and
        # belongs to the await_requester_confirmation node in the path contract.
        path.append("await_requester_confirmation" if name == "__interrupt__" else name)
    return path


@dataclass(frozen=True)
class RequestParityScenario:
    name: str
    content: str
    results: tuple[ParsedReply | Exception, ...]
    expected_path: tuple[str, ...]
    draft: RequestDraft | None = None
    expected_field: str | None = None


_PARITY_PATH_ASK_MISSING = (
    "hydrate_authoritative_snapshot",
    "route_intent",
    "parse_request_patch",
    "merge_candidate",
    "persist_draft_cas",
    "validate_draft",
    "ask_missing_field",
    "finalize_public_outcome",
)
_PARITY_PATH_AWAIT_CONFIRMATION = (
    "hydrate_authoritative_snapshot",
    "route_intent",
    "parse_request_patch",
    "resolve_entitlement",
    "merge_candidate",
    "persist_draft_cas",
    "validate_draft",
    "await_requester_confirmation",
)
_PARITY_PATH_AWAIT_NO_RESOLVE = (
    "hydrate_authoritative_snapshot",
    "route_intent",
    "parse_request_patch",
    "merge_candidate",
    "persist_draft_cas",
    "validate_draft",
    "await_requester_confirmation",
)
_PARITY_PATH_RECOVERABLE = (
    "hydrate_authoritative_snapshot",
    "route_intent",
    "parse_request_patch",
    "merge_candidate",
    "persist_draft_cas",
    "validate_draft",
    "compose_recoverable_answer",
    "finalize_public_outcome",
)
_PARITY_PATH_RECOVERABLE_RESOLVED = (
    "hydrate_authoritative_snapshot",
    "route_intent",
    "parse_request_patch",
    "resolve_entitlement",
    "merge_candidate",
    "persist_draft_cas",
    "validate_draft",
    "compose_recoverable_answer",
    "finalize_public_outcome",
)
_PARITY_PATH_NUMERIC_DURATION = (
    "hydrate_authoritative_snapshot",
    "route_intent",
    "handle_numeric_followup",
    "finalize_public_outcome",
)


REQUEST_PARITY_SCENARIOS = (
    RequestParityScenario(
        "missing_fields",
        "申请 14 天",
        (ParsedReply(duration_days=14),),
        _PARITY_PATH_ASK_MISSING,
    ),
    RequestParityScenario(
        "canonical_eligible",
        "申请 insighthub.dashboard_view 14 天，用于季度数据核对",
        (
            ParsedReply(
                employee_id="EMP-999",
                entitlement_id="insighthub.dashboard_view",
                duration_days=14,
                justification="季度数据核对",
                confirmed=True,
            ),
        ),
        _PARITY_PATH_AWAIT_CONFIRMATION,
    ),
    RequestParityScenario(
        "alias_matched",
        "申请客户数据导出 14 天，用于季度数据核对",
        (
            ParsedReply(
                entitlement_id="客户数据导出",
                duration_days=14,
                justification="季度数据核对",
            ),
        ),
        _PARITY_PATH_AWAIT_CONFIRMATION,
    ),
    RequestParityScenario(
        "old_draft_without_current_entitlement",
        "申请 14 天，用于季度数据核对",
        (ParsedReply(duration_days=14, justification="季度数据核对"),),
        _PARITY_PATH_AWAIT_NO_RESOLVE,
        draft=RequestDraft(
            employee_id="EMP-001",
            entitlement_id="insighthub.dashboard_view",
            confirmed=False,
        ),
    ),
    RequestParityScenario(
        "safe_justification_cursor",
        "用于季度数据核对",
        (),
        _PARITY_PATH_AWAIT_NO_RESOLVE,
        draft=RequestDraft(
            employee_id="EMP-001",
            entitlement_id="insighthub.dashboard_view",
            duration_days=14,
            confirmed=False,
        ),
        expected_field="justification",
    ),
    *(
        RequestParityScenario(
            f"short_justification_{index}",
            reason,
            (),
            _PARITY_PATH_AWAIT_NO_RESOLVE,
            draft=RequestDraft(
                employee_id="EMP-001",
                entitlement_id="insighthub.dashboard_view",
                duration_days=120,
                confirmed=False,
            ),
            expected_field="justification",
        )
        for index, reason in enumerate(("演示需要", "季度汇报", "给新人培训"))
    ),
    RequestParityScenario(
        "ambiguous_entitlement",
        "申请 insighthub 权限",
        (ParsedReply(entitlement_id="insighthub"),),
        _PARITY_PATH_RECOVERABLE_RESOLVED,
    ),
    RequestParityScenario(
        "canonical_but_ineligible",
        "申请 insighthub.raw_customer_export",
        (ParsedReply(entitlement_id="insighthub.raw_customer_export"),),
        _PARITY_PATH_RECOVERABLE_RESOLVED,
    ),
    RequestParityScenario(
        "invalid_resolver_argument",
        "申请空白权限",
        (ParsedReply(entitlement_id="   "),),
        _PARITY_PATH_RECOVERABLE,
    ),
    RequestParityScenario(
        "validation_failed",
        "用于季度数据核对",
        (ParsedReply(justification="季度数据核对"),),
        _PARITY_PATH_RECOVERABLE,
        draft=RequestDraft(
            employee_id="EMP-001",
            entitlement_id="insighthub.customer_export",
            duration_days=31,
            confirmed=False,
        ),
    ),
    RequestParityScenario(
        "malformed_then_retry_success",
        "申请仪表盘查看 14 天，用于季度数据核对",
        (
            MalformedStructuredOutputError("bad primary"),
            ParsedReply(
                entitlement_id="仪表盘查看",
                duration_days=14,
                justification="季度数据核对",
            ),
        ),
        _PARITY_PATH_AWAIT_CONFIRMATION,
    ),
    RequestParityScenario(
        "malformed_twice",
        "申请仪表盘查看",
        (
            MalformedStructuredOutputError("bad primary"),
            MalformedStructuredOutputError("bad correction"),
        ),
        _PARITY_PATH_RECOVERABLE,
    ),
    RequestParityScenario(
        "provider_http_failure",
        "申请仪表盘查看",
        (httpx.ReadTimeout("provider timeout"),),
        _PARITY_PATH_RECOVERABLE,
    ),
)


def _cursor_projection(
    workspace_service: WorkspaceService,
    token: str,
    auth_session_id: str,
) -> tuple[str, int] | None:
    cursor = workspace_service.get(
        token,
        auth_session_id=auth_session_id,
    ).active_cursor()
    return (cursor.expected_field, cursor.draft_revision) if cursor is not None else None


def _assert_no_t32_downstream_writes(
    factory: sessionmaker[Session],
    workspace_id: UUID,
) -> None:
    with factory() as session:
        for model in (
            AccessRequestRecord,
            ApprovalCaseRecord,
            ApprovalStepRecord,
            AccessGrantRecord,
            ProvisioningAttemptRecord,
            AgentPendingInputRecord,
        ):
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.workspace_id == workspace_id)
                )
                == 0
            )
        assert (
            session.scalar(
                select(func.count())
                .select_from(DecisionPacketRecord)
                .join(
                    AccessRequestRecord,
                    DecisionPacketRecord.request_id == AccessRequestRecord.id,
                )
                .where(AccessRequestRecord.workspace_id == workspace_id)
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(WorkspaceEventRecord)
                .where(WorkspaceEventRecord.workspace_id == workspace_id)
            )
            == 1
        )


def _workspace_quota_and_steps(
    factory: sessionmaker[Session],
    workspace_id: UUID,
) -> tuple[int, int, int]:
    with factory() as session:
        workspace = session.get(WorkspaceRecord, workspace_id)
        assert workspace is not None
        steps = session.scalar(
            select(func.count())
            .select_from(AgentStepExecutionRecord)
            .where(AgentStepExecutionRecord.workspace_id == workspace_id)
        )
        assert steps is not None
        return (
            workspace.model_calls_used,
            workspace.model_retry_consumed,
            steps,
        )


@pytest.mark.parametrize("scenario", REQUEST_PARITY_SCENARIOS, ids=lambda item: item.name)
def test_request_collection_normalized_outcome_phase_quota_and_cursor_parity_twice(
    database_session_factory: sessionmaker[Session],
    scenario: RequestParityScenario,
) -> None:
    """Twelve fresh scenarios run twice: 24/24 exact path + outcome comparisons."""

    for _ in range(2):
        graph_model = SequencedReplyModel(list(scenario.results))
        legacy_model = SequencedReplyModel(list(scenario.results))
        fixture = _request_graph_fixture(
            database_session_factory,
            model=graph_model,
            draft=scenario.draft,
            expected_field=scenario.expected_field,
        )
        legacy_token, legacy = _legacy_fixture(
            database_session_factory,
            model=legacy_model,
            draft=scenario.draft,
            expected_field=scenario.expected_field,
        )
        graph_input = fixture.graph_input.model_copy(update={"safe_user_text": scenario.content})

        # Every round also locks the real compiled-stream node sequence, so the
        # matrix claims path + outcome parity instead of outcome-only parity.
        path_fixture = _request_graph_fixture(
            database_session_factory,
            model=SequencedReplyModel(list(scenario.results)),
            draft=scenario.draft,
            expected_field=scenario.expected_field,
        )
        assert _stream_path(
            build_production_graph(checkpointer=False),
            path_fixture.graph_input.model_copy(
                update={"safe_user_text": scenario.content}
            ),
            path_fixture.context,
        ) == list(scenario.expected_path)

        graph_result: ProductionGraphRunResult | None = None
        if scenario.expected_path[-1] == "await_requester_confirmation":
            # T34: a complete draft stops at the confirmation interrupt; the
            # pending/Cursor projection is a caller-side fenced transaction.
            with pytest.raises(ConfirmationInterruptRaised) as raised:
                build_production_graph(checkpointer=False).prepare(
                    graph_input,
                    context=fixture.context,
                )
            payload = raised.value.payload
            assert payload["kind"] == "confirmation"
            assert payload["pending_input_id"]
            # The interrupt reports the authoritative revision committed by the
            # upstream T32 draft CAS (persist may have advanced it).
            with database_session_factory() as session:
                workspace = session.get(WorkspaceRecord, fixture.workspace_id)
                assert workspace is not None
            assert payload["draft_revision"] == workspace.draft_revision
        else:
            graph_result = build_production_graph(checkpointer=False).prepare(
                graph_input,
                context=fixture.context,
            )
        legacy_result = legacy.prepare(
            workspace_token=legacy_token,
            content=scenario.content,
            turn_id=f"turn-t32-{scenario.name}-{uuid4()}",
            auth_session_id="t32-legacy-auth",
        )

        if scenario.name.startswith("short_justification_"):
            assert legacy_result.turn.draft.justification == scenario.content
            assert legacy_result.turn.draft.duration_days == 120
            assert legacy_result.turn.draft.confirmed is False
            with database_session_factory() as session:
                persisted = session.get(WorkspaceRecord, fixture.workspace_id)
                assert persisted is not None and persisted.draft is not None
                assert persisted.draft["justification"] == scenario.content
                assert persisted.draft["duration_days"] == 120
                assert persisted.draft["confirmed"] is False

        if graph_result is None:
            # Legacy parity at the await boundary: the confirmation card.
            assert legacy_result.turn.business_status == "awaiting_confirmation"
            assert legacy_result.turn.phase == ConversationPhase.AWAITING_CONFIRMATION
        else:
            assert normalized_outcome(graph_result.turn) == normalized_outcome(legacy_result.turn)
            assert graph_result.turn.phase == legacy_result.turn.phase
            assert graph_result.turn.quota == legacy_result.turn.quota
        # The correction argument is provable: only the second attempt after a
        # malformed primary carries CORRECTION_PROMPT; every other call is None.
        expected_corrections: list[str | None] = []
        if scenario.results:
            expected_corrections.append(None)
            if (
                len(scenario.results) > 1
                and isinstance(scenario.results[0], MalformedStructuredOutputError)
            ):
                expected_corrections.append(CORRECTION_PROMPT)
        assert graph_model.calls == legacy_model.calls == len(expected_corrections)
        assert graph_model.corrections == legacy_model.corrections == expected_corrections
        assert _cursor_projection(
            fixture.workspace_service,
            fixture.token,
            fixture.context["auth_session_id"],
        ) == _cursor_projection(
            legacy.workspace_service,
            legacy_token,
            "t32-legacy-auth",
        )
        _assert_no_t32_downstream_writes(
            database_session_factory,
            fixture.workspace_id,
        )

        if graph_result is None:
            # No Cursor is projected before the caller's fenced interrupt
            # finalize transaction (T34 AC2) and the graph created none.
            assert (
                _cursor_projection(
                    fixture.workspace_service,
                    fixture.token,
                    fixture.context["auth_session_id"],
                )
                is None
            )
        else:
            graph_result.finalize_success()
            if graph_result.turn.business_status == "collecting":
                assert _cursor_projection(
                    fixture.workspace_service,
                    fixture.token,
                    fixture.context["auth_session_id"],
                ) == (
                    graph_result.turn.missing_fields[0],
                    graph_result.turn.draft_revision,
                )
            else:
                assert (
                    _cursor_projection(
                        fixture.workspace_service,
                        fixture.token,
                        fixture.context["auth_session_id"],
                    )
                    is None
                )


def test_revision_race_matches_legacy_twice_and_leaves_latest_draft_authoritative(
    database_session_factory: sessionmaker[Session],
) -> None:
    content = "申请仪表盘查看 14 天，用于季度数据核对"
    reply = ParsedReply(
        entitlement_id="insighthub.dashboard_view",
        duration_days=14,
        justification="季度数据核对",
    )
    concurrent_draft = RequestDraft(
        employee_id="EMP-001",
        entitlement_id="insighthub.dashboard_view",
        confirmed=False,
    )
    for _ in range(2):
        fixture = _request_graph_fixture(
            database_session_factory,
            model=StaticReplyModel(reply),
        )
        legacy_token, legacy = _legacy_fixture(
            database_session_factory,
            model=StaticReplyModel(reply),
        )
        fixture.context["structured_reply_model"] = CallbackReplyModel(
            lambda fixture=fixture: fixture.workspace_service.save_draft_cas(
                fixture.token,
                expected_revision=0,
                draft=concurrent_draft,
                auth_session_id=fixture.context["auth_session_id"],
            ),
            reply,
        )
        legacy.model = CallbackReplyModel(
            lambda legacy=legacy, legacy_token=legacy_token: (
                legacy.workspace_service.save_draft_cas(
                    legacy_token,
                    expected_revision=0,
                    draft=concurrent_draft,
                    auth_session_id="t32-legacy-auth",
                )
            ),
            reply,
        )

        # Every round also locks the compiled-stream node sequence: the race
        # must surface as parse -> resolve -> merge -> persist conflict ->
        # recoverable, not as a result-only coincidence.
        path_fixture = _request_graph_fixture(
            database_session_factory,
            model=StaticReplyModel(reply),
        )
        path_fixture.context["structured_reply_model"] = CallbackReplyModel(
            lambda fixture=path_fixture: fixture.workspace_service.save_draft_cas(
                fixture.token,
                expected_revision=0,
                draft=concurrent_draft,
                auth_session_id=fixture.context["auth_session_id"],
            ),
            reply,
        )
        assert _stream_path(
            build_production_graph(checkpointer=False),
            path_fixture.graph_input.model_copy(
                update={"safe_user_text": content}
            ),
            path_fixture.context,
        ) == list(_PARITY_PATH_RECOVERABLE_RESOLVED)

        graph_turn = build_production_graph(checkpointer=False).invoke(
            fixture.graph_input.model_copy(update={"safe_user_text": content}),
            context=fixture.context,
        )
        legacy_turn = legacy.prepare(
            workspace_token=legacy_token,
            content=content,
            turn_id=f"turn-t32-race-{uuid4()}",
            auth_session_id="t32-legacy-auth",
        ).turn

        assert normalized_outcome(graph_turn) == normalized_outcome(legacy_turn)
        assert graph_turn.phase == legacy_turn.phase
        assert graph_turn.quota == legacy_turn.quota
        assert graph_turn.error_code == "DRAFT_REVISION_CONFLICT"
        assert graph_turn.draft == concurrent_draft
        _assert_no_t32_downstream_writes(
            database_session_factory,
            fixture.workspace_id,
        )


def test_legal_numeric_duration_matches_legacy_twice_and_replays_before_cursor(
    database_session_factory: sessionmaker[Session],
) -> None:
    existing = RequestDraft(
        employee_id="EMP-001",
        entitlement_id="insighthub.customer_export",
        confirmed=False,
    )
    for _ in range(2):
        graph_model = SequencedReplyModel([])
        legacy_model = SequencedReplyModel([])
        fixture = _request_graph_fixture(
            database_session_factory,
            model=graph_model,
            draft=existing,
            expected_field="duration_days",
        )
        legacy_token, legacy = _legacy_fixture(
            database_session_factory,
            model=legacy_model,
            draft=existing,
            expected_field="duration_days",
        )
        graph = build_production_graph(checkpointer=False)
        graph_input = fixture.graph_input.model_copy(update={"safe_user_text": "14"})

        # Every round also locks the compiled-stream node sequence for the
        # legal numeric path: hydrate -> route -> handle_numeric_followup ->
        # finalize, not a result-only comparison.
        path_fixture = _request_graph_fixture(
            database_session_factory,
            model=SequencedReplyModel([]),
            draft=existing,
            expected_field="duration_days",
        )
        assert _stream_path(
            build_production_graph(checkpointer=False),
            path_fixture.graph_input.model_copy(update={"safe_user_text": "14"}),
            path_fixture.context,
        ) == list(_PARITY_PATH_NUMERIC_DURATION)

        first = graph.prepare(graph_input, context=fixture.context)
        legacy_turn = legacy.prepare(
            workspace_token=legacy_token,
            content="14",
            turn_id=f"turn-t32-numeric-{uuid4()}",
            auth_session_id="t32-legacy-auth",
        ).turn
        replay = graph.prepare(graph_input, context=fixture.context)

        assert normalized_outcome(first.turn) == normalized_outcome(legacy_turn)
        assert normalized_outcome(replay.turn) == normalized_outcome(first.turn)
        assert first.turn.phase == legacy_turn.phase
        assert first.turn.quota == legacy_turn.quota
        assert graph_model.calls == legacy_model.calls == 0
        assert (
            _cursor_projection(
                fixture.workspace_service,
                fixture.token,
                fixture.context["auth_session_id"],
            )
            is None
        )
        first.finalize_success()
        replay.finalize_success()
        assert _cursor_projection(
            fixture.workspace_service,
            fixture.token,
            fixture.context["auth_session_id"],
        ) == ("justification", 2)
        with database_session_factory() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AgentStepExecutionRecord)
                    .where(
                        AgentStepExecutionRecord.workspace_id == fixture.workspace_id,
                        AgentStepExecutionRecord.step_key == "persist_numeric_duration",
                    )
                )
                == 1
            )
        _assert_no_t32_downstream_writes(
            database_session_factory,
            fixture.workspace_id,
        )


@pytest.mark.parametrize(
    ("name", "results", "limit", "used", "expected_calls", "expected_facts"),
    [
        (
            "primary_exhausted",
            [ParsedReply(duration_days=14)],
            1,
            1,
            0,
            (1, 0, 0),
        ),
        (
            "one_remaining_primary_malformed",
            [MalformedStructuredOutputError("bad primary")],
            2,
            1,
            1,
            (2, 0, 1),
        ),
    ],
)
def test_quota_exception_matrix_matches_legacy_twice_without_partial_writes(
    database_session_factory: sessionmaker[Session],
    name: str,
    results: list[ParsedReply | Exception],
    limit: int,
    used: int,
    expected_calls: int,
    expected_facts: tuple[int, int, int],
) -> None:
    """Two quota scenarios run on fresh fixtures twice: 4/4 exact failures."""

    for _ in range(2):
        graph_model = SequencedReplyModel(list(results))
        legacy_model = SequencedReplyModel(list(results))
        fixture = _request_graph_fixture(
            database_session_factory,
            model=graph_model,
            model_call_limit=limit,
            model_calls_used=used,
        )
        legacy_token, legacy = _legacy_fixture(
            database_session_factory,
            model=legacy_model,
            model_call_limit=limit,
            model_calls_used=used,
        )
        graph_input = fixture.graph_input.model_copy(
            update={"safe_user_text": "申请仪表盘查看 14 天"}
        )

        with pytest.raises(ModelQuotaExceededError):
            build_production_graph(checkpointer=False).invoke(
                graph_input,
                context=fixture.context,
            )
        with pytest.raises(ModelQuotaExceededError):
            legacy.prepare(
                workspace_token=legacy_token,
                content=graph_input.safe_user_text,
                turn_id=f"turn-t32-quota-{name}-{uuid4()}",
                auth_session_id="t32-legacy-auth",
            )

        assert graph_model.calls == legacy_model.calls == expected_calls
        assert (
            _workspace_quota_and_steps(
                database_session_factory,
                fixture.workspace_id,
            )
            == expected_facts
        )
        graph_workspace = fixture.workspace_service.get(
            fixture.token,
            auth_session_id=fixture.context["auth_session_id"],
        )
        legacy_workspace = legacy.workspace_service.get(
            legacy_token,
            auth_session_id="t32-legacy-auth",
        )
        assert graph_workspace.draft == legacy_workspace.draft is None
        assert graph_workspace.draft_revision == legacy_workspace.draft_revision == 0
        assert graph_workspace.active_cursor() is None
        _assert_no_t32_downstream_writes(
            database_session_factory,
            fixture.workspace_id,
        )


def test_resolver_unavailable_matches_legacy_without_leaking_internal_failure(
    database_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*args: object, **kwargs: object) -> ToolResult:
        del args, kwargs
        return ToolResult(status="workspace_not_found")

    monkeypatch.setattr(
        "accesspilot.agent.production_graph.execute_read_only_tool",
        unavailable,
    )
    monkeypatch.setattr(
        "accesspilot.conversation.execute_read_only_tool",
        unavailable,
    )
    content = "申请仪表盘查看"
    reply = ParsedReply(entitlement_id="仪表盘查看")
    for _ in range(2):
        fixture = _request_graph_fixture(
            database_session_factory,
            model=StaticReplyModel(reply),
        )
        legacy_token, legacy = _legacy_fixture(
            database_session_factory,
            model=StaticReplyModel(reply),
        )

        graph_turn = build_production_graph(checkpointer=False).invoke(
            fixture.graph_input.model_copy(update={"safe_user_text": content}),
            context=fixture.context,
        )
        legacy_turn = legacy.prepare(
            workspace_token=legacy_token,
            content=content,
            turn_id=f"turn-t32-resolver-unavailable-{uuid4()}",
            auth_session_id="t32-legacy-auth",
        ).turn

        assert normalized_outcome(graph_turn) == normalized_outcome(legacy_turn)
        assert graph_turn.business_status == "resolution_unavailable"
        assert graph_turn.draft_revision == 0
        assert "workspace_not_found" not in graph_turn.assistant_message
        _assert_no_t32_downstream_writes(
            database_session_factory,
            fixture.workspace_id,
        )


def test_missing_running_execution_fails_before_model_quota_or_draft(
    database_session_factory: sessionmaker[Session],
) -> None:
    model = StaticReplyModel(ParsedReply(duration_days=14))
    fixture = _request_graph_fixture(database_session_factory, model=model)
    with database_session_factory() as session:
        execution = session.scalar(
            select(AgentTurnExecutionRecord).where(
                AgentTurnExecutionRecord.workspace_id == fixture.workspace_id
            )
        )
        assert execution is not None
        session.delete(execution)
        session.commit()

    with pytest.raises(StepExecutionRejected, match="execution binding"):
        build_production_graph(checkpointer=False).invoke(
            fixture.graph_input.model_copy(update={"safe_user_text": "申请 14 天"}),
            context=fixture.context,
        )

    assert model.calls == 0
    assert _workspace_quota_and_steps(
        database_session_factory,
        fixture.workspace_id,
    ) == (0, 0, 0)
    workspace = fixture.workspace_service.get(
        fixture.token,
        auth_session_id=fixture.context["auth_session_id"],
    )
    assert workspace.draft is None and workspace.draft_revision == 0
    _assert_no_t32_downstream_writes(
        database_session_factory,
        fixture.workspace_id,
    )


@pytest.mark.parametrize(
    ("results", "expected_calls", "expected_quota"),
    [
        (
            [
                ParsedReply(entitlement_id="insighthub.raw_customer_export"),
                ParsedReply(entitlement_id="insighthub.raw_customer_export"),
            ],
            2,
            (1, 0, 1),
        ),
        (
            [
                MalformedStructuredOutputError("primary one"),
                MalformedStructuredOutputError("retry one"),
                MalformedStructuredOutputError("primary replay"),
                MalformedStructuredOutputError("retry replay"),
            ],
            4,
            (2, 1, 2),
        ),
    ],
)
def test_no_draft_replay_is_provider_at_least_once_without_quota_recharge(
    database_session_factory: sessionmaker[Session],
    results: list[ParsedReply | Exception],
    expected_calls: int,
    expected_quota: tuple[int, int, int],
) -> None:
    model = SequencedReplyModel(results)
    fixture = _request_graph_fixture(database_session_factory, model=model)
    graph_input = fixture.graph_input.model_copy(
        update={"safe_user_text": "申请 insighthub.raw_customer_export"}
    )
    graph = build_production_graph(checkpointer=False)

    first = graph.invoke(graph_input, context=fixture.context)
    replay = graph.invoke(graph_input, context=fixture.context)

    assert normalized_outcome(replay) == normalized_outcome(first)
    assert model.calls == expected_calls
    assert _workspace_quota_and_steps(
        database_session_factory,
        fixture.workspace_id,
    ) == expected_quota
    assert first.draft_revision == replay.draft_revision == 0
    _assert_no_t32_downstream_writes(
        database_session_factory,
        fixture.workspace_id,
    )


@pytest.mark.parametrize(
    "provider_error",
    [httpx.ReadTimeout("HTTP timeout"), TimeoutError("provider timeout")],
)
def test_http_and_timeout_fail_closed_without_correction_retry(
    database_session_factory: sessionmaker[Session],
    provider_error: Exception,
) -> None:
    graph_model = SequencedReplyModel([provider_error])
    legacy_model = SequencedReplyModel([provider_error])
    fixture = _request_graph_fixture(database_session_factory, model=graph_model)
    legacy_token, legacy = _legacy_fixture(
        database_session_factory,
        model=legacy_model,
    )
    content = "申请仪表盘查看"

    graph_turn = build_production_graph(checkpointer=False).invoke(
        fixture.graph_input.model_copy(update={"safe_user_text": content}),
        context=fixture.context,
    )
    legacy_turn = legacy.prepare(
        workspace_token=legacy_token,
        content=content,
        turn_id=f"turn-t32-timeout-{uuid4()}",
        auth_session_id="t32-legacy-auth",
    ).turn

    assert normalized_outcome(graph_turn) == normalized_outcome(legacy_turn)
    assert graph_model.calls == legacy_model.calls == 1
    assert graph_model.corrections == legacy_model.corrections == [None]
    assert _workspace_quota_and_steps(
        database_session_factory,
        fixture.workspace_id,
    ) == (1, 0, 1)


def test_missing_request_matches_legacy_and_activates_cursor_only_after_finalizer(
    database_session_factory: sessionmaker[Session],
) -> None:
    graph_model = StaticReplyModel(ParsedReply(duration_days=14))
    legacy_model = StaticReplyModel(ParsedReply(duration_days=14))
    fixture = _request_graph_fixture(
        database_session_factory,
        model=graph_model,
    )
    legacy_token, legacy = _legacy_fixture(
        database_session_factory,
        model=legacy_model,
    )
    content = "申请 14 天"
    graph_input = fixture.graph_input.model_copy(update={"safe_user_text": content})
    graph = build_production_graph(checkpointer=False)

    result = graph.prepare(graph_input, context=fixture.context)
    legacy_result = legacy.prepare(
        workspace_token=legacy_token,
        content=content,
        turn_id="turn-t32-legacy-missing",
        auth_session_id="t32-legacy-auth",
    )

    assert normalized_outcome(result.turn) == normalized_outcome(legacy_result.turn)
    assert (
        fixture.workspace_service.get(
            fixture.token,
            auth_session_id=fixture.context["auth_session_id"],
        ).active_cursor()
        is None
    )
    result.finalize_success()
    cursor = fixture.workspace_service.get(
        fixture.token,
        auth_session_id=fixture.context["auth_session_id"],
    ).active_cursor()
    assert cursor is not None and cursor.expected_field == "entitlement_id"


def test_complete_request_stops_at_await_without_interrupt_pending_or_cursor(
    database_session_factory: sessionmaker[Session],
) -> None:
    model = StaticReplyModel(
        ParsedReply(
            entitlement_id="insighthub.dashboard_view",
            duration_days=14,
            justification="T32 完整申请",
            confirmed=True,
        )
    )
    fixture = _request_graph_fixture(database_session_factory, model=model)
    graph = build_production_graph(checkpointer=False)
    graph_input = fixture.graph_input.model_copy(
        update={"safe_user_text": "申请仪表盘查看 14 天，用于 T32 完整申请"}
    )

    path = _stream_path(graph, graph_input, fixture.context)
    assert path == [
        "hydrate_authoritative_snapshot",
        "route_intent",
        "parse_request_patch",
        "resolve_entitlement",
        "merge_candidate",
        "persist_draft_cas",
        "validate_draft",
        "await_requester_confirmation",
    ]
    with pytest.raises(ConfirmationInterruptRaised) as raised:
        graph.prepare(graph_input, context=fixture.context)
    payload = raised.value.payload
    assert payload["kind"] == "confirmation"
    assert payload["draft_revision"] == 1
    assert model.calls == 1
    with database_session_factory() as session:
        workspace = session.get(WorkspaceRecord, fixture.workspace_id)
        assert workspace is not None
        assert workspace.draft is not None
        assert workspace.draft["confirmed"] is False
    # The graph itself must not create pending/Cursor: those belong to the
    # caller's fenced interrupt finalize transaction (T34 AC2).
    assert (
        fixture.workspace_service.get(
            fixture.token,
            auth_session_id=fixture.context["auth_session_id"],
        ).active_cursor()
        is None
    )
    with database_session_factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(AgentPendingInputRecord)
                .where(AgentPendingInputRecord.workspace_id == fixture.workspace_id)
            )
            == 0
        )


def test_long_justification_is_authoritative_in_workspace_not_step_reference(
    database_session_factory: sessionmaker[Session],
) -> None:
    justification = "业" * 2_000
    model = StaticReplyModel(
        ParsedReply(
            entitlement_id="insighthub.dashboard_view",
            duration_days=14,
            justification=justification,
        )
    )
    fixture = _request_graph_fixture(database_session_factory, model=model)
    graph_input = fixture.graph_input.model_copy(
        update={"safe_user_text": "申请仪表盘查看 14 天"}
    )
    graph = build_production_graph(checkpointer=False)

    for _ in range(2):
        with pytest.raises(ConfirmationInterruptRaised) as raised:
            graph.invoke(graph_input, context=fixture.context)
        assert raised.value.payload["kind"] == "confirmation"
        assert raised.value.payload["draft_revision"] == 1
    assert model.calls == 1
    with database_session_factory() as session:
        workspace = session.get(WorkspaceRecord, fixture.workspace_id)
        assert workspace is not None
        assert workspace.draft is not None
        assert workspace.draft["justification"] == justification
    with database_session_factory() as session:
        references = session.scalars(
            select(AgentStepExecutionRecord.result_reference).where(
                AgentStepExecutionRecord.workspace_id == fixture.workspace_id
            )
        ).all()
    assert len(references) == 2
    assert references.count("quota_consumed") == references.count(None) == 1
    assert all(
        reference is None or justification not in reference for reference in references
    )


def test_finalizer_projects_missing_cursor_once_and_replays_idempotently(
    database_session_factory: sessionmaker[Session],
) -> None:
    model = StaticReplyModel(ParsedReply(duration_days=14))
    fixture = _request_graph_fixture(database_session_factory, model=model)
    graph_input = fixture.graph_input.model_copy(
        update={"safe_user_text": "申请 14 天"}
    )
    graph = build_production_graph(checkpointer=False)

    first = graph.prepare(graph_input, context=fixture.context)
    replay = graph.prepare(graph_input, context=fixture.context)
    assert (
        _cursor_projection(
            fixture.workspace_service,
            fixture.token,
            fixture.context["auth_session_id"],
        )
        is None
    )

    first.finalize_success()
    with database_session_factory() as session:
        workspace = session.get(WorkspaceRecord, fixture.workspace_id)
        assert workspace is not None
        issued_at = workspace.cursor_issued_at
    assert _cursor_projection(
        fixture.workspace_service,
        fixture.token,
        fixture.context["auth_session_id"],
    ) == ("entitlement_id", 1)

    replay.finalize_success()
    assert _cursor_projection(
        fixture.workspace_service,
        fixture.token,
        fixture.context["auth_session_id"],
    ) == ("entitlement_id", 1)
    with database_session_factory() as session:
        workspace = session.get(WorkspaceRecord, fixture.workspace_id)
        assert workspace is not None and workspace.cursor_issued_at == issued_at
        assert (
            session.scalar(
                select(func.count())
                .select_from(AgentStepExecutionRecord)
                .where(
                    AgentStepExecutionRecord.workspace_id == fixture.workspace_id,
                    AgentStepExecutionRecord.step_key == "activate_missing_cursor",
                )
            )
            == 1
        )


def test_finalizer_rejects_stale_execution_fence_without_activating_cursor(
    database_session_factory: sessionmaker[Session],
) -> None:
    model = StaticReplyModel(ParsedReply(duration_days=14))
    fixture = _request_graph_fixture(database_session_factory, model=model)
    graph_input = fixture.graph_input.model_copy(
        update={"safe_user_text": "申请 14 天"}
    )
    result = build_production_graph(checkpointer=False).prepare(
        graph_input,
        context=fixture.context,
    )

    with database_session_factory() as session:
        execution = session.scalar(
            select(AgentTurnExecutionRecord).where(
                AgentTurnExecutionRecord.workspace_id == fixture.workspace_id
            )
        )
        assert execution is not None
        execution.lease_fence = 2  # 新 owner 已接管该逻辑输入
        session.commit()

    with pytest.raises(StepExecutionRejected, match="execution binding"):
        result.finalize_success()

    assert (
        _cursor_projection(
            fixture.workspace_service,
            fixture.token,
            fixture.context["auth_session_id"],
        )
        is None
    )
    with database_session_factory() as session:
        workspace = session.get(WorkspaceRecord, fixture.workspace_id)
        assert workspace is not None
        assert workspace.cursor_expected_field is None
        assert workspace.cursor_issued_at is None
        assert (
            session.scalar(
                select(func.count())
                .select_from(AgentStepExecutionRecord)
                .where(
                    AgentStepExecutionRecord.workspace_id == fixture.workspace_id,
                    AgentStepExecutionRecord.step_key == "activate_missing_cursor",
                )
            )
            == 0
        )
