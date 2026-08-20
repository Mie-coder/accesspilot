"""T37 crash -> restart -> takeover/reconcile proofs.

These tests use the production graph, application transactions, PostgreSQL
lease/advisory-lock primitives, and a process-persistent in-memory LangGraph
checkpointer.  The latter keeps the tests deterministic while still proving
that unaccepted candidates are not selected by recovery.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.checkpoint import (
    AcceptedCheckpointHeadStore,
    FencedPostgresSaverAdapter,
    ServerExecutionContext,
)
from accesspilot.agent.embeddings import DeterministicEmbeddingModel
from accesspilot.agent.fault_injection import FaultPoint, InjectedCrash, inject_fault
from accesspilot.agent.production_graph import (
    ConfirmationInterruptRaised,
    GraphInput,
    GraphOutput,
    GraphRuntimeContext,
    build_production_graph,
)
from accesspilot.agent.routing import DeterministicIntentRouter
from accesspilot.agent.step_operations import (
    AgentStepContext,
    AgentStepOperationService,
    StepExecutionRejected,
)
from accesspilot.agent.turn_execution import (
    StaleTurnFenceError,
    TurnExecutionHandle,
    TurnExecutionService,
)
from accesspilot.auth import Principal
from accesspilot.db.models import (
    AccessGrantRecord,
    AccessRequestRecord,
    AgentPendingInputRecord,
    AgentStepExecutionRecord,
    AgentTurnExecutionRecord,
    ApprovalCaseRecord,
    AuthSessionRecord,
    WorkspaceEventRecord,
    WorkspaceRecord,
)
from accesspilot.db.seed import seed_catalog
from accesspilot.db.workspace_store import SqlAlchemyWorkspaceStore, hash_workspace_token
from accesspilot.domain.models import ParsedReply
from accesspilot.tools.catalog import ToolResult
from accesspilot.tools.policies import PolicyService
from accesspilot.workspaces import WorkspaceService

_REQUEST_TEXT = "申请仪表盘查看 14 天，用于 T37 故障恢复"


class CountingReplyModel:
    def __init__(self) -> None:
        self.calls = 0

    def parse_reply(
        self,
        user_reply: str,
        correction: str | None = None,
    ) -> ParsedReply:
        del user_reply, correction
        self.calls += 1
        return ParsedReply(
            entitlement_id="insighthub.dashboard_view",
            duration_days=14,
            justification="T37 故障恢复",
        )


@dataclass(frozen=True)
class RecoveryFixture:
    token: str
    workspace_id: UUID
    auth_session_id: UUID
    handle: TurnExecutionHandle
    service: TurnExecutionService


def _fixture(
    factory: sessionmaker[Session],
    *,
    safe_user_text: str = _REQUEST_TEXT,
) -> RecoveryFixture:
    token = f"t37-{uuid4()}"
    auth_session_id = uuid4()
    with factory() as session:
        seed_catalog(session)
        workspace = WorkspaceRecord(
            token_hash=hash_workspace_token(token),
            actor_id="EMP-001",
            flow_version=2,
            model_call_limit=20,
        )
        session.add(workspace)
        session.flush()
        workspace_id = workspace.id
        session.add(
            AuthSessionRecord(
                id=auth_session_id,
                token_hash=sha256(f"auth-{auth_session_id}".encode()).hexdigest(),
                csrf_hash=sha256(f"csrf-{auth_session_id}".encode()).hexdigest(),
                employee_id="EMP-001",
                workspace_id=workspace_id,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        session.commit()
    service = TurnExecutionService(factory)
    handle = service.begin_input(
        workspace_token=token,
        auth_session_ref=auth_session_id,
        actor_id="EMP-001",
        safe_user_text=safe_user_text,
    )
    return RecoveryFixture(token, workspace_id, auth_session_id, handle, service)


def _server_context(
    factory: sessionmaker[Session],
    execution_id: UUID,
) -> ServerExecutionContext:
    with factory() as session:
        execution = session.get(AgentTurnExecutionRecord, execution_id)
        assert execution is not None
        return ServerExecutionContext.from_record(execution)


def _runtime_context(
    factory: sessionmaker[Session],
    fixture: RecoveryFixture,
    handle: TurnExecutionHandle,
    model: object,
    pending_input_id: UUID,
) -> GraphRuntimeContext:
    return {
        "session_factory": factory,
        "workspace_service": WorkspaceService(SqlAlchemyWorkspaceStore(factory)),
        "policy_service": PolicyService(
            embedding_model=DeterministicEmbeddingModel()
        ),
        "structured_reply_model": model,
        "intent_router": DeterministicIntentRouter(),
        "principal": Principal(
            employee_id="EMP-001",
            name="林晓",
            department="数据平台部",
            roles=("analyst",),
        ),
        "current_turn_id": handle.input_turn_id,
        "current_input_seq": handle.input_seq,
        "current_fence": handle.lease_fence,
        "workspace_token": fixture.token,
        "auth_session_id": str(fixture.auth_session_id),
        "cookie": "test-only-cookie",
        "csrf_token": "test-only-csrf",
        "api_key": "test-only-api-key",
        "pending_input_id": str(pending_input_id),
        "trace_enabled": True,
    }


def _graph_input(
    fixture: RecoveryFixture,
    handle: TurnExecutionHandle,
    safe_user_text: str,
) -> GraphInput:
    return GraphInput(
        schema_version=1,
        flow_version=2,
        workspace_ref=fixture.workspace_id,
        graph_run_id=handle.graph_run_id,
        input_seq=handle.input_seq,
        input_turn_id=handle.input_turn_id,
        safe_user_text=safe_user_text,
        input_kind="new_input",
    )


def _config(context: ServerExecutionContext) -> dict[str, object]:
    configurable: dict[str, object] = {
        "thread_id": context.checkpoint_thread_id,
        "checkpoint_ns": context.checkpoint_ns,
    }
    if context.accepted_checkpoint_id is not None:
        configurable["checkpoint_id"] = context.accepted_checkpoint_id
    return {"configurable": configurable}


def _handle_from_plan(plan: Any, fixture: RecoveryFixture) -> TurnExecutionHandle:
    return TurnExecutionHandle(
        execution_id=plan.execution_id,
        workspace_id=plan.workspace_id,
        agent_thread_id=plan.agent_thread_id,
        graph_run_id=plan.graph_run_id,
        checkpoint_thread_id=plan.checkpoint_thread_id,
        input_seq=plan.input_seq,
        input_turn_id=plan.input_turn_id,
        input_event_id=plan.input_event_id,
        attempt=plan.attempt,
        lease_fence=plan.lease_fence,
        lease_expires_at=plan.lease_expires_at,
        actor_id="EMP-001",
        auth_session_ref=fixture.auth_session_id,
        accepted_checkpoint_id=plan.accepted_checkpoint_id,
    )


def _step_context(handle: TurnExecutionHandle) -> AgentStepContext:
    return AgentStepContext(
        workspace_id=handle.workspace_id,
        graph_run_id=handle.graph_run_id,
        input_seq=handle.input_seq,
        input_turn_id=handle.input_turn_id,
        actor_id=handle.actor_id,
        auth_session_ref=handle.auth_session_ref,
        lease_fence=handle.lease_fence,
    )


def _expire(factory: sessionmaker[Session], execution_id: UUID) -> None:
    with factory() as session:
        execution = session.get(AgentTurnExecutionRecord, execution_id)
        assert execution is not None
        execution.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()


def _safe_input_fact(factory: sessionmaker[Session], input_event_id: int) -> str:
    with factory() as session:
        event = session.get(WorkspaceEventRecord, input_event_id)
        assert event is not None and event.event_type == "message.user"
        content = event.payload.get("content")
        assert isinstance(content, str) and content
        return content


def _verify_interrupt(invocation: Any, graph: Any) -> Any:
    candidate = invocation.candidate
    assert candidate is not None
    return invocation.verify_candidate(
        candidate,
        graph_stopped=True,
        graph_state_reader=graph.compiled.get_state,
        state_validator=lambda state: any(task.interrupts for task in state.tasks),
    )


def _verify_end(invocation: Any, graph: Any) -> Any:
    candidate = invocation.candidate
    assert candidate is not None
    return invocation.verify_candidate(
        candidate,
        graph_stopped=True,
        graph_state_reader=graph.compiled.get_state,
        state_validator=lambda state: not state.next,
    )


def _terminal_payload(handle: TurnExecutionHandle, turn: GraphOutput) -> dict[str, object]:
    return {
        "turn_id": handle.input_turn_id,
        "message_id": f"msg-{uuid4()}",
        "content": turn.assistant_message,
        "intent": turn.intent,
        "business_status": turn.business_status,
        "draft_revision": turn.draft_revision,
    }


def _assert_no_duplicate_event_keys(
    factory: sessionmaker[Session],
    workspace_id: UUID,
) -> None:
    with factory() as session:
        duplicate = session.execute(
            select(WorkspaceEventRecord.event_key, func.count())
            .where(
                WorkspaceEventRecord.workspace_id == workspace_id,
                WorkspaceEventRecord.event_key.is_not(None),
            )
            .group_by(WorkspaceEventRecord.event_key)
            .having(func.count() > 1)
        ).first()
        assert duplicate is None


def _assert_trace_identities_are_at_most_once(
    factory: sessionmaker[Session],
    workspace_id: UUID,
) -> None:
    identity_fields = {
        "agent.node.started": "step_id",
        "agent.node.completed": "step_id",
        "model.started": "step_id",
        "model.completed": "step_id",
        "tool.started": "tool_call_id",
        "tool.completed": "tool_call_id",
    }
    with factory() as session:
        events = session.scalars(
            select(WorkspaceEventRecord).where(
                WorkspaceEventRecord.workspace_id == workspace_id,
                WorkspaceEventRecord.event_type.in_(tuple(identity_fields)),
            )
        ).all()
    seen: set[tuple[str, object]] = set()
    for event in events:
        identity = (event.event_type, event.payload[identity_fields[event.event_type]])
        assert identity not in seen
        seen.add(identity)


def _assert_one_terminal_for_turn(
    factory: sessionmaker[Session],
    workspace_id: UUID,
    turn_id: str,
) -> None:
    with factory() as session:
        terminals = [
            event
            for event in session.scalars(
                select(WorkspaceEventRecord).where(
                    WorkspaceEventRecord.workspace_id == workspace_id,
                    WorkspaceEventRecord.event_type.in_(
                        ("message.completed", "error.recoverable", "turn.interrupted")
                    ),
                )
            )
            if event.payload.get("turn_id") == turn_id
        ]
    assert len(terminals) == 1


def _assert_formal_business_rows_unchanged(
    factory: sessionmaker[Session],
    workspace_id: UUID,
) -> None:
    with factory() as session:
        for model in (AccessRequestRecord, ApprovalCaseRecord, AccessGrantRecord):
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.workspace_id == workspace_id)
                )
                == 0
            )


@pytest.mark.parametrize(
    ("point", "calls_before_crash", "calls_after_recovery"),
    [
        (FaultPoint.AFTER_QUOTA_COMMIT_BEFORE_CHECKPOINT, 1, 2),
        (FaultPoint.AFTER_DRAFT_CAS_BEFORE_CHECKPOINT, 1, 1),
        (FaultPoint.AFTER_INTERRUPT_SAVED, 1, 1),
    ],
)
def test_new_input_crash_restarts_from_same_safe_input_without_duplicate_local_writes(
    database_session_factory: sessionmaker[Session],
    point: FaultPoint,
    calls_before_crash: int,
    calls_after_recovery: int,
) -> None:
    fixture = _fixture(database_session_factory)
    saver = InMemorySaver()
    model = CountingReplyModel()
    pending_input_id = uuid4()
    context_a = _server_context(database_session_factory, fixture.handle.execution_id)
    invocation_a = FencedPostgresSaverAdapter(saver).for_execution(context_a)
    graph_a = build_production_graph(checkpointer=invocation_a)

    with fixture.service.advisory_lock(fixture.handle.agent_thread_id):
        with inject_fault(point), pytest.raises(InjectedCrash, match=point.value):
            graph_a.invoke(
                _graph_input(fixture, fixture.handle, _REQUEST_TEXT),
                _config(context_a),
                context=_runtime_context(
                    database_session_factory,
                    fixture,
                    fixture.handle,
                    model,
                    pending_input_id,
                ),
            )
    assert model.calls == calls_before_crash
    stale_verified = (
        _verify_interrupt(invocation_a, graph_a)
        if point == FaultPoint.AFTER_INTERRUPT_SAVED
        else None
    )

    _expire(database_session_factory, fixture.handle.execution_id)
    with fixture.service.advisory_lock(fixture.handle.agent_thread_id) as lock:
        plan = fixture.service.takeover(
            workspace_token=fixture.token,
            graph_run_id=fixture.handle.graph_run_id,
            input_seq=fixture.handle.input_seq,
            auth_session_ref=fixture.auth_session_id,
            actor_id="EMP-001",
            lock=lock,
            saver=saver,
        )
        recovered = _handle_from_plan(plan, fixture)
        assert plan.source == "input_event"
        assert plan.graph_run_id == fixture.handle.graph_run_id
        assert plan.input_seq == fixture.handle.input_seq
        assert plan.input_turn_id == fixture.handle.input_turn_id
        assert plan.input_event_id == fixture.handle.input_event_id
        assert plan.attempt == fixture.handle.attempt + 1
        assert plan.lease_fence == fixture.handle.lease_fence + 1

        # The expired owner cannot consume another unit of local quota after
        # takeover changed the execution/workspace fence.
        with pytest.raises(StepExecutionRejected):
            AgentStepOperationService(database_session_factory).reserve_model_attempt(
                _step_context(fixture.handle),
                workspace_token=fixture.token,
                attempt=2,
            )
        if stale_verified is not None:
            with database_session_factory() as stale_session, stale_session.begin():
                assert (
                    AcceptedCheckpointHeadStore().promote(
                        stale_session,
                        stale_verified,
                    )
                    is False
                )

        recovered_safe_text = _safe_input_fact(
            database_session_factory,
            plan.input_event_id,
        )
        context_b = _server_context(database_session_factory, recovered.execution_id)
        invocation_b = FencedPostgresSaverAdapter(saver).for_execution(context_b)
        graph_b = build_production_graph(checkpointer=invocation_b)
        with pytest.raises(ConfirmationInterruptRaised) as stopped:
            graph_b.invoke(
                _graph_input(fixture, recovered, recovered_safe_text),
                _config(context_b),
                context=_runtime_context(
                    database_session_factory,
                    fixture,
                    recovered,
                    model,
                    pending_input_id,
                ),
            )
        verified = _verify_interrupt(invocation_b, graph_b)
        terminal_id = fixture.service.finalize_interrupt(
            recovered,
            workspace_token=fixture.token,
            lock=lock,
            verified=verified,
            pending_input_id=pending_input_id,
            draft_revision=int(stopped.value.payload["draft_revision"]),
        )

    assert model.calls == calls_after_recovery
    with database_session_factory() as session:
        workspace = session.get(WorkspaceRecord, fixture.workspace_id)
        execution = session.get(AgentTurnExecutionRecord, fixture.handle.execution_id)
        assert workspace is not None and execution is not None
        assert workspace.model_calls_used == 1
        assert workspace.draft_revision == 1
        assert execution.attempt == 2
        assert execution.lease_fence == 2
        assert execution.status == "waiting_input"
        assert execution.terminal_event_id == terminal_id
        assert execution.accepted_checkpoint_id == verified.locator.checkpoint_id
        assert (
            session.scalar(
                select(func.count())
                .select_from(AgentStepExecutionRecord)
                .where(
                    AgentStepExecutionRecord.workspace_id == fixture.workspace_id,
                    AgentStepExecutionRecord.step_key == "persist_draft_cas",
                )
            )
            == 1
        )
        user_event = session.get(WorkspaceEventRecord, fixture.handle.input_event_id)
        assert user_event is not None and user_event.payload["content"] == _REQUEST_TEXT
    _assert_no_duplicate_event_keys(database_session_factory, fixture.workspace_id)
    _assert_trace_identities_are_at_most_once(
        database_session_factory, fixture.workspace_id
    )
    _assert_one_terminal_for_turn(
        database_session_factory,
        fixture.workspace_id,
        fixture.handle.input_turn_id,
    )
    _assert_formal_business_rows_unchanged(
        database_session_factory, fixture.workspace_id
    )


def test_tool_completion_crash_allows_at_least_once_tool_but_one_local_trace_and_terminal(
    database_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    safe_text = "查看我当前拥有的权限"
    fixture = _fixture(database_session_factory, safe_user_text=safe_text)
    saver = InMemorySaver()
    model = CountingReplyModel()
    pending_input_id = uuid4()
    calls = 0
    from accesspilot.agent import production_graph

    real_execute = production_graph.execute_read_only_tool

    def counted_execute(*args: Any, **kwargs: Any) -> ToolResult:
        nonlocal calls
        calls += 1
        return real_execute(*args, **kwargs)

    monkeypatch.setattr(production_graph, "execute_read_only_tool", counted_execute)
    context_a = _server_context(database_session_factory, fixture.handle.execution_id)
    invocation_a = FencedPostgresSaverAdapter(saver).for_execution(context_a)
    graph_a = build_production_graph(checkpointer=invocation_a)
    with fixture.service.advisory_lock(fixture.handle.agent_thread_id):
        point = FaultPoint.AFTER_TOOL_COMPLETION_BEFORE_EVENT
        with inject_fault(point), pytest.raises(InjectedCrash, match=point.value):
            graph_a.invoke(
                _graph_input(fixture, fixture.handle, safe_text),
                _config(context_a),
                context=_runtime_context(
                    database_session_factory,
                    fixture,
                    fixture.handle,
                    model,
                    pending_input_id,
                ),
            )
    assert calls == 1

    _expire(database_session_factory, fixture.handle.execution_id)
    with fixture.service.advisory_lock(fixture.handle.agent_thread_id) as lock:
        plan = fixture.service.takeover(
            workspace_token=fixture.token,
            graph_run_id=fixture.handle.graph_run_id,
            input_seq=fixture.handle.input_seq,
            auth_session_ref=fixture.auth_session_id,
            actor_id="EMP-001",
            lock=lock,
            saver=saver,
        )
        recovered = _handle_from_plan(plan, fixture)
        recovered_safe_text = _safe_input_fact(
            database_session_factory,
            plan.input_event_id,
        )
        context_b = _server_context(database_session_factory, recovered.execution_id)
        invocation_b = FencedPostgresSaverAdapter(saver).for_execution(context_b)
        graph_b = build_production_graph(checkpointer=invocation_b)
        turn = graph_b.invoke(
            _graph_input(fixture, recovered, recovered_safe_text),
            _config(context_b),
            context=_runtime_context(
                database_session_factory,
                fixture,
                recovered,
                model,
                pending_input_id,
            ),
        )
        verified = _verify_end(invocation_b, graph_b)

        # A normal graph END has the same candidate -> accepted invariant as
        # interrupt/resume finalization. Promotion failure must roll back the
        # terminal event, status change and lease release together.
        with monkeypatch.context() as patch:
            patch.setattr(AcceptedCheckpointHeadStore, "promote", lambda *args: False)
            with pytest.raises(StaleTurnFenceError, match="head promotion failed"):
                fixture.service.finalize_graph_turn_with_event(
                    recovered,
                    workspace_token=fixture.token,
                    lock=lock,
                    verified=verified,
                    event_type="message.completed",
                    payload=_terminal_payload(recovered, turn),
                )
        with database_session_factory() as session:
            execution = session.get(AgentTurnExecutionRecord, recovered.execution_id)
            assert execution is not None
            assert execution.status == "running"
            assert execution.lease_expires_at is not None
            assert execution.terminal_event_id is None
            assert execution.accepted_checkpoint_id == recovered.accepted_checkpoint_id

        terminal_id = fixture.service.finalize_graph_turn_with_event(
            recovered,
            workspace_token=fixture.token,
            lock=lock,
            verified=verified,
            event_type="message.completed",
            payload=_terminal_payload(recovered, turn),
        )

    assert calls == 2  # honest at-least-once read-tool boundary
    with database_session_factory() as session:
        execution = session.get(AgentTurnExecutionRecord, fixture.handle.execution_id)
        assert execution is not None
        assert execution.attempt == 2
        assert execution.lease_fence == 2
        assert execution.status == "completed"
        assert execution.lease_expires_at is None
        assert execution.terminal_event_id == terminal_id
        assert execution.accepted_checkpoint_id == verified.locator.checkpoint_id
        completed = session.scalars(
            select(WorkspaceEventRecord).where(
                WorkspaceEventRecord.workspace_id == fixture.workspace_id,
                WorkspaceEventRecord.event_type == "tool.completed",
            )
        ).all()
        assert len(completed) == 1
    _assert_no_duplicate_event_keys(database_session_factory, fixture.workspace_id)
    _assert_trace_identities_are_at_most_once(
        database_session_factory, fixture.workspace_id
    )
    _assert_one_terminal_for_turn(
        database_session_factory,
        fixture.workspace_id,
        fixture.handle.input_turn_id,
    )
    _assert_formal_business_rows_unchanged(
        database_session_factory, fixture.workspace_id
    )


def _establish_confirmation_pending(
    factory: sessionmaker[Session],
) -> tuple[RecoveryFixture, InMemorySaver, UUID]:
    fixture = _fixture(factory)
    saver = InMemorySaver()
    pending_input_id = uuid4()
    model = CountingReplyModel()
    context = _server_context(factory, fixture.handle.execution_id)
    invocation = FencedPostgresSaverAdapter(saver).for_execution(context)
    graph = build_production_graph(checkpointer=invocation)
    with fixture.service.advisory_lock(fixture.handle.agent_thread_id) as lock:
        with pytest.raises(ConfirmationInterruptRaised) as stopped:
            graph.invoke(
                _graph_input(fixture, fixture.handle, _REQUEST_TEXT),
                _config(context),
                context=_runtime_context(
                    factory, fixture, fixture.handle, model, pending_input_id
                ),
            )
        verified = _verify_interrupt(invocation, graph)
        fixture.service.finalize_interrupt(
            fixture.handle,
            workspace_token=fixture.token,
            lock=lock,
            verified=verified,
            pending_input_id=pending_input_id,
            draft_revision=int(stopped.value.payload["draft_revision"]),
        )
    return fixture, saver, pending_input_id


@pytest.mark.parametrize(
    ("point", "decision", "safe_text", "expected_status", "expected_revision"),
    [
        (
            FaultPoint.AFTER_NON_CONFIRM_RESUME_CONSUMED,
            "route_new_input",
            "查看我当前拥有的权限",
            "answered",
            1,
        ),
        (
            FaultPoint.AFTER_CONFIRMATION_CAS_BEFORE_TERMINAL,
            "confirm",
            "确认提交",
            "ready_to_submit",
            2,
        ),
    ],
)
def test_resume_crash_reuses_original_resume_turn_and_finishes_once(
    database_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    point: FaultPoint,
    decision: str,
    safe_text: str,
    expected_status: str,
    expected_revision: int,
) -> None:
    fixture, saver, pending_input_id = _establish_confirmation_pending(
        database_session_factory
    )
    with fixture.service.advisory_lock(fixture.handle.agent_thread_id) as lock:
        resume = fixture.service.begin_resume(
            workspace_token=fixture.token,
            auth_session_ref=fixture.auth_session_id,
            actor_id="EMP-001",
            safe_user_text=safe_text,
            pending_input_id=pending_input_id,
            lock=lock,
            saver=saver,
        )
    context_a = _server_context(database_session_factory, resume.execution_id)
    invocation_a = FencedPostgresSaverAdapter(saver).for_execution(context_a)
    graph_a = build_production_graph(checkpointer=invocation_a)
    command = Command(resume={"decision": decision, "safe_user_text": safe_text})
    with fixture.service.advisory_lock(resume.agent_thread_id):
        with inject_fault(point), pytest.raises(InjectedCrash, match=point.value):
            graph_a.invoke(
                command,
                _config(context_a),
                context=_runtime_context(
                    database_session_factory,
                    fixture,
                    resume,
                    CountingReplyModel(),
                    pending_input_id,
                ),
            )

    _expire(database_session_factory, resume.execution_id)
    with fixture.service.advisory_lock(resume.agent_thread_id) as lock:
        plan = fixture.service.takeover(
            workspace_token=fixture.token,
            graph_run_id=resume.graph_run_id,
            input_seq=resume.input_seq,
            auth_session_ref=fixture.auth_session_id,
            actor_id="EMP-001",
            lock=lock,
            saver=saver,
        )
        recovered = _handle_from_plan(plan, fixture)
        assert plan.source == "checkpoint"
        assert plan.graph_run_id == resume.graph_run_id
        assert plan.input_seq == resume.input_seq
        assert plan.input_turn_id == resume.input_turn_id
        assert plan.input_event_id == resume.input_event_id
        assert plan.attempt == 2
        assert plan.lease_fence == resume.lease_fence + 1

        recovered_safe_text = _safe_input_fact(
            database_session_factory,
            plan.input_event_id,
        )
        recovered_command = Command(
            resume={"decision": decision, "safe_user_text": recovered_safe_text}
        )
        context_b = _server_context(database_session_factory, recovered.execution_id)
        invocation_b = FencedPostgresSaverAdapter(saver).for_execution(context_b)
        graph_b = build_production_graph(checkpointer=invocation_b)
        turn = graph_b.invoke(
            recovered_command,
            _config(context_b),
            context=_runtime_context(
                database_session_factory,
                fixture,
                recovered,
                CountingReplyModel(),
                pending_input_id,
            ),
        )
        assert turn.business_status == expected_status, turn.error_code
        assert turn.draft_revision == expected_revision
        verified = _verify_end(invocation_b, graph_b)

        # A checkpoint/head failure must leave the lease, pending and terminal
        # untouched; the same verified candidate may then be finalized once.
        with monkeypatch.context() as patch:
            patch.setattr(AcceptedCheckpointHeadStore, "promote", lambda *args: False)
            with pytest.raises(StaleTurnFenceError, match="head promotion failed"):
                fixture.service.finalize_resume_outcome(
                    recovered,
                    workspace_token=fixture.token,
                    lock=lock,
                    verified=verified,
                    pending_input_id=pending_input_id,
                    event_type="message.completed",
                    payload=_terminal_payload(recovered, turn),
                )
        with database_session_factory() as session:
            execution = session.get(AgentTurnExecutionRecord, recovered.execution_id)
            pending = session.scalar(
                select(AgentPendingInputRecord).where(
                    AgentPendingInputRecord.pending_input_id == pending_input_id
                )
            )
            assert execution is not None and pending is not None
            assert execution.status == "running"
            assert execution.lease_expires_at is not None
            assert execution.terminal_event_id is None
            assert pending.status == "resuming"

        terminal_id = fixture.service.finalize_resume_outcome(
            recovered,
            workspace_token=fixture.token,
            lock=lock,
            verified=verified,
            pending_input_id=pending_input_id,
            event_type="message.completed",
            payload=_terminal_payload(recovered, turn),
        )

    with database_session_factory() as session:
        workspace = session.get(WorkspaceRecord, fixture.workspace_id)
        execution = session.get(AgentTurnExecutionRecord, resume.execution_id)
        pending = session.scalar(
            select(AgentPendingInputRecord).where(
                AgentPendingInputRecord.pending_input_id == pending_input_id
            )
        )
        assert workspace is not None and execution is not None and pending is not None
        assert workspace.draft_revision == expected_revision
        assert bool(workspace.draft and workspace.draft.get("confirmed")) == (
            decision == "confirm"
        )
        assert execution.status == "completed"
        assert execution.terminal_event_id == terminal_id
        assert execution.accepted_checkpoint_id == verified.locator.checkpoint_id
        assert pending.status == "resolved"
        user_event = session.get(WorkspaceEventRecord, resume.input_event_id)
        assert user_event is not None and user_event.payload["content"] == safe_text
        confirmation_steps = session.scalars(
            select(AgentStepExecutionRecord).where(
                AgentStepExecutionRecord.workspace_id == fixture.workspace_id,
                AgentStepExecutionRecord.step_key == "apply_confirmation",
            )
        ).all()
        assert len(confirmation_steps) == (1 if decision == "confirm" else 0)
    _assert_no_duplicate_event_keys(database_session_factory, fixture.workspace_id)
    _assert_trace_identities_are_at_most_once(
        database_session_factory, fixture.workspace_id
    )
    _assert_one_terminal_for_turn(
        database_session_factory,
        fixture.workspace_id,
        resume.input_turn_id,
    )
    _assert_formal_business_rows_unchanged(
        database_session_factory, fixture.workspace_id
    )
