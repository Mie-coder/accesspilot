"""T33 real PostgreSQL concurrency/recovery proof on a disposable database."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import TypedDict
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from psycopg import sql
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.session import close_all_sessions

from accesspilot.agent.checkpoint import (
    AcceptedCheckpointHeadStore,
    ExactCheckpointRequired,
    FencedPostgresSaverAdapter,
    PostgresCheckpointRuntime,
    ServerExecutionContext,
)
from accesspilot.agent.step_operations import (
    AgentStepContext,
    AgentStepOperationService,
    StepExecutionRejected,
)
from accesspilot.agent.turn_execution import (
    RecoveryPlan,
    StaleTurnFenceError,
    TurnExecutionError,
    TurnExecutionHandle,
    TurnExecutionService,
    TurnInProgressError,
    TurnRecoveryInProgressError,
)
from accesspilot.agent.turn_runner import FencedGraphTurnRunner
from accesspilot.checkpoint_init import run_official_checkpoint_setup
from accesspilot.config import Settings
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


@contextmanager
def _isolated_checkpoint_database() -> Iterator[Settings]:
    configured = _admin_url()
    if not configured:
        pytest.skip(
            f"set {_ADMIN_URL_ENV} to run the destructive-isolated T33 checkpoint proof"
        )
    admin_url = make_url(configured).set(
        drivername="postgresql", database="postgres"
    )
    admin_conninfo = admin_url.render_as_string(hide_password=False)
    suffix = uuid4().hex[:12]
    database = f"t33chk_{suffix}"
    migration_role = f"t33m_{suffix}"
    runtime_role = f"t33r_{suffix}"
    migration_password = f"m-{uuid4().hex}"
    runtime_password = f"r-{uuid4().hex}"
    with psycopg.connect(admin_conninfo, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(migration_role), sql.Literal(migration_password)
            )
        )
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(runtime_role), sql.Literal(runtime_password)
            )
        )
        admin.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(database), sql.Identifier(migration_role)
            )
        )
    database_admin_url = make_url(configured).set(
        drivername="postgresql", database=database
    ).render_as_string(hide_password=False)
    with psycopg.connect(database_admin_url, autocommit=True) as database_admin:
        database_admin.execute("CREATE EXTENSION vector")
    migration_url = make_url(configured).set(
        username=migration_role,
        password=migration_password,
        database=database,
    ).render_as_string(hide_password=False)
    runtime_url = make_url(configured).set(
        username=runtime_role,
        password=runtime_password,
        database=database,
    ).render_as_string(hide_password=False)
    schema = f"t33_checkpoint_{suffix}"
    settings = Settings(
        database_url=migration_url,
        checkpoint_migration_database_url=migration_url,
        checkpoint_database_url=runtime_url,
        checkpoint_schema=schema,
        orchestrator_mode="mixed",
        langgraph_canary_percent=0,
        _env_file=None,
    )
    try:
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
        alembic_config = Config(os.path.join(root, "alembic.ini"))
        alembic_config.attributes["database_url"] = migration_url
        command.upgrade(alembic_config, "head")
        run_official_checkpoint_setup(settings)
        yield settings
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
            admin.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(runtime_role))
            )
            admin.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(migration_role))
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
                AgentTurnExecutionRecord.workspace_id == workspace_id,
                AgentTurnExecutionRecord.status == "running",
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


class _SmallState(TypedDict):
    value: str


def _small_node(state: _SmallState) -> dict[str, str]:
    return {"value": state["value"] + "!"}


def _small_graph(checkpointer: object) -> object:
    builder = StateGraph(_SmallState)
    builder.add_node("small_node", _small_node)
    builder.add_edge(START, "small_node")
    builder.add_edge("small_node", END)
    return builder.compile(checkpointer=checkpointer)


class _InterruptState(TypedDict):
    value: str


def _interrupt_node(state: _InterruptState) -> dict[str, str]:
    decision = interrupt({"kind": "test", "value": state["value"]})
    return {"value": f"{state['value']}:{decision}"}


def _interrupt_graph(checkpointer: object) -> object:
    builder = StateGraph(_InterruptState)
    builder.add_node("interrupt_node", _interrupt_node)
    builder.add_edge(START, "interrupt_node")
    builder.add_edge("interrupt_node", END)
    return builder.compile(checkpointer=checkpointer)


def _server_context(
    factory: sessionmaker[Session],
    workspace_id: UUID,
) -> ServerExecutionContext:
    with factory() as session:
        record = session.scalar(
            select(AgentTurnExecutionRecord).where(
                AgentTurnExecutionRecord.workspace_id == workspace_id
            )
        )
        assert record is not None
        return ServerExecutionContext.from_record(record)


def _checkpoint_settings(database_url: str) -> Settings:
    schema = f"t33_checkpoint_{uuid4().hex[:12]}"
    return Settings(
        database_url=database_url,
        checkpoint_migration_database_url=database_url,
        checkpoint_database_url=database_url,
        checkpoint_schema=schema,
        orchestrator_mode="mixed",
        langgraph_canary_percent=0,
        _env_file=None,
    )


def _promote_real_end_head(
    factory: sessionmaker[Session],
    runtime: PostgresCheckpointRuntime,
    service: TurnExecutionService,
    handle,
    agent_thread_id: UUID,
) -> tuple[ServerExecutionContext, object, object]:
    context = _server_context(factory, handle.workspace_id)
    invocation = FencedPostgresSaverAdapter(runtime.saver).for_execution(context)
    graph = _small_graph(invocation)
    base_config = {
        "configurable": {
            "thread_id": context.checkpoint_thread_id,
            "checkpoint_ns": "",
        }
    }
    graph.invoke({"value": "a"}, base_config, durability="sync")
    candidate = invocation.candidate
    assert candidate is not None
    verified = invocation.verify_candidate(
        candidate,
        graph_stopped=True,
        graph_state_reader=graph.get_state,
        state_validator=lambda state: not state.next,
    )
    with service.advisory_lock(agent_thread_id) as lock:
        assert (
            AcceptedCheckpointHeadStore().promote(lock.session, verified, lock=lock)
            is True
        )
    updated_context = _server_context(factory, handle.workspace_id)
    return updated_context, candidate, verified


def _promote_real_interrupt_head(
    factory: sessionmaker[Session],
    runtime: PostgresCheckpointRuntime,
    service: TurnExecutionService,
    handle,
    agent_thread_id: UUID,
) -> tuple[ServerExecutionContext, object, object]:
    context = _server_context(factory, handle.workspace_id)
    invocation = FencedPostgresSaverAdapter(runtime.saver).for_execution(context)
    graph = _interrupt_graph(invocation)
    base_config = {
        "configurable": {
            "thread_id": context.checkpoint_thread_id,
            "checkpoint_ns": "",
        }
    }
    graph.invoke({"value": "a"}, base_config, durability="sync")
    candidate = invocation.candidate
    assert candidate is not None
    verified = invocation.verify_candidate(
        candidate,
        graph_stopped=True,
        graph_state_reader=graph.get_state,
        state_validator=lambda state: any(task.interrupts for task in state.tasks),
    )
    with service.advisory_lock(agent_thread_id) as lock:
        assert (
            AcceptedCheckpointHeadStore().promote(lock.session, verified, lock=lock)
            is True
        )
    updated_context = _server_context(factory, handle.workspace_id)
    return updated_context, candidate, verified


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

        with service.advisory_lock(agent_thread_id) as lock:
            plan = service.takeover(
                workspace_token=token,
                graph_run_id=handle.graph_run_id,
                input_seq=handle.input_seq,
                auth_session_ref=auth_session_id,
                actor_id="EMP-001",
                lock=lock,
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

        with service.advisory_lock(agent_thread_id) as lock:
            plan = service.takeover(
                workspace_token=token,
                graph_run_id=handle.graph_run_id,
                input_seq=handle.input_seq,
                auth_session_ref=auth_session_id,
                actor_id="EMP-001",
                lock=lock,
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
        token, workspace_id, agent_thread_id, auth_session_id = _workspace_fixture(
            factory
        )
        service = TurnExecutionService(factory)
        handle = service.begin_input(
            workspace_token=token,
            auth_session_ref=auth_session_id,
            actor_id="EMP-001",
            safe_user_text="lock order",
        )
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
                with service.advisory_lock(agent_thread_id) as lock:
                    service.complete_turn_with_event(
                        handle,
                        workspace_token=token,
                        lock=lock,
                        event_type="message.completed",
                        payload={
                            "turn_id": handle.input_turn_id,
                            "message_id": "msg-lock",
                            "content": "ok",
                        },
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


def test_real_checkpoint_exact_end_head_and_no_implicit_latest() -> None:
    with _isolated_checkpoint_database() as settings:
        runtime = PostgresCheckpointRuntime(settings)
        runtime.start()
        runtime.check_readiness()
        factory = build_session_factory(build_engine(settings.database_url))
        token, workspace_id, agent_thread_id, auth_session_id = _workspace_fixture(
            factory
        )
        service = TurnExecutionService(factory)
        handle = service.begin_input(
            workspace_token=token,
            auth_session_ref=auth_session_id,
            actor_id="EMP-001",
            safe_user_text="real checkpoint",
        )
        context, _candidate, _verified = _promote_real_end_head(
            factory, runtime, service, handle, agent_thread_id
        )
        accepted_locator = context.accepted_locator
        assert accepted_locator is not None
        assert runtime.saver is not None
        assert runtime.saver.get_tuple(accepted_locator.as_config()) is not None

        _expire_lease(factory, workspace_id)
        with service.advisory_lock(agent_thread_id) as lock:
            plan = service.takeover(
                workspace_token=token,
                graph_run_id=handle.graph_run_id,
                input_seq=handle.input_seq,
                auth_session_ref=auth_session_id,
                actor_id="EMP-001",
                lock=lock,
                saver=runtime.saver,
            )

        assert plan.source == "checkpoint"
        assert plan.accepted_checkpoint_id == accepted_locator.checkpoint_id

        new_context = _server_context(factory, workspace_id)
        fresh_invocation = FencedPostgresSaverAdapter(runtime.saver).for_execution(
            new_context
        )
        assert fresh_invocation.get_exact(accepted_locator) is not None
        base_config = {
            "configurable": {
                "thread_id": new_context.checkpoint_thread_id,
                "checkpoint_ns": "",
            }
        }
        with pytest.raises(ExactCheckpointRequired):
            fresh_invocation.get_tuple(base_config)
        runtime.close()
        close_all_sessions()


def test_takeover_missing_accepted_head_fails_closed_with_real_saver() -> None:
    with _isolated_checkpoint_database() as settings:
        runtime = PostgresCheckpointRuntime(settings)
        runtime.start()
        runtime.check_readiness()
        factory = build_session_factory(build_engine(settings.database_url))
        token, workspace_id, agent_thread_id, auth_session_id = _workspace_fixture(
            factory
        )
        service = TurnExecutionService(factory)
        handle = service.begin_input(
            workspace_token=token,
            auth_session_ref=auth_session_id,
            actor_id="EMP-001",
            safe_user_text="missing head",
        )
        missing = "missing-head"
        with factory() as session:
            execution = session.scalar(
                select(AgentTurnExecutionRecord).where(
                    AgentTurnExecutionRecord.workspace_id == workspace_id
                )
            )
            assert execution is not None
            execution.accepted_checkpoint_id = missing
            execution.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()

        with service.advisory_lock(agent_thread_id) as lock:
            with pytest.raises(TurnExecutionError, match="accepted checkpoint head is missing"):
                service.takeover(
                    workspace_token=token,
                    graph_run_id=handle.graph_run_id,
                    input_seq=handle.input_seq,
                    auth_session_ref=auth_session_id,
                    actor_id="EMP-001",
                    lock=lock,
                    saver=runtime.saver,
                )

        with factory() as session:
            execution = session.scalar(
                select(AgentTurnExecutionRecord).where(
                    AgentTurnExecutionRecord.workspace_id == workspace_id
                )
            )
            assert execution is not None
            assert execution.attempt == 1
            assert execution.lease_fence == handle.lease_fence
            workspace = session.get(WorkspaceRecord, workspace_id)
            assert workspace is not None
            assert workspace.lease_fence == handle.lease_fence
        runtime.close()
        close_all_sessions()


def test_takeover_historical_accepted_head_forbids_input_event_fallback() -> None:
    with _isolated_checkpoint_database() as settings:
        runtime = PostgresCheckpointRuntime(settings)
        runtime.start()
        runtime.check_readiness()
        factory = build_session_factory(build_engine(settings.database_url))
        token, workspace_id, agent_thread_id, auth_session_id = _workspace_fixture(
            factory
        )
        service = TurnExecutionService(factory)
        first = service.begin_input(
            workspace_token=token,
            auth_session_ref=auth_session_id,
            actor_id="EMP-001",
            safe_user_text="first input",
        )
        first_context, _candidate, _verified = _promote_real_end_head(
            factory, runtime, service, first, agent_thread_id
        )
        accepted_id = first_context.accepted_checkpoint_id
        assert accepted_id is not None

        with service.advisory_lock(agent_thread_id) as lock:
            service.complete_turn_with_event(
                first,
                workspace_token=token,
                lock=lock,
                event_type="message.completed",
                payload={
                    "turn_id": first.input_turn_id,
                    "message_id": "msg-historical",
                    "content": "ok",
                },
            )

        second = service.begin_input(
            workspace_token=token,
            auth_session_ref=auth_session_id,
            actor_id="EMP-001",
            safe_user_text="second input",
        )
        assert second.graph_run_id == first.graph_run_id
        assert second.input_seq == first.input_seq + 1
        assert second.accepted_checkpoint_id is None
        _expire_lease(factory, workspace_id)

        with service.advisory_lock(agent_thread_id) as lock:
            plan = service.takeover(
                workspace_token=token,
                graph_run_id=second.graph_run_id,
                input_seq=second.input_seq,
                auth_session_ref=auth_session_id,
                actor_id="EMP-001",
                lock=lock,
                saver=runtime.saver,
            )

        assert plan.source == "checkpoint"
        assert plan.accepted_checkpoint_id == accepted_id
        with factory() as session:
            execution = session.scalar(
                select(AgentTurnExecutionRecord).where(
                    AgentTurnExecutionRecord.id == second.execution_id
                )
            )
            assert execution is not None
            assert execution.accepted_checkpoint_id == accepted_id
        runtime.close()
        close_all_sessions()


def test_stale_owner_head_and_terminal_zero_write_after_takeover() -> None:
    with _isolated_checkpoint_database() as settings:
        runtime = PostgresCheckpointRuntime(settings)
        runtime.start()
        runtime.check_readiness()
        factory = build_session_factory(build_engine(settings.database_url))
        token, workspace_id, agent_thread_id, auth_session_id = _workspace_fixture(
            factory
        )
        service = TurnExecutionService(factory)
        old_handle = service.begin_input(
            workspace_token=token,
            auth_session_ref=auth_session_id,
            actor_id="EMP-001",
            safe_user_text="stale zero writes",
        )
        context, _candidate, old_verified = _promote_real_end_head(
            factory, runtime, service, old_handle, agent_thread_id
        )
        accepted_id = context.accepted_checkpoint_id
        assert accepted_id is not None
        event_count_before = _count_events(factory, workspace_id)
        _expire_lease(factory, workspace_id)

        with service.advisory_lock(agent_thread_id) as lock:
            plan = service.takeover(
                workspace_token=token,
                graph_run_id=old_handle.graph_run_id,
                input_seq=old_handle.input_seq,
                auth_session_ref=auth_session_id,
                actor_id="EMP-001",
                lock=lock,
                saver=runtime.saver,
            )
            assert plan.source == "checkpoint"

            # Head promotion by the stale owner must be a zero-write.
            assert (
                AcceptedCheckpointHeadStore().promote(
                    lock.session, old_verified, lock=lock
                )
                is False
            )

            # Terminal write by the stale owner must be a zero-write: the
            # terminal event is created in the same fenced transaction as the
            # execution terminalize, so a stale owner must not add any event.
            with pytest.raises(StaleTurnFenceError):
                service.complete_turn_with_event(
                    old_handle,
                    workspace_token=token,
                    lock=lock,
                    event_type="message.completed",
                    payload={
                        "turn_id": old_handle.input_turn_id,
                        "message_id": "msg-stale",
                        "content": "ok",
                    },
                )

        assert _count_events(factory, workspace_id) == event_count_before
        with factory() as session:
            execution = session.scalar(
                select(AgentTurnExecutionRecord).where(
                    AgentTurnExecutionRecord.workspace_id == workspace_id
                )
            )
            assert execution is not None
            assert execution.accepted_checkpoint_id == accepted_id
            assert execution.terminal_event_id is None
            assert execution.status == "running"
        runtime.close()
        close_all_sessions()


def test_takeover_runner_promote_finalize_combined_real_checkpoint() -> None:
    with _isolated_checkpoint_database() as settings:
        runtime = PostgresCheckpointRuntime(settings)
        runtime.start()
        runtime.check_readiness()
        factory = build_session_factory(build_engine(settings.database_url))
        token, workspace_id, agent_thread_id, auth_session_id = _workspace_fixture(
            factory
        )
        service = TurnExecutionService(factory)
        old_handle = service.begin_input(
            workspace_token=token,
            auth_session_ref=auth_session_id,
            actor_id="EMP-001",
            safe_user_text="combined",
        )
        _old_context, _old_candidate, _old_verified = _promote_real_interrupt_head(
            factory, runtime, service, old_handle, agent_thread_id
        )
        _expire_lease(factory, workspace_id)

        with service.advisory_lock(agent_thread_id) as lock:
            plan = service.takeover(
                workspace_token=token,
                graph_run_id=old_handle.graph_run_id,
                input_seq=old_handle.input_seq,
                auth_session_ref=auth_session_id,
                actor_id="EMP-001",
                lock=lock,
                saver=runtime.saver,
            )
            assert plan.source == "checkpoint"

            new_handle = TurnExecutionHandle(
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
                auth_session_ref=auth_session_id,
                accepted_checkpoint_id=plan.accepted_checkpoint_id,
            )

            new_context = _server_context(factory, workspace_id)
            accepted_locator = new_context.accepted_locator
            assert accepted_locator is not None
            invocation = FencedPostgresSaverAdapter(runtime.saver).for_execution(
                new_context
            )
            graph = _interrupt_graph(invocation)
            runner = FencedGraphTurnRunner(graph, service, heartbeat_interval=0.05)
            runner.run(
                new_handle,
                Command(resume="ok"),
                context={},
                lock=lock,
                config=accepted_locator.as_config(),
            )

            candidate = invocation.candidate
            assert candidate is not None
            verified = invocation.verify_candidate(
                candidate,
                graph_stopped=True,
                graph_state_reader=graph.get_state,
                state_validator=lambda state: not state.next,
            )
            assert (
                AcceptedCheckpointHeadStore().promote(lock.session, verified, lock=lock)
                is True
            )
            service.complete_turn_with_event(
                new_handle,
                workspace_token=token,
                lock=lock,
                event_type="message.completed",
                payload={
                    "turn_id": new_handle.input_turn_id,
                    "message_id": "msg-combined",
                    "content": "ok",
                },
            )

        with factory() as session:
            execution = session.scalar(
                select(AgentTurnExecutionRecord).where(
                    AgentTurnExecutionRecord.workspace_id == workspace_id
                )
            )
            assert execution is not None
            assert execution.status == "completed"
            assert execution.lease_expires_at is None
            assert execution.terminal_event_id is not None
            assert execution.accepted_checkpoint_id == verified.locator.checkpoint_id
        runtime.close()
        close_all_sessions()
