"""T33 real PostgreSQL concurrency/recovery proof on a disposable database."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.session import close_all_sessions

from accesspilot.agent.step_operations import (
    AgentStepContext,
    AgentStepOperationService,
    StepExecutionRejected,
)
from accesspilot.agent.turn_execution import (
    RecoveryPlan,
    TurnExecutionService,
    TurnInProgressError,
    TurnRecoveryInProgressError,
)
from accesspilot.db.models import (
    AgentTurnExecutionRecord,
    AuthSessionRecord,
    WorkspaceEventRecord,
    WorkspaceRecord,
)
from accesspilot.db.seed import seed_catalog
from accesspilot.db.session import build_engine, build_session_factory
from accesspilot.db.workspace_store import hash_workspace_token

_ADMIN_URL_ENV = "ACCESSPILOT_T33_ADMIN_DATABASE_URL"
_FALLBACK_URL_ENVS = (
    "ACCESSPILOT_T30_ADMIN_DATABASE_URL",
    "ACCESSPILOT_T29_ADMIN_DATABASE_URL",
)


def _admin_url() -> str | None:
    for name in (_ADMIN_URL_ENV, *_FALLBACK_URL_ENVS):
        value = os.getenv(name)
        if value:
            return value
    return None


@contextmanager
def _isolated_database() -> Iterator[str]:
    configured = _admin_url()
    if not configured:
        pytest.skip(
            f"set {_ADMIN_URL_ENV} to run the destructive-isolated T33 PostgreSQL proof"
        )
    admin_url = make_url(configured).set(
        drivername="postgresql", database="postgres"
    )
    admin_conninfo = admin_url.render_as_string(hide_password=False)
    suffix = uuid4().hex[:12]
    database = f"t33_db_{suffix}"
    with psycopg.connect(admin_conninfo, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database))
        )
    database_url = make_url(configured).set(database=database).render_as_string(
        hide_password=False
    )
    try:
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
        alembic_config = Config(os.path.join(root, "alembic.ini"))
        alembic_config.attributes["database_url"] = database_url
        command.upgrade(alembic_config, "head")
        yield database_url
    finally:
        with psycopg.connect(admin_conninfo, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database,),
            )
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database))
            )


def _workspace_fixture(
    factory: sessionmaker[Session],
) -> tuple[str, UUID, UUID, UUID]:
    token = f"t33-pg-{uuid4()}"
    auth_session_id = uuid4()
    with factory() as session:
        seed_catalog(session)
        workspace = WorkspaceRecord(
            token_hash=hash_workspace_token(token),
            actor_id="EMP-001",
            flow_version=2,
            lease_fence=0,
            model_call_limit=20,
        )
        session.add(workspace)
        session.flush()
        workspace_id = workspace.id
        agent_thread_id = workspace.agent_thread_id
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


def _expire_lease(
    factory: sessionmaker[Session],
    workspace_id: UUID,
) -> None:
    with factory() as session:
        execution = session.scalar(
            select(AgentTurnExecutionRecord).where(
                AgentTurnExecutionRecord.workspace_id == workspace_id
            )
        )
        assert execution is not None
        execution.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()


def _count_events(factory: sessionmaker[Session], workspace_id: UUID) -> int:
    with factory() as session:
        return len(
            session.scalars(
                select(WorkspaceEventRecord).where(
                    WorkspaceEventRecord.workspace_id == workspace_id
                )
            ).all()
        )


def _step_context_from_handle(
    handle,
    *,
    lease_fence: int | None = None,
) -> AgentStepContext:
    return AgentStepContext(
        workspace_id=handle.workspace_id,
        graph_run_id=handle.graph_run_id,
        input_seq=handle.input_seq,
        input_turn_id=handle.input_turn_id,
        actor_id=handle.actor_id,
        auth_session_ref=handle.auth_session_ref,
        lease_fence=handle.lease_fence if lease_fence is None else lease_fence,
    )


def test_concurrent_begin_input_second_conflict_without_new_facts() -> None:
    with _isolated_database() as database_url:
        factory = build_session_factory(build_engine(database_url))
        token, workspace_id, _agent_thread_id, auth_session_id = _workspace_fixture(
            factory
        )
        service = TurnExecutionService(factory)
        barrier = threading.Barrier(2)
        results: list[object] = []
        errors: list[BaseException] = []

        def attempt() -> None:
            barrier.wait()
            try:
                results.append(
                    service.begin_input(
                        workspace_token=token,
                        auth_session_ref=auth_session_id,
                        actor_id="EMP-001",
                        safe_user_text="concurrent",
                    )
                )
            except BaseException as error:  # pragma: no cover - exercised by thread
                errors.append(error)

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(
            errors[0], (TurnInProgressError, TurnRecoveryInProgressError)
        )
        assert _count_events(factory, workspace_id) == 2
        with factory() as session:
            assert (
                session.scalar(
                    select(AgentTurnExecutionRecord).where(
                        AgentTurnExecutionRecord.workspace_id == workspace_id
                    )
                )
                is not None
            )
        close_all_sessions()


def test_takeover_reuses_identity_increments_fence_and_stale_step_write_fails() -> None:
    with _isolated_database() as database_url:
        factory = build_session_factory(build_engine(database_url))
        token, workspace_id, agent_thread_id, auth_session_id = _workspace_fixture(
            factory
        )
        service = TurnExecutionService(factory)
        handle = service.begin_input(
            workspace_token=token,
            auth_session_ref=auth_session_id,
            actor_id="EMP-001",
            safe_user_text="takeover me",
        )
        old_fence = handle.lease_fence
        old_attempt = handle.attempt
        _expire_lease(factory, workspace_id)
        before_events = _count_events(factory, workspace_id)

        with service.advisory_lock(agent_thread_id):
            plan = service.takeover(
                workspace_token=token,
                graph_run_id=handle.graph_run_id,
                input_seq=handle.input_seq,
                auth_session_ref=auth_session_id,
                actor_id="EMP-001",
            )

        assert isinstance(plan, RecoveryPlan)
        assert plan.graph_run_id == handle.graph_run_id
        assert plan.input_seq == handle.input_seq
        assert plan.input_turn_id == handle.input_turn_id
        assert plan.input_event_id == handle.input_event_id
        assert plan.attempt == old_attempt + 1
        assert plan.lease_fence == old_fence + 1
        assert plan.source == "input_event"
        assert plan.accepted_checkpoint_id is None
        assert _count_events(factory, workspace_id) == before_events

        with factory() as session:
            execution = session.scalar(
                select(AgentTurnExecutionRecord).where(
                    AgentTurnExecutionRecord.workspace_id == workspace_id
                )
            )
            assert execution is not None
            assert execution.attempt == old_attempt + 1
            assert execution.lease_fence == old_fence + 1
            workspace = session.get(WorkspaceRecord, workspace_id)
            assert workspace is not None
            assert workspace.lease_fence == old_fence + 1

        stale_context = _step_context_from_handle(handle, lease_fence=old_fence)
        with pytest.raises(StepExecutionRejected):
            AgentStepOperationService(factory).reserve_model_attempt(
                stale_context,
                workspace_token=token,
                attempt=1,
            )
        with factory() as session:
            workspace = session.get(WorkspaceRecord, workspace_id)
            assert workspace is not None
            assert workspace.model_calls_used == 0
        close_all_sessions()


def test_takeover_with_accepted_head_plans_exact_checkpoint_recovery() -> None:
    with _isolated_database() as database_url:
        factory = build_session_factory(build_engine(database_url))
        token, workspace_id, agent_thread_id, auth_session_id = _workspace_fixture(
            factory
        )
        service = TurnExecutionService(factory)
        handle = service.begin_input(
            workspace_token=token,
            auth_session_ref=auth_session_id,
            actor_id="EMP-001",
            safe_user_text="checkpoint me",
        )
        accepted = "0" * 32
        with factory() as session:
            execution = session.scalar(
                select(AgentTurnExecutionRecord).where(
                    AgentTurnExecutionRecord.workspace_id == workspace_id
                )
            )
            assert execution is not None
            execution.accepted_checkpoint_id = accepted
            execution.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()

        with service.advisory_lock(agent_thread_id):
            plan = service.takeover(
                workspace_token=token,
                graph_run_id=handle.graph_run_id,
                input_seq=handle.input_seq,
                auth_session_ref=auth_session_id,
                actor_id="EMP-001",
            )

        assert plan.source == "checkpoint"
        assert plan.accepted_checkpoint_id == accepted
        assert plan.checkpoint_thread_id == handle.checkpoint_thread_id
        assert plan.input_turn_id == handle.input_turn_id
        assert plan.input_event_id == handle.input_event_id
        with factory() as session:
            execution = session.scalar(
                select(AgentTurnExecutionRecord).where(
                    AgentTurnExecutionRecord.workspace_id == workspace_id
                )
            )
            assert execution is not None
            assert execution.accepted_checkpoint_id == accepted
        close_all_sessions()


def test_t33_and_t32_write_paths_share_lock_order_no_deadlock() -> None:
    with _isolated_database() as database_url:
        factory = build_session_factory(build_engine(database_url))
        token, workspace_id, _agent_thread_id, auth_session_id = _workspace_fixture(
            factory
        )
        service = TurnExecutionService(factory)
        handle = service.begin_input(
            workspace_token=token,
            auth_session_ref=auth_session_id,
            actor_id="EMP-001",
            safe_user_text="lock order",
        )
        with factory() as session:
            terminal = WorkspaceEventRecord(
                workspace_id=workspace_id,
                event_type="message.completed",
                payload={
                    "turn_id": handle.input_turn_id,
                    "message_id": "msg-lock",
                    "content": "ok",
                },
            )
            session.add(terminal)
            session.flush()
            terminal_event_id = terminal.id
            session.commit()

        errors: list[BaseException] = []

        def t32_style_write() -> None:
            try:
                with factory() as session, session.begin():
                    session.execute(text("SET LOCAL lock_timeout = '500ms'"))
                    session.execute(
                        text(
                            "SELECT id FROM agent_turn_executions "
                            "WHERE id = :execution_id FOR UPDATE"
                        ),
                        {"execution_id": handle.execution_id},
                    )
                    time.sleep(0.2)
                    session.execute(
                        text(
                            "SELECT id FROM workspaces WHERE id = :workspace_id FOR UPDATE"
                        ),
                        {"workspace_id": workspace_id},
                    )
            except BaseException as error:  # pragma: no cover - failure path
                errors.append(error)

        def t33_complete() -> None:
            try:
                service.complete_turn(
                    handle,
                    workspace_token=token,
                    terminal_event_id=terminal_event_id,
                )
            except BaseException as error:  # pragma: no cover - failure path
                errors.append(error)

        threads = [
            threading.Thread(target=t32_style_write),
            threading.Thread(target=t33_complete),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert errors == [], [repr(error) for error in errors]
        with factory() as session:
            execution = session.scalar(
                select(AgentTurnExecutionRecord).where(
                    AgentTurnExecutionRecord.workspace_id == workspace_id
                )
            )
            assert execution is not None
            assert execution.status == "completed"
            assert execution.lease_expires_at is None
        close_all_sessions()
