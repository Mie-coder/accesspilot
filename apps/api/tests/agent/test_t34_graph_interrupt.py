"""T34 graph wiring: real confirmation interrupt/resume inside the production graph.

These tests drive the compiled production graph (InMemorySaver) end to end and
assert the *graph-level* contract of T34 AC1/AC3: a complete draft stops at a
safe JSON-serializable confirmation interrupt; resume via a single
``Command(resume=...)`` rehydrates and either confirms (CAS once) or re-routes
non-confirm input inside the same graph call.  The fenced application
transactions (finalize_interrupt / begin_resume / finalize resume outcomes)
and real-PostgreSQL recovery proofs live in ``tests/db/test_t34_postgres.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import cast
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.embeddings import DeterministicEmbeddingModel
from accesspilot.agent.production_graph import (
    ConfirmationInterruptRaised,
    GraphInput,
    GraphRuntimeContext,
    build_production_graph,
)
from accesspilot.agent.routing import DeterministicIntentRouter
from accesspilot.auth import Principal
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


@dataclass(frozen=True)
class T34GraphFixture:
    token: str
    workspace_id: UUID
    graph_input: GraphInput
    context: GraphRuntimeContext
    workspace_service: WorkspaceService
    pending_input_id: UUID


def _t34_graph_fixture(
    factory: sessionmaker[Session],
    *,
    model: object,
    draft: RequestDraft | None = None,
    expected_field: str | None = None,
    pending_input_id: UUID | None = None,
    pending: bool = False,
    revoke_auth: bool = False,
    pending_draft_revision: int | None = None,
) -> T34GraphFixture:
    token = f"t34-graph-{uuid4()}"
    graph_run_id = uuid4()
    input_turn_id = f"turn-{uuid4()}"
    auth_session_id = uuid4()
    pending_id = pending_input_id or uuid4()
    with factory() as session:
        seed_catalog(session)
        workspace = WorkspaceRecord(
            token_hash=sha256(token.encode()).hexdigest(),
            actor_id="EMP-001",
            flow_version=2,
            lease_fence=1,
            draft=draft.model_dump(mode="json") if draft is not None else None,
            draft_revision=1 if draft is not None else 0,
            cursor_actor_id=(
                "EMP-001" if expected_field is not None or pending else None
            ),
            cursor_auth_session_id=(
                str(auth_session_id) if expected_field is not None or pending else None
            ),
            cursor_expected_field=expected_field or ("confirmation" if pending else None),
            cursor_last_question_kind=expected_field or ("confirmation" if pending else None),
            cursor_issued_at=(
                datetime.now(UTC) if expected_field is not None or pending else None
            ),
            model_call_limit=20,
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
            payload={"content": "T34 fixture", "turn_id": input_turn_id},
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
        if revoke_auth:
            auth = session.get(AuthSessionRecord, auth_session_id)
            assert auth is not None
            auth.revoked_at = datetime.now(UTC)
        if pending:
            session.add(
                AgentPendingInputRecord(
                    id=uuid4(),
                    workspace_id=workspace.id,
                    agent_thread_id=workspace.agent_thread_id,
                    graph_run_id=graph_run_id,
                    checkpoint_thread_id=f"accesspilot:v1.3:{graph_run_id}",
                    pending_input_id=pending_id,
                    kind="confirmation",
                    draft_revision=(
                        pending_draft_revision
                        if pending_draft_revision is not None
                        else (1 if draft is not None else 0)
                    ),
                    auth_session_ref=auth_session_id,
                    actor_id="EMP-001",
                    engine="langgraph",
                    checkpoint_ns="",
                    accepted_checkpoint_id="0" * 32,
                    status="active",
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
        "pending_input_id": str(pending_id),
    }
    return T34GraphFixture(
        token,
        workspace_id,
        graph_input,
        context,
        workspace_service,
        pending_id,
    )


def _complete_draft() -> RequestDraft:
    return RequestDraft(
        employee_id="EMP-001",
        entitlement_id="insighthub.customer_export",
        duration_days=30,
        justification="业务需要",
        confirmed=False,
    )


_REQUEST_TEXT = "申请客户数据导出 30 天，用于业务需要"


def _with_request_text(fixture: T34GraphFixture) -> GraphInput:
    return fixture.graph_input.model_copy(update={"safe_user_text": _REQUEST_TEXT})


def _complete_reply() -> ParsedReply:
    return ParsedReply(
        entitlement_id="insighthub.customer_export",
        duration_days=30,
        justification="业务需要",
    )


def _config_for(thread_id: str, checkpoint_id: str | None = None) -> dict[str, object]:
    config: dict[str, object] = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
        }
    }
    if checkpoint_id is not None:
        cast(dict[str, object], config["configurable"])["checkpoint_id"] = checkpoint_id
    return config


def _stream_path(
    graph,
    graph_input: GraphInput | Command,
    context: GraphRuntimeContext,
    config: dict[str, object] | None = None,
) -> list[str]:
    path: list[str] = []
    for update in graph.stream(graph_input, config, context=context, stream_mode="updates"):
        assert isinstance(update, dict) and len(update) == 1
        name = next(iter(update))
        # A stopped dynamic interrupt surfaces as the ``__interrupt__`` key;
        # it belongs to the confirmation node in the path contract.
        path.append("await_requester_confirmation" if name == "__interrupt__" else name)
    return path


def _assert_no_business_writes(
    factory: sessionmaker[Session],
    workspace_id: UUID,
    *,
    expected_events: int,
) -> None:
    """AC1: the interrupt node itself creates no formal/pending business rows.

    Upstream T32 facts (draft CAS step ledger, model attempt ledger) are legal;
    the interrupt boundary itself must not add Request/Approval/Grant/pending
    rows or extra events.
    """
    with factory() as session:
        for model in (
            AccessRequestRecord,
            ApprovalCaseRecord,
            ApprovalStepRecord,
            AccessGrantRecord,
            ProvisioningAttemptRecord,
            AgentPendingInputRecord,
        ):
            count = session.scalar(
                select(func.count()).select_from(model).where(model.workspace_id == workspace_id)
            )
            assert count == 0, f"{model.__name__} must stay untouched"
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
            == expected_events
        )


def test_complete_draft_stops_at_confirmation_interrupt_with_safe_payload(
    database_session_factory: sessionmaker[Session],
) -> None:
    """AC1: a complete draft must stop at a confirmation interrupt carrying a
    safe, JSON-serializable payload; the interrupt node itself writes nothing."""
    fixture = _t34_graph_fixture(
        database_session_factory,
        model=StaticReplyModel(_complete_reply()),
        draft=_complete_draft(),
    )
    graph = build_production_graph(checkpointer=InMemorySaver())
    config = _config_for(fixture.graph_input.graph_run_id)
    with pytest.raises(ConfirmationInterruptRaised) as raised:
        graph.invoke(
            _with_request_text(fixture),
            config,
            context=fixture.context,
        )
    payload = raised.value.payload
    assert payload["kind"] == "confirmation"
    assert payload["pending_input_id"] == str(fixture.pending_input_id)
    assert payload["draft_revision"] == 1
    assert "summary" in payload and payload["summary"]
    assert set(payload["allowed_decisions"]) == {"confirm", "route_new_input"}
    # The payload must be plain JSON data (no objects, no secrets).
    json.dumps(payload)

    # The interrupt node must be side-effect free: no business rows, no pending,
    # no model calls beyond the upstream parse, no quota consumption.
    model = fixture.context["structured_reply_model"]
    assert isinstance(model, StaticReplyModel)
    assert model.calls == 1
    _assert_no_business_writes(
        database_session_factory,
        fixture.workspace_id,
        expected_events=1,
    )
    with database_session_factory() as session:
        workspace = session.get(WorkspaceRecord, fixture.workspace_id)
        assert workspace is not None
        # Only the upstream T32 parse consumed quota; the interrupt node itself
        # must not add any model call.
        assert workspace.model_calls_used == 1
        assert workspace.draft_revision == 1
        assert workspace.draft is not None
        assert workspace.draft["confirmed"] is False


def test_resume_confirm_applies_confirmation_cas_exactly_once_in_one_call(
    database_session_factory: sessionmaker[Session],
) -> None:
    """AC3: confirm runs through rehydrate then the confirmation CAS exactly once;
    the draft revision advances once and the graph ends ready_to_submit."""
    fixture = _t34_graph_fixture(
        database_session_factory,
        model=StaticReplyModel(_complete_reply()),
        draft=_complete_draft(),
        pending=True,
    )
    graph = build_production_graph(checkpointer=InMemorySaver())
    config = _config_for(fixture.graph_input.graph_run_id)
    with pytest.raises(ConfirmationInterruptRaised):
        graph.invoke(_with_request_text(fixture), config, context=fixture.context)
    interrupted_at = graph.get_state(config)
    assert len(interrupted_at.tasks) == 1
    interrupt_task = interrupted_at.tasks[0]
    assert interrupt_task.name == "await_requester_confirmation"

    # Single Command(resume): the graph must rehydrate and confirm in this call.
    path = _stream_path(
        graph,
        Command(resume={"decision": "confirm", "safe_user_text": "确认提交"}),
        fixture.context,
        config,
    )
    assert path == [
        "await_requester_confirmation",
        "rehydrate_resume_snapshot",
        "apply_confirmation_cas",
        "ready_to_submit",
        "finalize_public_outcome",
    ]
    final_state = graph.get_state(config)
    assert final_state.values["business_status"] == "ready_to_submit"
    assert final_state.values["phase"] == "ready_to_submit"
    assert final_state.values["committed_draft_revision"] == 2

    with database_session_factory() as session:
        workspace = session.get(WorkspaceRecord, fixture.workspace_id)
        assert workspace is not None
        assert workspace.draft_revision == 2
        assert workspace.draft is not None
        assert workspace.draft["confirmed"] is True
        # The confirmation CAS must be recorded exactly once.
        steps = session.scalars(
            select(AgentStepExecutionRecord).where(
                AgentStepExecutionRecord.workspace_id == fixture.workspace_id,
                AgentStepExecutionRecord.step_key == "apply_confirmation",
            )
        ).all()
        assert len(steps) == 1
        assert steps[0].committed_revision == 2
        assert steps[0].status == "completed"


def test_resume_route_new_input_reclassifies_in_same_call_without_confirmation(
    database_session_factory: sessionmaker[Session],
) -> None:
    """AC3: a non-confirm input (topic change) is re-routed inside the same
    resume call; nothing is confirmed and the old draft stays untouched."""
    fixture = _t34_graph_fixture(
        database_session_factory,
        model=StaticReplyModel(_complete_reply()),
        draft=_complete_draft(),
        pending=True,
    )
    graph = build_production_graph(checkpointer=InMemorySaver())
    config = _config_for(fixture.graph_input.graph_run_id)
    with pytest.raises(ConfirmationInterruptRaised):
        graph.invoke(_with_request_text(fixture), config, context=fixture.context)

    path = _stream_path(
        graph,
        Command(resume={"decision": "route_new_input", "safe_user_text": "查一下政策"}),
        fixture.context,
        config,
    )
    assert path[0] == "await_requester_confirmation"
    assert path[1] == "rehydrate_resume_snapshot"
    assert "route_intent" in path
    assert "retrieve_policy_pgvector" in path
    assert path[-1] == "finalize_public_outcome"

    final_state = graph.get_state(config)
    assert final_state.values["business_status"] == "answered"
    with database_session_factory() as session:
        workspace = session.get(WorkspaceRecord, fixture.workspace_id)
        assert workspace is not None
        assert workspace.draft_revision == 1
        assert workspace.draft is not None
        assert workspace.draft["confirmed"] is False


def test_resume_field_edit_reachieves_interrupt_without_confirming(
    database_session_factory: sessionmaker[Session],
) -> None:
    """AC3: editing a field re-routes inside the resume; a still-complete draft
    reaches the confirmation interrupt again instead of confirming silently."""
    fixture = _t34_graph_fixture(
        database_session_factory,
        model=StaticReplyModel(
            ParsedReply(
                entitlement_id="insighthub.dashboard_view",
                duration_days=60,
                justification="业务需要",
            )
        ),
        draft=RequestDraft(
            employee_id="EMP-001",
            entitlement_id="insighthub.dashboard_view",
            duration_days=30,
            justification="业务需要",
            confirmed=False,
        ),
        pending=True,
        pending_draft_revision=2,
    )
    graph = build_production_graph(checkpointer=InMemorySaver())
    config = _config_for(fixture.graph_input.graph_run_id)
    with pytest.raises(ConfirmationInterruptRaised):
        graph.invoke(_with_request_text(fixture), config, context=fixture.context)
    _restore_confirmation_cursor(database_session_factory, fixture)

    path = _stream_path(
        graph,
        Command(resume={"decision": "route_new_input", "safe_user_text": "改成 60 天"}),
        fixture.context,
        config,
    )
    assert path[0] == "await_requester_confirmation"
    assert path[1] == "rehydrate_resume_snapshot"
    assert "parse_request_patch" in path
    assert "persist_draft_cas" in path
    assert path[-1] == "await_requester_confirmation"

    final_state = graph.get_state(config)
    assert final_state.values["business_status"] == "awaiting_confirmation"
    with database_session_factory() as session:
        workspace = session.get(WorkspaceRecord, fixture.workspace_id)
        assert workspace is not None
        # The edited duration was persisted by persist_draft_cas (T32 contract).
        assert workspace.draft is not None
        assert workspace.draft["duration_days"] == 60
        assert workspace.draft["confirmed"] is False
        assert workspace.draft_revision == 2


def test_resume_explicit_rejection_is_not_a_confirmation(
    database_session_factory: sessionmaker[Session],
) -> None:
    """AC3/AC2 parity: explicit rejection (不确认) must never confirm; it is a
    normal re-route that leaves the draft unconfirmed (still awaiting)."""
    fixture = _t34_graph_fixture(
        database_session_factory,
        model=StaticReplyModel(ParsedReply()),
        draft=_complete_draft(),
        pending=True,
    )
    graph = build_production_graph(checkpointer=InMemorySaver())
    config = _config_for(fixture.graph_input.graph_run_id)
    with pytest.raises(ConfirmationInterruptRaised):
        graph.invoke(_with_request_text(fixture), config, context=fixture.context)

    path = _stream_path(
        graph,
        Command(resume={"decision": "route_new_input", "safe_user_text": "不确认，先不提交"}),
        fixture.context,
        config,
    )
    assert path[0] == "await_requester_confirmation"
    assert path[1] == "rehydrate_resume_snapshot"
    assert "route_intent" in path
    assert "apply_confirmation_cas" not in path
    # The draft is still complete, so the graph returns to the confirmation
    # interrupt instead of silently confirming or ending.
    assert path[-1] == "await_requester_confirmation"
    with database_session_factory() as session:
        workspace = session.get(WorkspaceRecord, fixture.workspace_id)
        assert workspace is not None
        assert workspace.draft["confirmed"] is False  # type: ignore[index]
        assert workspace.draft_revision == 1


def _restore_confirmation_cursor(
    factory: sessionmaker[Session],
    fixture: T34GraphFixture,
) -> None:
    """Simulate the caller's fenced finalize transaction: after the graph's
    collection turn persisted a draft change, the confirmation Cursor is
    re-projected (the graph itself never writes Cursor columns)."""
    with factory() as session:
        workspace = session.get(WorkspaceRecord, fixture.workspace_id)
        assert workspace is not None
        workspace.cursor_actor_id = "EMP-001"
        workspace.cursor_auth_session_id = fixture.context["auth_session_id"]
        workspace.cursor_expected_field = "confirmation"
        workspace.cursor_last_question_kind = "confirmation"
        workspace.cursor_issued_at = datetime.now(UTC)
        workspace.cursor_consumed_at = None
        session.commit()


def test_resume_confirm_after_same_turn_collection_advances_revision(
    database_session_factory: sessionmaker[Session],
) -> None:
    """P1-1 graph-level: no prior draft (revision 0); the collection turn
    persists revision 1 before interrupting, and confirm must succeed."""
    fixture = _t34_graph_fixture(
        database_session_factory,
        model=StaticReplyModel(_complete_reply()),
        pending=True,
        pending_draft_revision=1,
    )
    graph = build_production_graph(checkpointer=InMemorySaver())
    config = _config_for(fixture.graph_input.graph_run_id)
    with pytest.raises(ConfirmationInterruptRaised) as raised:
        graph.invoke(_with_request_text(fixture), config, context=fixture.context)
    assert raised.value.payload["draft_revision"] == 1
    _restore_confirmation_cursor(database_session_factory, fixture)

    path = _stream_path(
        graph,
        Command(resume={"decision": "confirm", "safe_user_text": "确认提交"}),
        fixture.context,
        config,
    )
    assert "apply_confirmation_cas" in path
    assert "compose_recoverable_answer" not in path
    final_state = graph.get_state(config)
    assert final_state.values["business_status"] == "ready_to_submit"
    with database_session_factory() as session:
        workspace = session.get(WorkspaceRecord, fixture.workspace_id)
        assert workspace is not None
        assert workspace.draft["confirmed"] is True  # type: ignore[index]
        assert workspace.draft_revision == 2


def test_resume_revoked_session_routes_conflict_without_business_branch(
    database_session_factory: sessionmaker[Session],
) -> None:
    """P1-2 graph-level: a revoked AuthSession at resume time closes through
    the recoverable branch; the business route is never entered."""
    fixture = _t34_graph_fixture(
        database_session_factory,
        model=StaticReplyModel(_complete_reply()),
        draft=_complete_draft(),
        pending=True,
    )
    graph = build_production_graph(checkpointer=InMemorySaver())
    config = _config_for(fixture.graph_input.graph_run_id)
    with pytest.raises(ConfirmationInterruptRaised):
        graph.invoke(_with_request_text(fixture), config, context=fixture.context)
    # Revoke the session between the interrupt and the resume call.
    with database_session_factory() as session:
        auth = session.scalar(select(AuthSessionRecord).where(
            AuthSessionRecord.workspace_id == fixture.workspace_id
        ))
        assert auth is not None
        auth.revoked_at = datetime.now(UTC)
        session.commit()

    path = _stream_path(
        graph,
        Command(resume={"decision": "route_new_input", "safe_user_text": "查一下政策"}),
        fixture.context,
        config,
    )
    assert path[0] == "await_requester_confirmation"
    assert path[1] == "rehydrate_resume_snapshot"
    assert "compose_recoverable_answer" in path
    assert "route_intent" not in path
    assert "retrieve_policy_pgvector" not in path
    final_state = graph.get_state(config)
    assert final_state.values["business_status"] == "recoverable_error"
    assert final_state.values["recoverable_error"].code == "CONFIRMATION_CONFLICT"
