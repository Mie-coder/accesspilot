"""T32 transaction-aware model quota and draft step operations."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.step_operations import (
    AgentStepContext,
    AgentStepOperationService,
    StepExecutionRejected,
)
from accesspilot.db.models import (
    AgentStepExecutionRecord,
    AgentTurnExecutionRecord,
    AuthSessionRecord,
    WorkspaceEventRecord,
    WorkspaceRecord,
)
from accesspilot.db.seed import seed_catalog
from accesspilot.domain.models import RequestDraft
from accesspilot.events import ModelQuotaExceededError
from accesspilot.workspaces import DraftRevisionConflictError


@dataclass(frozen=True)
class StepFixture:
    token: str
    workspace_id: UUID
    auth_session_id: UUID
    context: AgentStepContext


def _step_fixture(
    factory: sessionmaker[Session],
    *,
    draft: RequestDraft | None = None,
    expected_field: str | None = None,
    model_call_limit: int = 20,
    model_calls_used: int = 0,
) -> StepFixture:
    token = f"t32-step-{uuid4()}"
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
        auth = AuthSessionRecord(
            id=auth_session_id,
            token_hash=sha256(f"auth-{auth_session_id}".encode()).hexdigest(),
            csrf_hash=sha256(f"csrf-{auth_session_id}".encode()).hexdigest(),
            employee_id="EMP-001",
            workspace_id=workspace.id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        session.add(auth)
        event = WorkspaceEventRecord(
            workspace_id=workspace.id,
            event_type="message.user",
            payload={"content": "T32 fixture", "turn_id": input_turn_id},
        )
        session.add(event)
        session.flush()
        session.add(
            AgentTurnExecutionRecord(
                workspace_id=workspace.id,
                graph_run_id=graph_run_id,
                checkpoint_thread_id=f"accesspilot:v1.3:{graph_run_id}",
                input_seq=0,
                input_turn_id=input_turn_id,
                input_event_id=event.id,
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
    context = AgentStepContext(
        workspace_id=workspace_id,
        graph_run_id=graph_run_id,
        input_seq=0,
        input_turn_id=input_turn_id,
        actor_id="EMP-001",
        auth_session_ref=auth_session_id,
        lease_fence=1,
    )
    return StepFixture(token, workspace_id, auth_session_id, context)


def test_model_attempt_reserve_and_complete_are_replay_safe_in_one_ledger(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _step_fixture(database_session_factory)
    service = AgentStepOperationService(database_session_factory)

    first = service.reserve_model_attempt(
        fixture.context,
        workspace_token=fixture.token,
        attempt=1,
    )
    replay = service.reserve_model_attempt(
        fixture.context,
        workspace_token=fixture.token,
        attempt=1,
    )
    completed = service.complete_model_attempt(
        fixture.context,
        workspace_token=fixture.token,
        attempt=1,
    )
    completed_replay = service.reserve_model_attempt(
        fixture.context,
        workspace_token=fixture.token,
        attempt=1,
    )

    assert first.status == replay.status == "reserved"
    assert first.quota.used == replay.quota.used == 1
    assert completed.status == completed_replay.status == "completed"
    assert completed_replay.result_reference == "quota_consumed"
    with database_session_factory() as session:
        workspace = session.get(WorkspaceRecord, fixture.workspace_id)
        assert workspace is not None
        assert workspace.model_calls_used == 1
        assert workspace.model_retry_consumed == 0
        assert (
            session.scalar(
                select(func.count())
                .select_from(AgentStepExecutionRecord)
                .where(AgentStepExecutionRecord.workspace_id == fixture.workspace_id)
            )
            == 1
        )


def test_retry_attempt_has_distinct_stable_operation_and_consumes_retry_once(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _step_fixture(database_session_factory, model_call_limit=2)
    service = AgentStepOperationService(database_session_factory)

    primary = service.reserve_model_attempt(
        fixture.context,
        workspace_token=fixture.token,
        attempt=1,
    )
    retry = service.reserve_model_attempt(
        fixture.context,
        workspace_token=fixture.token,
        attempt=2,
    )
    replay = service.reserve_model_attempt(
        fixture.context,
        workspace_token=fixture.token,
        attempt=2,
    )

    assert primary.operation_id != retry.operation_id
    assert retry.operation_id == replay.operation_id
    assert retry.quota.used == replay.quota.used == 2
    assert retry.quota.retry_consumed == replay.quota.retry_consumed == 1


@pytest.mark.parametrize(
    "context_update",
    [
        {"lease_fence": 2},
        {"lease_fence": True},
        {"lease_fence": 0},
        {"actor_id": "EMP-003"},
        {"auth_session_ref": uuid4()},
        {"input_turn_id": f"turn-{uuid4()}"},
        {"input_seq": 1},
        {"input_seq": True},
        {"graph_run_id": uuid4()},
        {"workspace_id": uuid4()},
    ],
)
def test_execution_mismatch_fails_before_quota_or_step_write(
    database_session_factory: sessionmaker[Session],
    context_update: dict[str, object],
) -> None:
    fixture = _step_fixture(database_session_factory)
    service = AgentStepOperationService(database_session_factory)
    invalid = fixture.context.model_copy(update=context_update)

    with pytest.raises(StepExecutionRejected):
        service.reserve_model_attempt(
            invalid,
            workspace_token=fixture.token,
            attempt=1,
        )

    with database_session_factory() as session:
        workspace = session.get(WorkspaceRecord, fixture.workspace_id)
        assert workspace is not None
        assert workspace.model_calls_used == 0
        assert (
            session.scalar(
                select(func.count())
                .select_from(AgentStepExecutionRecord)
                .where(AgentStepExecutionRecord.workspace_id == fixture.workspace_id)
            )
            == 0
        )


def test_expired_execution_fails_before_quota_or_step_write(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _step_fixture(database_session_factory)
    with database_session_factory() as session:
        execution = session.scalar(
            select(AgentTurnExecutionRecord).where(
                AgentTurnExecutionRecord.workspace_id == fixture.workspace_id
            )
        )
        assert execution is not None
        execution.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

    with pytest.raises(StepExecutionRejected):
        AgentStepOperationService(database_session_factory).reserve_model_attempt(
            fixture.context,
            workspace_token=fixture.token,
            attempt=1,
        )

    with database_session_factory() as session:
        workspace = session.get(WorkspaceRecord, fixture.workspace_id)
        assert workspace is not None and workspace.model_calls_used == 0


def test_revoked_auth_session_fails_before_quota_or_step_write(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _step_fixture(database_session_factory)
    with database_session_factory() as session:
        auth_session = session.get(AuthSessionRecord, fixture.auth_session_id)
        assert auth_session is not None
        auth_session.revoked_at = datetime.now(UTC)
        session.commit()

    with pytest.raises(StepExecutionRejected):
        AgentStepOperationService(database_session_factory).reserve_model_attempt(
            fixture.context,
            workspace_token=fixture.token,
            attempt=1,
        )

    with database_session_factory() as session:
        workspace = session.get(WorkspaceRecord, fixture.workspace_id)
        assert workspace is not None and workspace.model_calls_used == 0
        assert session.scalar(
            select(func.count())
            .select_from(AgentStepExecutionRecord)
            .where(AgentStepExecutionRecord.workspace_id == fixture.workspace_id)
        ) == 0


def test_workspace_token_or_current_fence_mismatch_fails_before_mutation(
    database_session_factory: sessionmaker[Session],
) -> None:
    token_fixture = _step_fixture(database_session_factory)
    service = AgentStepOperationService(database_session_factory)
    with pytest.raises(StepExecutionRejected):
        service.reserve_model_attempt(
            token_fixture.context,
            workspace_token="wrong-workspace-token",
            attempt=1,
        )

    fence_fixture = _step_fixture(database_session_factory)
    with database_session_factory() as session:
        workspace = session.get(WorkspaceRecord, fence_fixture.workspace_id)
        assert workspace is not None
        workspace.lease_fence = 2
        session.commit()
    with pytest.raises(StepExecutionRejected):
        service.reserve_model_attempt(
            fence_fixture.context,
            workspace_token=fence_fixture.token,
            attempt=1,
        )

    with database_session_factory() as session:
        for fixture in (token_fixture, fence_fixture):
            workspace = session.get(WorkspaceRecord, fixture.workspace_id)
            assert workspace is not None and workspace.model_calls_used == 0
            assert session.scalar(
                select(func.count())
                .select_from(AgentStepExecutionRecord)
                .where(AgentStepExecutionRecord.workspace_id == fixture.workspace_id)
            ) == 0


def test_draft_cas_and_step_completion_commit_together_and_replay_once(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _step_fixture(database_session_factory)
    service = AgentStepOperationService(database_session_factory)
    draft = RequestDraft(
        employee_id="EMP-001",
        entitlement_id="insighthub.dashboard_view",
        duration_days=14,
        justification="T32 transaction test",
        confirmed=False,
    )

    first = service.persist_draft(
        fixture.context,
        workspace_token=fixture.token,
        expected_revision=0,
        draft=draft,
    )
    replay = service.persist_draft(
        fixture.context,
        workspace_token=fixture.token,
        expected_revision=0,
        draft=draft,
    )

    assert first.committed_revision == replay.committed_revision == 1
    assert replay.replayed is True
    with database_session_factory() as session:
        workspace = session.get(WorkspaceRecord, fixture.workspace_id)
        step = session.scalar(
            select(AgentStepExecutionRecord).where(
                AgentStepExecutionRecord.workspace_id == fixture.workspace_id,
                AgentStepExecutionRecord.step_key == "persist_draft_cas",
            )
        )
        assert workspace is not None and workspace.draft_revision == 1
        assert workspace.draft == draft.model_dump(mode="json")
        assert step is not None and step.status == "completed"
        assert step.committed_revision == 1


def test_numeric_duration_cas_consumes_cursor_and_completes_step_atomically(
    database_session_factory: sessionmaker[Session],
) -> None:
    existing = RequestDraft(
        employee_id="EMP-001",
        entitlement_id="insighthub.customer_export",
        confirmed=False,
    )
    fixture = _step_fixture(
        database_session_factory,
        draft=existing,
        expected_field="duration_days",
    )
    proposed = existing.model_copy(update={"duration_days": 14})
    service = AgentStepOperationService(database_session_factory)

    first = service.persist_numeric_duration(
        fixture.context,
        workspace_token=fixture.token,
        expected_revision=1,
        draft=proposed,
    )
    replay = service.persist_numeric_duration(
        fixture.context,
        workspace_token=fixture.token,
        expected_revision=1,
        draft=proposed,
    )

    assert first.committed_revision == replay.committed_revision == 2
    assert replay.replayed is True
    with database_session_factory() as session:
        workspace = session.get(WorkspaceRecord, fixture.workspace_id)
        assert workspace is not None
        assert workspace.draft_revision == 2
        assert workspace.cursor_consumed_at is not None
        assert workspace.cursor_expected_field == "duration_days"
        step = session.scalar(
            select(AgentStepExecutionRecord).where(
                AgentStepExecutionRecord.workspace_id == fixture.workspace_id,
                AgentStepExecutionRecord.step_key == "persist_numeric_duration",
            )
        )
        assert step is not None and step.committed_revision == 2


def test_quota_exhaustion_leaves_no_reserved_operation(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _step_fixture(
        database_session_factory,
        model_call_limit=1,
        model_calls_used=1,
    )

    with pytest.raises(ModelQuotaExceededError):
        AgentStepOperationService(database_session_factory).reserve_model_attempt(
            fixture.context,
            workspace_token=fixture.token,
            attempt=1,
        )

    with database_session_factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(AgentStepExecutionRecord)
                .where(AgentStepExecutionRecord.workspace_id == fixture.workspace_id)
            )
            == 0
        )


def test_draft_revision_conflict_rolls_back_step_and_draft_together(
    database_session_factory: sessionmaker[Session],
) -> None:
    existing = RequestDraft(employee_id="EMP-001", confirmed=False)
    fixture = _step_fixture(database_session_factory, draft=existing)
    proposed = existing.model_copy(update={"entitlement_id": "insighthub.dashboard_view"})

    with pytest.raises(DraftRevisionConflictError):
        AgentStepOperationService(database_session_factory).persist_draft(
            fixture.context,
            workspace_token=fixture.token,
            expected_revision=0,
            draft=proposed,
        )

    with database_session_factory() as session:
        workspace = session.get(WorkspaceRecord, fixture.workspace_id)
        assert workspace is not None
        assert workspace.draft == existing.model_dump(mode="json")
        assert workspace.draft_revision == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(AgentStepExecutionRecord)
                .where(AgentStepExecutionRecord.workspace_id == fixture.workspace_id)
            )
            == 0
        )


def test_ledger_flush_failure_rolls_back_quota_and_draft_mutations(
    database_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quota_fixture = _step_fixture(database_session_factory)
    draft_fixture = _step_fixture(database_session_factory)
    original_flush = Session.flush

    def fail_step_flush(
        session: Session,
        objects: object | None = None,
    ) -> None:
        if any(
            isinstance(record, AgentStepExecutionRecord) for record in session.new
        ):
            raise RuntimeError("injected step ledger failure")
        original_flush(session, objects)  # type: ignore[arg-type]

    with monkeypatch.context() as scoped:
        scoped.setattr(Session, "flush", fail_step_flush)
        with pytest.raises(RuntimeError, match="ledger failure"):
            AgentStepOperationService(
                database_session_factory
            ).reserve_model_attempt(
                quota_fixture.context,
                workspace_token=quota_fixture.token,
                attempt=1,
            )
        with pytest.raises(RuntimeError, match="ledger failure"):
            AgentStepOperationService(database_session_factory).persist_draft(
                draft_fixture.context,
                workspace_token=draft_fixture.token,
                expected_revision=0,
                draft=RequestDraft(
                    employee_id="EMP-001",
                    entitlement_id="insighthub.dashboard_view",
                    confirmed=False,
                ),
            )

    with database_session_factory() as session:
        for fixture in (quota_fixture, draft_fixture):
            workspace = session.get(WorkspaceRecord, fixture.workspace_id)
            assert workspace is not None
            assert workspace.model_calls_used == 0
            assert workspace.draft is None and workspace.draft_revision == 0
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AgentStepExecutionRecord)
                    .where(
                        AgentStepExecutionRecord.workspace_id
                        == fixture.workspace_id
                    )
                )
                == 0
            )


def test_concurrent_same_operation_consumes_quota_once(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _step_fixture(database_session_factory)
    service = AgentStepOperationService(database_session_factory)

    def reserve() -> str:
        return service.reserve_model_attempt(
            fixture.context,
            workspace_token=fixture.token,
            attempt=1,
        ).operation_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        operation_ids = list(pool.map(lambda _: reserve(), range(2)))

    assert operation_ids[0] == operation_ids[1]
    with database_session_factory() as session:
        workspace = session.get(WorkspaceRecord, fixture.workspace_id)
        assert workspace is not None and workspace.model_calls_used == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(AgentStepExecutionRecord)
                .where(AgentStepExecutionRecord.workspace_id == fixture.workspace_id)
            )
            == 1
        )


def test_next_input_sequence_has_new_operation_identity_and_can_consume_quota(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _step_fixture(database_session_factory)
    service = AgentStepOperationService(database_session_factory)
    first = service.reserve_model_attempt(
        fixture.context,
        workspace_token=fixture.token,
        attempt=1,
    )
    next_turn_id = f"turn-{uuid4()}"
    with database_session_factory() as session:
        execution = session.scalar(
            select(AgentTurnExecutionRecord).where(
                AgentTurnExecutionRecord.workspace_id == fixture.workspace_id,
                AgentTurnExecutionRecord.input_seq == 0,
            )
        )
        assert execution is not None
        terminal = WorkspaceEventRecord(
            workspace_id=fixture.workspace_id,
            event_type="agent.completed",
            payload={"turn_id": fixture.context.input_turn_id},
        )
        next_input = WorkspaceEventRecord(
            workspace_id=fixture.workspace_id,
            event_type="message.user",
            payload={"turn_id": next_turn_id},
        )
        session.add_all([terminal, next_input])
        session.flush()
        execution.status = "completed"
        execution.lease_expires_at = None
        execution.terminal_event_id = terminal.id
        session.flush()
        session.add(
            AgentTurnExecutionRecord(
                workspace_id=fixture.workspace_id,
                graph_run_id=fixture.context.graph_run_id,
                checkpoint_thread_id=(f"accesspilot:v1.3:{fixture.context.graph_run_id}"),
                input_seq=1,
                input_turn_id=next_turn_id,
                input_event_id=next_input.id,
                auth_session_ref=fixture.auth_session_id,
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
    next_context = fixture.context.model_copy(
        update={"input_seq": 1, "input_turn_id": next_turn_id}
    )

    second = service.reserve_model_attempt(
        next_context,
        workspace_token=fixture.token,
        attempt=1,
    )

    assert first.operation_id != second.operation_id
    with database_session_factory() as session:
        workspace = session.get(WorkspaceRecord, fixture.workspace_id)
        assert workspace is not None and workspace.model_calls_used == 2
        assert (
            session.scalar(
                select(func.count())
                .select_from(AgentStepExecutionRecord)
                .where(AgentStepExecutionRecord.workspace_id == fixture.workspace_id)
            )
            == 2
        )


def test_activate_missing_cursor_writes_five_columns_and_replays_on_same_row(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _step_fixture(
        database_session_factory,
        draft=RequestDraft(employee_id="EMP-001", duration_days=14, confirmed=False),
    )
    service = AgentStepOperationService(database_session_factory)

    first = service.activate_missing_cursor(
        fixture.context,
        workspace_token=fixture.token,
        expected_revision=1,
        expected_field="entitlement_id",
    )
    with database_session_factory() as session:
        workspace = session.get(WorkspaceRecord, fixture.workspace_id)
        assert workspace is not None
        first_issued_at = workspace.cursor_issued_at
    replay = service.activate_missing_cursor(
        fixture.context,
        workspace_token=fixture.token,
        expected_revision=1,
        expected_field="entitlement_id",
    )

    assert first.committed_revision == replay.committed_revision == 1
    assert first.replayed is False
    assert replay.replayed is True
    with database_session_factory() as session:
        workspace = session.get(WorkspaceRecord, fixture.workspace_id)
        assert workspace is not None
        assert workspace.cursor_actor_id == "EMP-001"
        assert workspace.cursor_auth_session_id == str(fixture.auth_session_id)
        assert workspace.cursor_expected_field == "entitlement_id"
        assert workspace.cursor_last_question_kind == "entitlement_id"
        assert workspace.cursor_issued_at == first_issued_at
        assert workspace.cursor_consumed_at is None
        assert workspace.draft_revision == 1
        steps = session.scalars(
            select(AgentStepExecutionRecord).where(
                AgentStepExecutionRecord.workspace_id == fixture.workspace_id,
                AgentStepExecutionRecord.step_key == "activate_missing_cursor",
            )
        ).all()
    assert len(steps) == 1
    assert steps[0].status == "completed"
    assert steps[0].committed_revision == 1


def test_activate_missing_cursor_rejects_stale_fence_or_expired_lease_with_zero_writes(
    database_session_factory: sessionmaker[Session],
) -> None:
    stale_fence_fixture = _step_fixture(database_session_factory)
    expired_lease_fixture = _step_fixture(database_session_factory)
    with database_session_factory() as session:
        for fixture, tamper in (
            (stale_fence_fixture, "fence"),
            (expired_lease_fixture, "lease"),
        ):
            execution = session.scalar(
                select(AgentTurnExecutionRecord).where(
                    AgentTurnExecutionRecord.workspace_id == fixture.workspace_id
                )
            )
            assert execution is not None
            if tamper == "fence":
                execution.lease_fence = 2
            else:
                execution.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

    service = AgentStepOperationService(database_session_factory)
    for fixture in (stale_fence_fixture, expired_lease_fixture):
        with pytest.raises(StepExecutionRejected):
            service.activate_missing_cursor(
                fixture.context,
                workspace_token=fixture.token,
                expected_revision=0,
                expected_field="entitlement_id",
            )

    with database_session_factory() as session:
        for fixture in (stale_fence_fixture, expired_lease_fixture):
            workspace = session.get(WorkspaceRecord, fixture.workspace_id)
            assert workspace is not None
            assert workspace.cursor_actor_id is None
            assert workspace.cursor_auth_session_id is None
            assert workspace.cursor_expected_field is None
            assert workspace.cursor_last_question_kind is None
            assert workspace.cursor_issued_at is None
            assert workspace.cursor_consumed_at is None
            assert workspace.draft_revision == 0
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AgentStepExecutionRecord)
                    .where(AgentStepExecutionRecord.workspace_id == fixture.workspace_id)
                )
                == 0
            )


def test_activate_missing_cursor_other_session_cursor_and_revision_conflict_roll_back(
    database_session_factory: sessionmaker[Session],
) -> None:
    draft = RequestDraft(
        employee_id="EMP-001",
        entitlement_id="insighthub.dashboard_view",
        confirmed=False,
    )
    other_session_fixture = _step_fixture(
        database_session_factory,
        draft=draft,
        expected_field="justification",
    )
    with database_session_factory() as session:
        workspace = session.get(
            WorkspaceRecord,
            other_session_fixture.workspace_id,
        )
        assert workspace is not None
        workspace.cursor_auth_session_id = str(uuid4())
        session.commit()

    with pytest.raises(DraftRevisionConflictError):
        AgentStepOperationService(
            database_session_factory
        ).activate_missing_cursor(
            other_session_fixture.context,
            workspace_token=other_session_fixture.token,
            expected_revision=1,
            expected_field="duration_days",
        )

    revision_fixture = _step_fixture(database_session_factory, draft=draft)
    with pytest.raises(DraftRevisionConflictError):
        AgentStepOperationService(
            database_session_factory
        ).activate_missing_cursor(
            revision_fixture.context,
            workspace_token=revision_fixture.token,
            expected_revision=0,
            expected_field="duration_days",
        )

    with database_session_factory() as session:
        other_workspace = session.get(
            WorkspaceRecord,
            other_session_fixture.workspace_id,
        )
        assert other_workspace is not None
        assert (
            other_workspace.cursor_auth_session_id
            != str(other_session_fixture.auth_session_id)
        )
        assert other_workspace.cursor_expected_field == "justification"
        assert other_workspace.draft_revision == 1
        revision_workspace = session.get(
            WorkspaceRecord,
            revision_fixture.workspace_id,
        )
        assert revision_workspace is not None
        assert revision_workspace.cursor_expected_field is None
        assert revision_workspace.cursor_issued_at is None
        assert revision_workspace.draft_revision == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(AgentStepExecutionRecord)
                .where(
                    AgentStepExecutionRecord.workspace_id.in_(
                        [
                            other_session_fixture.workspace_id,
                            revision_fixture.workspace_id,
                        ]
                    )
                )
            )
            == 0
        )


def test_semantic_route_quota_and_result_survive_replay_separately_from_extraction(
    database_session_factory: sessionmaker[Session],
) -> None:
    from accesspilot.agent.routing import IntentRoute

    fixture = _step_fixture(database_session_factory)
    service = AgentStepOperationService(database_session_factory)
    first = service.reserve_model_attempt(
        fixture.context, workspace_token=fixture.token, attempt=1, operation="route_intent",
    )
    # Simulate provider return lost before completion: repeat reserves no extra quota.
    repeated = service.reserve_model_attempt(
        fixture.context, workspace_token=fixture.token, attempt=1, operation="route_intent",
    )
    assert first.quota.used == repeated.quota.used == 1
    route = IntentRoute(intent="discover_eligible_access")
    assert service.read_model_attempt(
        fixture.context, workspace_token=fixture.token, attempt=1,
    ) is None
    service.complete_model_attempt(
        fixture.context, workspace_token=fixture.token, attempt=1,
        operation="route_intent", route=route,
    )
    replay = service.reserve_model_attempt(
        fixture.context, workspace_token=fixture.token, attempt=1, operation="route_intent",
    )
    assert replay.result_reference == route.model_dump_json()
    assert replay.quota.used == 1
    assert service.read_model_attempt(
        fixture.context, workspace_token=fixture.token, attempt=1, operation="route_intent",
    ) == replay
    extracted = service.reserve_model_attempt(
        fixture.context, workspace_token=fixture.token, attempt=1,
    )
    assert extracted.quota.used == 2
    assert extracted.operation_id != replay.operation_id
    assert extracted.quota.retry_consumed == 0
    with pytest.raises(StepExecutionRejected):
        service.reserve_model_attempt(
            fixture.context.model_copy(update={"lease_fence": 2}),
            workspace_token=fixture.token, attempt=1, operation="route_intent",
        )
