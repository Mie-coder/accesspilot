"""T33 turn execution lease, fence, advisory lock, and completion primitives."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.advisory_lock import AdvisoryLockOwnershipError
from accesspilot.agent.turn_execution import (
    StaleTurnFenceError,
    TurnExecutionService,
    TurnInProgressError,
    TurnLockUnavailableError,
    TurnRecoveryInProgressError,
)
from accesspilot.agent.turn_runner import FencedGraphTurnRunner
from accesspilot.db.models import (
    AgentTurnExecutionRecord,
    AuthSessionRecord,
    WorkspaceEventRecord,
    WorkspaceRecord,
)


def _workspace_fixture(
    factory: sessionmaker[Session],
    *,
    lease_fence: int = 0,
) -> tuple[str, UUID, UUID, UUID]:
    token = f"t33-turn-{uuid4()}"
    auth_session_id = uuid4()
    with factory() as session:
        workspace = WorkspaceRecord(
            token_hash=sha256(token.encode()).hexdigest(),
            actor_id="EMP-001",
            flow_version=2,
            lease_fence=lease_fence,
            model_call_limit=20,
        )
        session.add(workspace)
        session.flush()
        agent_thread_id = workspace.agent_thread_id
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
    return token, workspace_id, agent_thread_id, auth_session_id


def _execution(
    factory: sessionmaker[Session],
    workspace_id: UUID,
) -> AgentTurnExecutionRecord | None:
    with factory() as session:
        return session.scalar(
            select(AgentTurnExecutionRecord).where(
                AgentTurnExecutionRecord.workspace_id == workspace_id
            )
        )


def _event_count(
    factory: sessionmaker[Session],
    workspace_id: UUID,
) -> int:
    with factory() as session:
        return session.scalar(
            select(func.count())
            .select_from(WorkspaceEventRecord)
            .where(WorkspaceEventRecord.workspace_id == workspace_id)
        )


def test_begin_input_atomically_creates_execution_started_and_user_event(
    database_session_factory: sessionmaker[Session],
) -> None:
    token, workspace_id, _agent_thread_id, auth_session_id = _workspace_fixture(
        database_session_factory
    )
    service = TurnExecutionService(database_session_factory)

    handle = service.begin_input(
        workspace_token=token,
        auth_session_ref=auth_session_id,
        actor_id="EMP-001",
        safe_user_text="  请帮我申请权限  ",
    )

    assert handle.workspace_id == workspace_id
    assert handle.input_seq == 0
    assert handle.attempt == 1
    assert handle.lease_fence == 1
    assert handle.accepted_checkpoint_id is None
    assert handle.checkpoint_thread_id == f"accesspilot:v1.3:{handle.graph_run_id}"

    with database_session_factory() as session:
        workspace = session.get(WorkspaceRecord, workspace_id)
        assert workspace is not None
        assert workspace.lease_fence == 1
        events = list(
            session.scalars(
                select(WorkspaceEventRecord)
                .where(WorkspaceEventRecord.workspace_id == workspace_id)
                .order_by(WorkspaceEventRecord.id)
            ).all()
        )
        assert [event.event_type for event in events] == ["turn.started", "message.user"]
        execution = session.scalar(
            select(AgentTurnExecutionRecord).where(
                AgentTurnExecutionRecord.workspace_id == workspace_id
            )
        )
        assert execution is not None
        assert execution.input_event_id == events[1].id
        assert execution.status == "running"
        assert execution.lease_expires_at is not None
        assert events[1].payload["content"] == "请帮我申请权限"
        assert events[1].payload["turn_id"] == handle.input_turn_id


def test_second_running_input_fails_before_new_facts(
    database_session_factory: sessionmaker[Session],
) -> None:
    token, workspace_id, _agent_thread_id, auth_session_id = _workspace_fixture(
        database_session_factory
    )
    service = TurnExecutionService(database_session_factory)
    first = service.begin_input(
        workspace_token=token,
        auth_session_ref=auth_session_id,
        actor_id="EMP-001",
        safe_user_text="first",
    )
    before_events = _event_count(database_session_factory, workspace_id)
    before_fence = first.lease_fence

    with pytest.raises(TurnInProgressError):
        service.begin_input(
            workspace_token=token,
            auth_session_ref=auth_session_id,
            actor_id="EMP-001",
            safe_user_text="second",
        )

    with database_session_factory() as session:
        workspace = session.get(WorkspaceRecord, workspace_id)
        assert workspace is not None
        assert workspace.lease_fence == before_fence
    assert _event_count(database_session_factory, workspace_id) == before_events


def test_expired_running_input_reports_recovery_in_progress(
    database_session_factory: sessionmaker[Session],
) -> None:
    token, workspace_id, _agent_thread_id, auth_session_id = _workspace_fixture(
        database_session_factory
    )
    service = TurnExecutionService(database_session_factory)
    service.begin_input(
        workspace_token=token,
        auth_session_ref=auth_session_id,
        actor_id="EMP-001",
        safe_user_text="first",
    )
    with database_session_factory() as session:
        execution = session.scalar(
            select(AgentTurnExecutionRecord).where(
                AgentTurnExecutionRecord.workspace_id == workspace_id
            )
        )
        assert execution is not None
        execution.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

    before_events = _event_count(database_session_factory, workspace_id)
    with pytest.raises(TurnRecoveryInProgressError):
        service.begin_input(
            workspace_token=token,
            auth_session_ref=auth_session_id,
            actor_id="EMP-001",
            safe_user_text="second",
        )
    assert _event_count(database_session_factory, workspace_id) == before_events


def test_heartbeat_renews_lease_and_rejects_stale_fence(
    database_session_factory: sessionmaker[Session],
) -> None:
    token, workspace_id, _agent_thread_id, auth_session_id = _workspace_fixture(
        database_session_factory
    )
    service = TurnExecutionService(database_session_factory)
    handle = service.begin_input(
        workspace_token=token,
        auth_session_ref=auth_session_id,
        actor_id="EMP-001",
        safe_user_text="heartbeat",
    )

    renewed = service.heartbeat(handle)
    assert renewed > handle.lease_expires_at

    with database_session_factory() as session:
        execution = session.scalar(
            select(AgentTurnExecutionRecord).where(
                AgentTurnExecutionRecord.workspace_id == workspace_id
            )
        )
        assert execution is not None
        execution.lease_fence = 999
        session.commit()

    with pytest.raises(StaleTurnFenceError):
        service.heartbeat(handle)


def test_advisory_lock_is_exclusive_per_agent_thread(
    database_session_factory: sessionmaker[Session],
) -> None:
    token, workspace_id, agent_thread_id, auth_session_id = _workspace_fixture(
        database_session_factory
    )
    service = TurnExecutionService(database_session_factory)
    service.begin_input(
        workspace_token=token,
        auth_session_ref=auth_session_id,
        actor_id="EMP-001",
        safe_user_text="lock",
    )

    with service.advisory_lock(agent_thread_id):
        with pytest.raises(TurnLockUnavailableError):
            with service.advisory_lock(agent_thread_id):
                pass

    with service.advisory_lock(agent_thread_id):
        pass


def test_complete_turn_releases_lease_and_next_input_reuses_graph_run(
    database_session_factory: sessionmaker[Session],
) -> None:
    token, workspace_id, agent_thread_id, auth_session_id = _workspace_fixture(
        database_session_factory
    )
    service = TurnExecutionService(database_session_factory)
    first = service.begin_input(
        workspace_token=token,
        auth_session_ref=auth_session_id,
        actor_id="EMP-001",
        safe_user_text="first",
    )

    with database_session_factory() as session:
        terminal = WorkspaceEventRecord(
            workspace_id=workspace_id,
            event_type="message.completed",
            payload={
                "turn_id": first.input_turn_id,
                "message_id": "msg-1",
                "content": "ok",
            },
        )
        session.add(terminal)
        session.flush()
        terminal_event_id = terminal.id
        session.commit()

    with service.advisory_lock(agent_thread_id) as lock:
        service.complete_turn(
            first,
            workspace_token=token,
            terminal_event_id=terminal_event_id,
            lock=lock,
        )

    with database_session_factory() as session:
        execution = session.scalar(
            select(AgentTurnExecutionRecord).where(
                AgentTurnExecutionRecord.workspace_id == workspace_id
            )
        )
        assert execution is not None
        assert execution.status == "completed"
        assert execution.lease_expires_at is None
        assert execution.terminal_event_id == terminal_event_id

    second = service.begin_input(
        workspace_token=token,
        auth_session_ref=auth_session_id,
        actor_id="EMP-001",
        safe_user_text="second",
    )
    assert second.graph_run_id == first.graph_run_id
    assert second.input_seq == first.input_seq + 1
    assert second.lease_fence == first.lease_fence + 1


class _BlockingGraph:
    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        self.started = started
        self.release = release
        self.called = False

    def invoke(
        self,
        graph_input: object,
        config: object | None = None,
        *,
        context: object | None = None,
        **kwargs: object,
    ) -> dict[str, bool]:
        del graph_input, config, context, kwargs
        self.called = True
        self.started.set()
        assert self.release.wait(5)
        return {"ok": True}


class _CountingGraph:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(
        self,
        graph_input: object,
        config: object | None = None,
        *,
        context: object | None = None,
        **kwargs: object,
    ) -> dict[str, bool]:
        del graph_input, config, context, kwargs
        self.calls += 1
        return {"ok": True}


def test_runner_expired_handle_fails_before_graph_invoke(
    database_session_factory: sessionmaker[Session],
) -> None:
    token, workspace_id, agent_thread_id, auth_session_id = _workspace_fixture(
        database_session_factory
    )
    service = TurnExecutionService(database_session_factory)
    handle = service.begin_input(
        workspace_token=token,
        auth_session_ref=auth_session_id,
        actor_id="EMP-001",
        safe_user_text="expired runner",
    )
    with database_session_factory() as session:
        execution = session.scalar(
            select(AgentTurnExecutionRecord).where(
                AgentTurnExecutionRecord.workspace_id == workspace_id
            )
        )
        assert execution is not None
        execution.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

    graph = _CountingGraph()
    runner = FencedGraphTurnRunner(graph, service, heartbeat_interval=0.05)
    with pytest.raises(StaleTurnFenceError):
        with service.advisory_lock(agent_thread_id) as lock:
            runner.run(handle, {}, context={}, lock=lock)  # type: ignore[arg-type]
    assert graph.calls == 0


def test_runner_rejects_wrong_thread_lock_before_graph_invoke(
    database_session_factory: sessionmaker[Session],
) -> None:
    token_a, workspace_id_a, _agent_thread_a, auth_session_a = _workspace_fixture(
        database_session_factory
    )
    _token_b, _workspace_id_b, agent_thread_b, _auth_session_b = _workspace_fixture(
        database_session_factory
    )
    service = TurnExecutionService(database_session_factory)
    handle_a = service.begin_input(
        workspace_token=token_a,
        auth_session_ref=auth_session_a,
        actor_id="EMP-001",
        safe_user_text="workspace A",
    )
    graph = _CountingGraph()
    runner = FencedGraphTurnRunner(graph, service, heartbeat_interval=0.05)

    with service.advisory_lock(agent_thread_b) as wrong_lock:
        with pytest.raises(AdvisoryLockOwnershipError):
            runner.run(handle_a, {}, context={}, lock=wrong_lock)  # type: ignore[arg-type]
    assert graph.calls == 0
    assert workspace_id_a is not None


def test_runner_holds_advisory_lock_during_graph_invoke(
    database_session_factory: sessionmaker[Session],
) -> None:
    token, _workspace_id, agent_thread_id, auth_session_id = _workspace_fixture(
        database_session_factory
    )
    service = TurnExecutionService(database_session_factory)
    handle = service.begin_input(
        workspace_token=token,
        auth_session_ref=auth_session_id,
        actor_id="EMP-001",
        safe_user_text="runner",
    )
    started = threading.Event()
    release = threading.Event()
    graph = _BlockingGraph(started, release)
    runner = FencedGraphTurnRunner(graph, service, heartbeat_interval=0.05)
    results: list[dict[str, bool]] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            with service.advisory_lock(agent_thread_id) as lock:
                results.append(
                    runner.run(handle, {}, context={}, lock=lock)  # type: ignore[arg-type]
                )
        except BaseException as error:  # pragma: no cover - failure path
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(5)
    with pytest.raises(TurnLockUnavailableError):
        with service.advisory_lock(agent_thread_id):
            pass
    release.set()
    thread.join(timeout=5)
    assert errors == []
    assert results == [{"ok": True}]
    assert graph.called is True
