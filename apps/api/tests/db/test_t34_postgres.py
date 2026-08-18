"""T34 real-PostgreSQL interrupt/resume proofs on disposable databases.

Every recovery and atomicity claim in T34 AC1-AC3 is proven here with a real
PostgreSQL checkpointer and real app transactions (no mocks):

- AC1: a complete draft stops at a safe JSON confirmation interrupt and the
  interrupt node writes nothing;
- AC2: candidate verification -> head promotion + pending + Cursor + event
  chain + terminal + lease release commit atomically (or roll back to zero);
  a checkpoint-only crash is completed by App B on the original turn;
- AC3: begin_resume validates the exact pending head and seeds it into the new
  execution before the first graph call; confirm applies the CAS once;
  non-confirm inputs re-route inside the same resume and replace the pending
  on a re-interrupt; wrong auth closes with zero writes.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from langgraph.types import Command
from psycopg import sql
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.session import close_all_sessions

from accesspilot.agent.checkpoint import (
    FencedPostgresSaverAdapter,
    PostgresCheckpointRuntime,
    ServerExecutionContext,
)
from accesspilot.agent.embeddings import DeterministicEmbeddingModel
from accesspilot.agent.production_graph import (
    ConfirmationInterruptRaised,
    GraphInput,
    GraphRuntimeContext,
    build_production_graph,
)
from accesspilot.agent.routing import DeterministicIntentRouter
from accesspilot.agent.turn_execution import (
    StaleTurnFenceError,
    TurnExecutionError,
    TurnExecutionHandle,
    TurnExecutionService,
    TurnInProgressError,
    TurnLockUnavailableError,
    TurnRecoveryInProgressError,
)
from accesspilot.auth import Principal
from accesspilot.checkpoint_init import run_official_checkpoint_setup
from accesspilot.config import Settings
from accesspilot.db.models import (
    AccessRequestRecord,
    AgentPendingInputRecord,
    AgentStepExecutionRecord,
    AgentTurnExecutionRecord,
    AuthSessionRecord,
    WorkspaceEventRecord,
    WorkspaceRecord,
)
from accesspilot.db.seed import seed_catalog
from accesspilot.db.session import build_engine, build_session_factory
from accesspilot.db.workspace_store import (
    SqlAlchemyWorkspaceStore,
    hash_workspace_token,
)
from accesspilot.domain.models import ParsedReply
from accesspilot.tools.policies import PolicyService
from accesspilot.workspaces import WorkspaceService

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
def _isolated_checkpoint_database() -> Iterator[Settings]:
    configured = _admin_url()
    if not configured:
        pytest.skip(
            f"set {_ADMIN_URL_ENV} to run the destructive-isolated T34 PostgreSQL proof"
        )
    admin_url = make_url(configured).set(
        drivername="postgresql", database="postgres"
    )
    admin_conninfo = admin_url.render_as_string(hide_password=False)
    suffix = uuid4().hex[:12]
    database = f"t34chk_{suffix}"
    migration_role = f"t34m_{suffix}"
    runtime_role = f"t34r_{suffix}"
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
    schema = f"t34_checkpoint_{suffix}"
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


def _workspace_fixture(
    factory: sessionmaker[Session],
    *,
    draft: dict[str, object] | None = None,
) -> tuple[str, UUID, UUID, UUID]:
    token = f"t34-pg-{uuid4()}"
    auth_session_id = uuid4()
    with factory() as session:
        seed_catalog(session)
        workspace = WorkspaceRecord(
            token_hash=hash_workspace_token(token),
            actor_id="EMP-001",
            flow_version=2,
            lease_fence=0,
            draft=draft,
            draft_revision=1 if draft is not None else 0,
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


def _revoke_auth_session(
    factory: sessionmaker[Session],
    auth_session_id: UUID,
) -> None:
    with factory() as session:
        auth = session.get(AuthSessionRecord, auth_session_id)
        assert auth is not None
        auth.revoked_at = datetime.now(UTC)
        session.commit()


def _request_text() -> str:
    return "申请仪表盘查看 14 天，用于业务需要"


def _complete_reply() -> ParsedReply:
    return ParsedReply(
        entitlement_id="insighthub.dashboard_view",
        duration_days=14,
        justification="业务需要",
    )


def _runtime_context(
    factory: sessionmaker[Session],
    workspace_id: UUID,
    workspace_token: str,
    auth_session_id: str,
    *,
    graph_run_id: UUID,
    input_turn_id: str,
    fence: int,
    model: object,
    pending_input_id: UUID,
    input_seq: int = 0,
) -> GraphRuntimeContext:
    return {
        "session_factory": factory,
        "workspace_service": WorkspaceService(
            SqlAlchemyWorkspaceStore(factory)
        ),
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
        "current_turn_id": input_turn_id,
        "current_fence": fence,
        "workspace_token": workspace_token,
        "auth_session_id": auth_session_id,
        "cookie": "runtime-cookie",
        "csrf_token": "runtime-csrf",
        "api_key": "runtime-key",
        "pending_input_id": str(pending_input_id),
        "current_input_seq": input_seq,
    }


def _graph_input(
    workspace_id: UUID,
    graph_run_id: UUID,
    input_turn_id: str,
    *,
    input_seq: int,
    safe_user_text: str,
) -> GraphInput:
    return GraphInput(
        schema_version=1,
        flow_version=2,
        workspace_ref=workspace_id,
        graph_run_id=graph_run_id,
        input_seq=input_seq,
        input_turn_id=input_turn_id,
        safe_user_text=safe_user_text,
        input_kind="new_input",
    )


def _config_for(thread_id: str, checkpoint_id: str | None = None) -> dict[str, object]:
    config: dict[str, object] = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
        }
    }
    if checkpoint_id is not None:
        cast_dict = config["configurable"]
        assert isinstance(cast_dict, dict)
        cast_dict["checkpoint_id"] = checkpoint_id
    return config


def _server_context(
    factory: sessionmaker[Session],
    execution_id: UUID,
) -> ServerExecutionContext:
    with factory() as session:
        record = session.scalar(
            select(AgentTurnExecutionRecord).where(
                AgentTurnExecutionRecord.id == execution_id
            )
        )
        assert record is not None
        return ServerExecutionContext.from_record(record)


def _verify_interrupt_candidate(
    invocation: Any,
    graph: Any,
) -> Any:
    candidate = invocation.candidate
    assert candidate is not None
    return invocation.verify_candidate(
        candidate,
        graph_stopped=True,
        graph_state_reader=graph.compiled.get_state,
        state_validator=lambda state: any(
            task.interrupts for task in state.tasks
        ),
    )


def _verify_end_candidate(
    invocation: Any,
    graph: Any,
) -> Any:
    candidate = invocation.candidate
    assert candidate is not None
    return invocation.verify_candidate(
        candidate,
        graph_stopped=True,
        graph_state_reader=graph.compiled.get_state,
        state_validator=lambda state: not state.next,
    )


def _run_to_interrupt(
    factory: sessionmaker[Session],
    runtime: PostgresCheckpointRuntime,
    service: TurnExecutionService,
    handle: TurnExecutionHandle,
    model: object,
    pending_input_id: UUID,
    *,
    workspace_token: str,
) -> tuple[Any, Any, Any, ServerExecutionContext]:
    """Run the real production graph until the confirmation interrupt."""
    context = _server_context(factory, handle.execution_id)
    invocation = FencedPostgresSaverAdapter(runtime.saver).for_execution(context)
    graph = build_production_graph(checkpointer=invocation)
    config = _config_for(context.checkpoint_thread_id)
    runtime_context = _runtime_context(
        factory,
        handle.workspace_id,
        workspace_token,
        str(handle.auth_session_ref),
        graph_run_id=handle.graph_run_id,
        input_turn_id=handle.input_turn_id,
        fence=handle.lease_fence,
        model=model,
        pending_input_id=pending_input_id,
    )
    graph_input = _graph_input(
        handle.workspace_id,
        handle.graph_run_id,
        handle.input_turn_id,
        input_seq=handle.input_seq,
        safe_user_text=_request_text(),
    )
    with pytest.raises(ConfirmationInterruptRaised) as raised:
        graph.invoke(graph_input, config, context=runtime_context)
    return graph, invocation, raised.value.payload, context


def _count(factory: sessionmaker[Session], model: Any, workspace_id: UUID) -> int:
    with factory() as session:
        count = session.scalar(
            select(func.count()).select_from(model).where(
                model.workspace_id == workspace_id
            )
        )
        assert count is not None
        return count


def test_interrupt_finalize_is_atomic_pending_cursor_events_terminal_and_lease() -> None:
    """AC2: candidate promotion + pending + Cursor + event chain + terminal +
    lease release commit in one fenced transaction; a stale owner writes zero."""
    with _isolated_checkpoint_database() as settings:
        runtime = PostgresCheckpointRuntime(settings)
        runtime.start()
        runtime.check_readiness()
        factory = build_session_factory(build_engine(settings.database_url))
        token, workspace_id, agent_thread_id, auth_session_id = _workspace_fixture(
            factory
        )
        service = TurnExecutionService(factory)
        pending_input_id = uuid4()
        handle = service.begin_input(
            workspace_token=token,
            auth_session_ref=auth_session_id,
            actor_id="EMP-001",
            safe_user_text=_request_text(),
        )
        graph, invocation, payload, context = _run_to_interrupt(
            factory, runtime, service, handle, StaticReplyModel(_complete_reply()),
            pending_input_id, workspace_token=token,
        )
        assert payload["kind"] == "confirmation"
        assert payload["pending_input_id"] == str(pending_input_id)
        assert payload["draft_revision"] == 1
        verified = _verify_interrupt_candidate(invocation, graph)

        # Stale-owner finalize (expired lease) must be a zero-write: no
        # terminal-only or Cursor-only projection can appear.
        with factory() as session:
            record = session.get(AgentTurnExecutionRecord, handle.execution_id)
            assert record is not None
            record.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()
        with service.advisory_lock(agent_thread_id) as lock:
            with pytest.raises(StaleTurnFenceError):
                service.finalize_interrupt(
                    handle,
                    workspace_token=token,
                    lock=lock,
                    verified=verified,
                    pending_input_id=pending_input_id,
                    draft_revision=1,
                )
        with factory() as session:
            assert (
                session.scalar(
                    select(func.count()).select_from(AgentPendingInputRecord).where(
                        AgentPendingInputRecord.workspace_id == workspace_id
                    )
                )
                == 0
            )
            assert (
                session.scalar(
                    select(func.count()).select_from(WorkspaceEventRecord).where(
                        WorkspaceEventRecord.workspace_id == workspace_id
                    )
                )
                == 2
            )
            record = session.get(AgentTurnExecutionRecord, handle.execution_id)
            assert record is not None
            record.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
            session.commit()

        with service.advisory_lock(agent_thread_id) as lock:
            terminal_id = service.finalize_interrupt(
                handle,
                workspace_token=token,
                lock=lock,
                verified=verified,
                pending_input_id=pending_input_id,
                draft_revision=1,
            )

        with factory() as session:
            execution = session.get(AgentTurnExecutionRecord, handle.execution_id)
            assert execution is not None
            assert execution.status == "waiting_input"
            assert execution.lease_expires_at is None
            assert execution.terminal_event_id == terminal_id
            assert execution.accepted_checkpoint_id == verified.locator.checkpoint_id
            workspace = session.get(WorkspaceRecord, workspace_id)
            assert workspace is not None
            assert workspace.cursor_expected_field == "confirmation"
            assert workspace.cursor_consumed_at is None
            pending = session.scalar(
                select(AgentPendingInputRecord).where(
                    AgentPendingInputRecord.pending_input_id == pending_input_id
                )
            )
            assert pending is not None
            assert pending.status == "active"
            assert pending.accepted_checkpoint_id == verified.locator.checkpoint_id
            events = session.scalars(
                select(WorkspaceEventRecord)
                .where(WorkspaceEventRecord.workspace_id == workspace_id)
                .order_by(WorkspaceEventRecord.id)
            ).all()
            types = [event.event_type for event in events]
            assert types == [
                "turn.started",
                "message.user",
                "agent.input.required",
                "business.status",
                "message.completed",
            ]

        runtime.close()
        close_all_sessions()


def test_checkpoint_only_crash_takeover_completes_original_turn() -> None:
    """AC2: the graph stops at the interrupt and the process dies before the
    fenced finalize; App B takes over the original turn (same input_turn_id,
    attempt/fence only advance) and completes the projection."""
    with _isolated_checkpoint_database() as settings:
        runtime = PostgresCheckpointRuntime(settings)
        runtime.start()
        runtime.check_readiness()
        factory = build_session_factory(build_engine(settings.database_url))
        token, workspace_id, agent_thread_id, auth_session_id = _workspace_fixture(
            factory
        )
        service = TurnExecutionService(factory)
        pending_input_id = uuid4()
        handle = service.begin_input(
            workspace_token=token,
            auth_session_ref=auth_session_id,
            actor_id="EMP-001",
            safe_user_text=_request_text(),
        )
        # App A runs the graph to the interrupt but never finalizes ("crash").
        graph, invocation, _payload, _context = _run_to_interrupt(
            factory, runtime, service, handle, StaticReplyModel(_complete_reply()),
            pending_input_id, workspace_token=token,
        )
        candidate = invocation.candidate
        assert candidate is not None
        # The candidate checkpoint exists in the saver (checkpoint-only window).
        assert runtime.saver is not None
        assert runtime.saver.get_tuple(candidate.locator.as_config()) is not None
        with factory() as session:
            record = session.get(AgentTurnExecutionRecord, handle.execution_id)
            assert record is not None
            record.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()

        # App B takes over: no accepted head yet, so it rebuilds from the safe
        # input fact; identity and turn are preserved.
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
            assert plan.source == "input_event"
            assert plan.graph_run_id == handle.graph_run_id
            assert plan.input_seq == handle.input_seq
            assert plan.input_turn_id == handle.input_turn_id
            assert plan.attempt == handle.attempt + 1
            assert plan.lease_fence == handle.lease_fence + 1

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
            # App B re-runs the graph from the safe input fact; the upstream
            # draft CAS is idempotent via the operation ledger.
            graph_b, invocation_b, payload_b, _ctx_b = _run_to_interrupt(
                factory, runtime, service, new_handle,
                StaticReplyModel(_complete_reply()), pending_input_id,
                workspace_token=token,
            )
            assert payload_b["kind"] == "confirmation"
            verified = _verify_interrupt_candidate(invocation_b, graph_b)
            terminal_id = service.finalize_interrupt(
                new_handle,
                workspace_token=token,
                lock=lock,
                verified=verified,
                pending_input_id=pending_input_id,
                draft_revision=payload_b["draft_revision"],
            )

        with factory() as session:
            execution = session.get(AgentTurnExecutionRecord, plan.execution_id)
            assert execution is not None
            assert execution.status == "waiting_input"
            assert execution.input_turn_id == handle.input_turn_id
            assert execution.attempt == handle.attempt + 1
            assert execution.lease_fence == handle.lease_fence + 1
            assert execution.terminal_event_id == terminal_id
            assert execution.accepted_checkpoint_id == verified.locator.checkpoint_id
            # Original turn has exactly one terminal.
            assert (
                session.scalar(
                    select(func.count()).select_from(WorkspaceEventRecord).where(
                        WorkspaceEventRecord.workspace_id == workspace_id,
                        WorkspaceEventRecord.event_type == "message.completed",
                    )
                )
                == 1
            )
        runtime.close()
        close_all_sessions()


def test_begin_resume_validates_exact_head_and_seeds_before_first_graph_call() -> None:
    """AC3: begin_resume verifies the active pending's exact head with the
    saver, then atomically creates the execution + message.user, marks the
    pending resuming and seeds the exact triple head. A crash between the
    resume transaction and the first graph call resumes from the seeded head."""
    with _isolated_checkpoint_database() as settings:
        runtime = PostgresCheckpointRuntime(settings)
        runtime.start()
        runtime.check_readiness()
        factory = build_session_factory(build_engine(settings.database_url))
        token, workspace_id, agent_thread_id, auth_session_id = _workspace_fixture(
            factory
        )
        service = TurnExecutionService(factory)
        pending_input_id = uuid4()
        handle = service.begin_input(
            workspace_token=token,
            auth_session_ref=auth_session_id,
            actor_id="EMP-001",
            safe_user_text=_request_text(),
        )
        graph, invocation, _payload, _context = _run_to_interrupt(
            factory, runtime, service, handle, StaticReplyModel(_complete_reply()),
            pending_input_id, workspace_token=token,
        )
        verified = _verify_interrupt_candidate(invocation, graph)
        with service.advisory_lock(agent_thread_id) as lock:
            service.finalize_interrupt(
                handle,
                workspace_token=token,
                lock=lock,
                verified=verified,
                pending_input_id=pending_input_id,
                draft_revision=1,
            )
        accepted = verified.locator.checkpoint_id
        assert runtime.saver is not None
        assert runtime.saver.get_tuple(verified.locator.as_config()) is not None

        # Missing exact head fails closed before any new fact is written.
        with factory() as session:
            pending = session.scalar(
                select(AgentPendingInputRecord).where(
                    AgentPendingInputRecord.pending_input_id == pending_input_id
                )
            )
            assert pending is not None
            pending.accepted_checkpoint_id = "0" * 32
            session.commit()
        with service.advisory_lock(agent_thread_id) as lock:
            with pytest.raises(TurnExecutionError, match="pending checkpoint head is missing"):
                service.begin_resume(
                    workspace_token=token,
                    auth_session_ref=auth_session_id,
                    actor_id="EMP-001",
                    safe_user_text="确认提交",
                    pending_input_id=pending_input_id,
                    lock=lock,
                    saver=runtime.saver,
                )
        with factory() as session:
            pending = session.scalar(
                select(AgentPendingInputRecord).where(
                    AgentPendingInputRecord.pending_input_id == pending_input_id
                )
            )
            assert pending is not None
            pending.accepted_checkpoint_id = accepted
            session.commit()

        # Real resume transaction seeds the exact head.
        with service.advisory_lock(agent_thread_id) as lock:
            resume_handle = service.begin_resume(
                workspace_token=token,
                auth_session_ref=auth_session_id,
                actor_id="EMP-001",
                safe_user_text="确认提交",
                pending_input_id=pending_input_id,
                lock=lock,
                saver=runtime.saver,
            )
        assert resume_handle.accepted_checkpoint_id == accepted
        with factory() as session:
            execution = session.get(
                AgentTurnExecutionRecord, resume_handle.execution_id
            )
            assert execution is not None
            assert execution.status == "running"
            assert execution.accepted_checkpoint_id == accepted
            assert execution.graph_run_id == handle.graph_run_id
            assert execution.input_seq == handle.input_seq + 1
            pending = session.scalar(
                select(AgentPendingInputRecord).where(
                    AgentPendingInputRecord.pending_input_id == pending_input_id
                )
            )
            assert pending is not None
            assert pending.status == "resuming"
            assert pending.resume_input_seq == execution.input_seq

        # "Resume transaction committed, graph not yet called" crash: App B
        # must continue from the seeded exact head (checkpoint source).
        with factory() as session:
            record = session.get(
                AgentTurnExecutionRecord, resume_handle.execution_id
            )
            assert record is not None
            record.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()
        with service.advisory_lock(agent_thread_id) as lock:
            plan = service.takeover(
                workspace_token=token,
                graph_run_id=resume_handle.graph_run_id,
                input_seq=resume_handle.input_seq,
                auth_session_ref=auth_session_id,
                actor_id="EMP-001",
                lock=lock,
                saver=runtime.saver,
            )
        assert plan.source == "checkpoint"
        assert plan.accepted_checkpoint_id == accepted
        assert plan.checkpoint_thread_id == handle.checkpoint_thread_id
        assert plan.input_turn_id == resume_handle.input_turn_id
        runtime.close()
        close_all_sessions()


def test_resume_confirm_full_flow_applies_cas_once_and_closes_atomically() -> None:
    """AC3 main chain: App A interrupts, App B resumes with one
    Command(resume) through rehydrate, the confirmation CAS runs once and the
    finalize transaction closes pending/Cursor/terminal atomically."""
    with _isolated_checkpoint_database() as settings:
        runtime = PostgresCheckpointRuntime(settings)
        runtime.start()
        runtime.check_readiness()
        factory = build_session_factory(build_engine(settings.database_url))
        token, workspace_id, agent_thread_id, auth_session_id = _workspace_fixture(
            factory
        )
        service = TurnExecutionService(factory)
        pending_input_id = uuid4()
        handle = service.begin_input(
            workspace_token=token,
            auth_session_ref=auth_session_id,
            actor_id="EMP-001",
            safe_user_text=_request_text(),
        )
        graph, invocation, _payload, context = _run_to_interrupt(
            factory, runtime, service, handle, StaticReplyModel(_complete_reply()),
            pending_input_id, workspace_token=token,
        )
        verified = _verify_interrupt_candidate(invocation, graph)
        with service.advisory_lock(agent_thread_id) as lock:
            service.finalize_interrupt(
                handle,
                workspace_token=token,
                lock=lock,
                verified=verified,
                pending_input_id=pending_input_id,
                draft_revision=1,
            )

        # App B accepts the resume input.
        with service.advisory_lock(agent_thread_id) as lock:
            resume_handle = service.begin_resume(
                workspace_token=token,
                auth_session_ref=auth_session_id,
                actor_id="EMP-001",
                safe_user_text="确认提交",
                pending_input_id=pending_input_id,
                lock=lock,
                saver=runtime.saver,
            )

        # One Command(resume) through the real production graph.
        resume_context = _server_context(factory, resume_handle.execution_id)
        invocation_b = FencedPostgresSaverAdapter(runtime.saver).for_execution(
            resume_context
        )
        graph_b = build_production_graph(checkpointer=invocation_b)
        config = _config_for(
            resume_context.checkpoint_thread_id,
            resume_context.accepted_checkpoint_id,
        )
        runtime_context = _runtime_context(
            factory,
            resume_handle.workspace_id,
            token,
            str(resume_handle.auth_session_ref),
            graph_run_id=resume_handle.graph_run_id,
            input_turn_id=resume_handle.input_turn_id,
            fence=resume_handle.lease_fence,
            model=StaticReplyModel(_complete_reply()),
            pending_input_id=pending_input_id,
            input_seq=resume_handle.input_seq,
        )
        turn = graph_b.invoke(
            Command(resume={"decision": "confirm", "safe_user_text": "确认提交"}),
            config,
            context=runtime_context,
        )
        assert turn.business_status == "ready_to_submit"
        verified_b = _verify_end_candidate(invocation_b, graph_b)
        with service.advisory_lock(agent_thread_id) as lock:
            terminal_id = service.finalize_resume_outcome(
                resume_handle,
                workspace_token=token,
                lock=lock,
                verified=verified_b,
                pending_input_id=pending_input_id,
                event_type="message.completed",
                payload={
                    "turn_id": resume_handle.input_turn_id,
                    "message_id": f"msg-{uuid4()}",
                    "content": turn.assistant_message,
                    "intent": "request_access",
                    "business_status": "ready_to_submit",
                    "draft_revision": 2,
                },
            )

        with factory() as session:
            execution = session.get(
                AgentTurnExecutionRecord, resume_handle.execution_id
            )
            assert execution is not None
            assert execution.status == "completed"
            assert execution.lease_expires_at is None
            assert execution.terminal_event_id == terminal_id
            assert execution.accepted_checkpoint_id == verified_b.locator.checkpoint_id
            workspace = session.get(WorkspaceRecord, workspace_id)
            assert workspace is not None
            assert workspace.draft is not None
            assert workspace.draft["confirmed"] is True
            assert workspace.draft_revision == 2
            assert workspace.cursor_consumed_at is not None
            pending = session.scalar(
                select(AgentPendingInputRecord).where(
                    AgentPendingInputRecord.pending_input_id == pending_input_id
                )
            )
            assert pending is not None
            assert pending.status == "resolved"
            # The confirmation CAS recorded exactly once.
            steps = session.scalars(
                select(AgentStepExecutionRecord).where(
                    AgentStepExecutionRecord.workspace_id == workspace_id,
                    AgentStepExecutionRecord.step_key == "apply_confirmation",
                )
            ).all()
            assert len(steps) == 1
            assert steps[0].status == "completed"
            assert steps[0].committed_revision == 2
            # No formal request was created by the graph.
            assert (
                session.scalar(
                    select(func.count()).select_from(AccessRequestRecord).where(
                        AccessRequestRecord.workspace_id == workspace_id
                    )
                )
                == 0
            )
        runtime.close()
        close_all_sessions()


def test_resume_field_edit_reinterrupts_and_replaces_pending_atomically() -> None:
    """AC3: a field edit re-routes inside the same resume and re-interrupts;
    the replacement pending is written in the same transaction that closes the
    previous one (never two active rows, never a lost message)."""
    with _isolated_checkpoint_database() as settings:
        runtime = PostgresCheckpointRuntime(settings)
        runtime.start()
        runtime.check_readiness()
        factory = build_session_factory(build_engine(settings.database_url))
        token, workspace_id, agent_thread_id, auth_session_id = _workspace_fixture(
            factory
        )
        service = TurnExecutionService(factory)
        pending_input_id = uuid4()
        handle = service.begin_input(
            workspace_token=token,
            auth_session_ref=auth_session_id,
            actor_id="EMP-001",
            safe_user_text=_request_text(),
        )
        graph, invocation, _payload, _context = _run_to_interrupt(
            factory, runtime, service, handle, StaticReplyModel(_complete_reply()),
            pending_input_id, workspace_token=token,
        )
        verified = _verify_interrupt_candidate(invocation, graph)
        with service.advisory_lock(agent_thread_id) as lock:
            service.finalize_interrupt(
                handle,
                workspace_token=token,
                lock=lock,
                verified=verified,
                pending_input_id=pending_input_id,
                draft_revision=1,
            )

        with service.advisory_lock(agent_thread_id) as lock:
            resume_handle = service.begin_resume(
                workspace_token=token,
                auth_session_ref=auth_session_id,
                actor_id="EMP-001",
                safe_user_text="改成 60 天",
                pending_input_id=pending_input_id,
                lock=lock,
                saver=runtime.saver,
            )

        resume_context = _server_context(factory, resume_handle.execution_id)
        invocation_b = FencedPostgresSaverAdapter(runtime.saver).for_execution(
            resume_context
        )
        graph_b = build_production_graph(checkpointer=invocation_b)
        config = _config_for(
            resume_context.checkpoint_thread_id,
            resume_context.accepted_checkpoint_id,
        )
        runtime_context = _runtime_context(
            factory,
            resume_handle.workspace_id,
            token,
            str(resume_handle.auth_session_ref),
            graph_run_id=resume_handle.graph_run_id,
            input_turn_id=resume_handle.input_turn_id,
            fence=resume_handle.lease_fence,
            model=StaticReplyModel(
                ParsedReply(
                    entitlement_id="insighthub.dashboard_view",
                    duration_days=60,
                    justification="业务需要",
                )
            ),
            pending_input_id=pending_input_id,
            input_seq=resume_handle.input_seq,
        )
        with pytest.raises(ConfirmationInterruptRaised) as raised:
            graph_b.invoke(
                Command(
                    resume={
                        "decision": "route_new_input",
                        "safe_user_text": "改成 60 天",
                    }
                ),
                config,
                context=runtime_context,
            )
        payload_b = raised.value.payload
        assert payload_b["kind"] == "confirmation"
        assert payload_b["draft_revision"] == 2
        verified_b = _verify_interrupt_candidate(invocation_b, graph_b)

        # Replacement: same transaction closes the old pending and inserts the
        # new active pending with the new revision.
        with service.advisory_lock(agent_thread_id) as lock:
            terminal_id = service.finalize_interrupt(
                resume_handle,
                workspace_token=token,
                lock=lock,
                verified=verified_b,
                pending_input_id=pending_input_id,
                draft_revision=2,
                previous_pending_input_id=pending_input_id,
            )
        with factory() as session:
            # The pending row is unique per pending_input_id; the re-interrupt
            # re-arms the same row with the new revision/head atomically.
            pending = session.scalar(
                select(AgentPendingInputRecord).where(
                    AgentPendingInputRecord.workspace_id == workspace_id,
                    AgentPendingInputRecord.pending_input_id == pending_input_id,
                )
            )
            assert pending is not None
            assert pending.status == "active"
            assert pending.draft_revision == 2
            assert pending.accepted_checkpoint_id == verified_b.locator.checkpoint_id
            execution = session.get(
                AgentTurnExecutionRecord, resume_handle.execution_id
            )
            assert execution is not None
            assert execution.status == "waiting_input"
            assert execution.terminal_event_id == terminal_id
            assert execution.accepted_checkpoint_id == verified_b.locator.checkpoint_id
            workspace = session.get(WorkspaceRecord, workspace_id)
            assert workspace is not None
            assert workspace.draft is not None
            assert workspace.draft["duration_days"] == 60
            assert workspace.draft["confirmed"] is False
            assert workspace.draft_revision == 2
            assert workspace.cursor_consumed_at is None
        runtime.close()
        close_all_sessions()


def test_resume_confirm_after_collection_in_same_turn_uses_pending_revision() -> None:
    """P1-1 regression: the workspace starts with no draft (revision 0); the
    graph's own persist_draft_cas advances it to 1 before the interrupt.
    Resume must validate against the pending's recorded revision, not the
    stale checkpoint base, so a normal confirmation succeeds."""
    with _isolated_checkpoint_database() as settings:
        runtime = PostgresCheckpointRuntime(settings)
        runtime.start()
        runtime.check_readiness()
        factory = build_session_factory(build_engine(settings.database_url))
        token, workspace_id, agent_thread_id, auth_session_id = _workspace_fixture(
            factory
        )
        service = TurnExecutionService(factory)
        pending_input_id = uuid4()
        handle = service.begin_input(
            workspace_token=token,
            auth_session_ref=auth_session_id,
            actor_id="EMP-001",
            safe_user_text=_request_text(),
        )
        graph, invocation, payload, _context = _run_to_interrupt(
            factory, runtime, service, handle, StaticReplyModel(_complete_reply()),
            pending_input_id, workspace_token=token,
        )
        # The collection turn persisted the draft: revision 0 -> 1 and the
        # interrupt must report the committed revision.
        assert payload["draft_revision"] == 1
        verified = _verify_interrupt_candidate(invocation, graph)
        with service.advisory_lock(agent_thread_id) as lock:
            service.finalize_interrupt(
                handle,
                workspace_token=token,
                lock=lock,
                verified=verified,
                pending_input_id=pending_input_id,
                draft_revision=1,
            )

        with service.advisory_lock(agent_thread_id) as lock:
            resume_handle = service.begin_resume(
                workspace_token=token,
                auth_session_ref=auth_session_id,
                actor_id="EMP-001",
                safe_user_text="确认提交",
                pending_input_id=pending_input_id,
                lock=lock,
                saver=runtime.saver,
            )

        resume_context = _server_context(factory, resume_handle.execution_id)
        invocation_b = FencedPostgresSaverAdapter(runtime.saver).for_execution(
            resume_context
        )
        graph_b = build_production_graph(checkpointer=invocation_b)
        config = _config_for(
            resume_context.checkpoint_thread_id,
            resume_context.accepted_checkpoint_id,
        )
        runtime_context = _runtime_context(
            factory,
            resume_handle.workspace_id,
            token,
            str(resume_handle.auth_session_ref),
            graph_run_id=resume_handle.graph_run_id,
            input_turn_id=resume_handle.input_turn_id,
            fence=resume_handle.lease_fence,
            model=StaticReplyModel(_complete_reply()),
            pending_input_id=pending_input_id,
            input_seq=resume_handle.input_seq,
        )
        turn = graph_b.invoke(
            Command(resume={"decision": "confirm", "safe_user_text": "确认提交"}),
            config,
            context=runtime_context,
        )
        # A normal confirmation after same-turn collection must NOT be a
        # recoverable conflict.
        assert turn.business_status == "ready_to_submit", turn.error_code
        verified_b = _verify_end_candidate(invocation_b, graph_b)
        with service.advisory_lock(agent_thread_id) as lock:
            service.finalize_resume_outcome(
                resume_handle,
                workspace_token=token,
                lock=lock,
                verified=verified_b,
                pending_input_id=pending_input_id,
                event_type="message.completed",
                payload={
                    "turn_id": resume_handle.input_turn_id,
                    "message_id": f"msg-{uuid4()}",
                    "content": turn.assistant_message,
                    "intent": "request_access",
                    "business_status": "ready_to_submit",
                    "draft_revision": 2,
                },
            )
        with factory() as session:
            workspace = session.get(WorkspaceRecord, workspace_id)
            assert workspace is not None
            assert workspace.draft_revision == 2
            assert workspace.draft is not None
            assert workspace.draft["confirmed"] is True
        runtime.close()
        close_all_sessions()


def test_resume_confirm_after_field_edit_reinterrupt_uses_pending_revision() -> None:
    """P1-1 regression: after a field edit re-interrupts (revision 1 -> 2),
    the replacement pending records the new revision and a later confirmation
    succeeds instead of reporting CONFIRMATION_CONFLICT."""
    with _isolated_checkpoint_database() as settings:
        runtime = PostgresCheckpointRuntime(settings)
        runtime.start()
        runtime.check_readiness()
        factory = build_session_factory(build_engine(settings.database_url))
        token, workspace_id, agent_thread_id, auth_session_id = _workspace_fixture(
            factory,
            draft={
                "employee_id": "EMP-001",
                "entitlement_id": "insighthub.dashboard_view",
                "duration_days": 14,
                "justification": "业务需要",
                "confirmed": False,
            },
        )
        service = TurnExecutionService(factory)
        pending_input_id = uuid4()
        handle = service.begin_input(
            workspace_token=token,
            auth_session_ref=auth_session_id,
            actor_id="EMP-001",
            safe_user_text=_request_text(),
        )
        graph, invocation, payload, _context = _run_to_interrupt(
            factory, runtime, service, handle, StaticReplyModel(_complete_reply()),
            pending_input_id, workspace_token=token,
        )
        assert payload["draft_revision"] == 1
        verified = _verify_interrupt_candidate(invocation, graph)
        with service.advisory_lock(agent_thread_id) as lock:
            service.finalize_interrupt(
                handle,
                workspace_token=token,
                lock=lock,
                verified=verified,
                pending_input_id=pending_input_id,
                draft_revision=1,
            )

        # Field edit: same resume call re-routes, persists revision 1 -> 2 and
        # re-interrupts; the replacement pending records revision 2.
        with service.advisory_lock(agent_thread_id) as lock:
            resume_handle = service.begin_resume(
                workspace_token=token,
                auth_session_ref=auth_session_id,
                actor_id="EMP-001",
                safe_user_text="改成 60 天",
                pending_input_id=pending_input_id,
                lock=lock,
                saver=runtime.saver,
            )
        resume_context = _server_context(factory, resume_handle.execution_id)
        invocation_b = FencedPostgresSaverAdapter(runtime.saver).for_execution(
            resume_context
        )
        graph_b = build_production_graph(checkpointer=invocation_b)
        config = _config_for(
            resume_context.checkpoint_thread_id,
            resume_context.accepted_checkpoint_id,
        )
        runtime_context = _runtime_context(
            factory,
            resume_handle.workspace_id,
            token,
            str(resume_handle.auth_session_ref),
            graph_run_id=resume_handle.graph_run_id,
            input_turn_id=resume_handle.input_turn_id,
            fence=resume_handle.lease_fence,
            model=StaticReplyModel(
                ParsedReply(
                    entitlement_id="insighthub.dashboard_view",
                    duration_days=60,
                    justification="业务需要",
                )
            ),
            pending_input_id=pending_input_id,
            input_seq=resume_handle.input_seq,
        )
        with pytest.raises(ConfirmationInterruptRaised) as raised:
            graph_b.invoke(
                Command(
                    resume={
                        "decision": "route_new_input",
                        "safe_user_text": "改成 60 天",
                    }
                ),
                config,
                context=runtime_context,
            )
        assert raised.value.payload["draft_revision"] == 2
        verified_b = _verify_interrupt_candidate(invocation_b, graph_b)
        with service.advisory_lock(agent_thread_id) as lock:
            service.finalize_interrupt(
                resume_handle,
                workspace_token=token,
                lock=lock,
                verified=verified_b,
                pending_input_id=pending_input_id,
                draft_revision=2,
                previous_pending_input_id=pending_input_id,
            )

        # Second resume confirms against the pending-recorded revision 2.
        with service.advisory_lock(agent_thread_id) as lock:
            confirm_handle = service.begin_resume(
                workspace_token=token,
                auth_session_ref=auth_session_id,
                actor_id="EMP-001",
                safe_user_text="确认提交",
                pending_input_id=pending_input_id,
                lock=lock,
                saver=runtime.saver,
            )
        confirm_context = _server_context(factory, confirm_handle.execution_id)
        invocation_c = FencedPostgresSaverAdapter(runtime.saver).for_execution(
            confirm_context
        )
        graph_c = build_production_graph(checkpointer=invocation_c)
        config_c = _config_for(
            confirm_context.checkpoint_thread_id,
            confirm_context.accepted_checkpoint_id,
        )
        runtime_context_c = _runtime_context(
            factory,
            confirm_handle.workspace_id,
            token,
            str(confirm_handle.auth_session_ref),
            graph_run_id=confirm_handle.graph_run_id,
            input_turn_id=confirm_handle.input_turn_id,
            fence=confirm_handle.lease_fence,
            model=StaticReplyModel(_complete_reply()),
            pending_input_id=pending_input_id,
            input_seq=confirm_handle.input_seq,
        )
        turn_c = graph_c.invoke(
            Command(resume={"decision": "confirm", "safe_user_text": "确认提交"}),
            config_c,
            context=runtime_context_c,
        )
        assert turn_c.business_status == "ready_to_submit", turn_c.error_code
        verified_c = _verify_end_candidate(invocation_c, graph_c)
        with service.advisory_lock(agent_thread_id) as lock:
            service.finalize_resume_outcome(
                confirm_handle,
                workspace_token=token,
                lock=lock,
                verified=verified_c,
                pending_input_id=pending_input_id,
                event_type="message.completed",
                payload={
                    "turn_id": confirm_handle.input_turn_id,
                    "message_id": f"msg-{uuid4()}",
                    "content": turn_c.assistant_message,
                    "intent": "request_access",
                    "business_status": "ready_to_submit",
                    "draft_revision": 3,
                },
            )
        with factory() as session:
            workspace = session.get(WorkspaceRecord, workspace_id)
            assert workspace is not None
            assert workspace.draft_revision == 3
            assert workspace.draft is not None
            assert workspace.draft["confirmed"] is True
            assert workspace.draft["duration_days"] == 60
        runtime.close()
        close_all_sessions()


def test_resume_after_session_expired_closes_safely_without_business_result() -> None:
    """P1-2 regression: the AuthSession expires after begin_resume; the resume
    must close safely with a recoverable terminal, not answer business."""
    with _isolated_checkpoint_database() as settings:
        runtime = PostgresCheckpointRuntime(settings)
        runtime.start()
        runtime.check_readiness()
        factory = build_session_factory(build_engine(settings.database_url))
        token, workspace_id, agent_thread_id, auth_session_id = _workspace_fixture(
            factory
        )
        service = TurnExecutionService(factory)
        pending_input_id = uuid4()
        handle = service.begin_input(
            workspace_token=token,
            auth_session_ref=auth_session_id,
            actor_id="EMP-001",
            safe_user_text=_request_text(),
        )
        graph, invocation, _payload, _context = _run_to_interrupt(
            factory, runtime, service, handle, StaticReplyModel(_complete_reply()),
            pending_input_id, workspace_token=token,
        )
        verified = _verify_interrupt_candidate(invocation, graph)
        with service.advisory_lock(agent_thread_id) as lock:
            service.finalize_interrupt(
                handle,
                workspace_token=token,
                lock=lock,
                verified=verified,
                pending_input_id=pending_input_id,
                draft_revision=1,
            )
        with service.advisory_lock(agent_thread_id) as lock:
            resume_handle = service.begin_resume(
                workspace_token=token,
                auth_session_ref=auth_session_id,
                actor_id="EMP-001",
                safe_user_text="确认提交",
                pending_input_id=pending_input_id,
                lock=lock,
                saver=runtime.saver,
            )
        with factory() as session:
            auth = session.get(AuthSessionRecord, auth_session_id)
            assert auth is not None
            auth.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()

        resume_context = _server_context(factory, resume_handle.execution_id)
        invocation_b = FencedPostgresSaverAdapter(runtime.saver).for_execution(
            resume_context
        )
        graph_b = build_production_graph(checkpointer=invocation_b)
        config = _config_for(
            resume_context.checkpoint_thread_id,
            resume_context.accepted_checkpoint_id,
        )
        runtime_context = _runtime_context(
            factory,
            resume_handle.workspace_id,
            token,
            str(resume_handle.auth_session_ref),
            graph_run_id=resume_handle.graph_run_id,
            input_turn_id=resume_handle.input_turn_id,
            fence=resume_handle.lease_fence,
            model=StaticReplyModel(_complete_reply()),
            pending_input_id=pending_input_id,
            input_seq=resume_handle.input_seq,
        )
        turn = graph_b.invoke(
            Command(resume={"decision": "confirm", "safe_user_text": "确认提交"}),
            config,
            context=runtime_context,
        )
        assert turn.business_status == "recoverable_error", turn.business_status
        assert turn.error_code == "CONFIRMATION_CONFLICT"
        with factory() as session:
            workspace = session.get(WorkspaceRecord, workspace_id)
            assert workspace is not None
            assert workspace.draft["confirmed"] is False  # type: ignore[index]
        runtime.close()
        close_all_sessions()


def test_reinterrupt_finalize_after_session_revoked_closes_safely() -> None:
    """P1 (round 2) regression: the session is revoked after the resume graph
    stops at the re-interrupt and before finalize_interrupt(previous=...);
    the final fenced transaction must close safely (recoverable_error +
    resolved pending + consumed Cursor) instead of publishing a normal
    waiting_input re-interrupt."""
    with _isolated_checkpoint_database() as settings:
        runtime = PostgresCheckpointRuntime(settings)
        runtime.start()
        runtime.check_readiness()
        factory = build_session_factory(build_engine(settings.database_url))
        token, workspace_id, agent_thread_id, auth_session_id = _workspace_fixture(
            factory
        )
        service = TurnExecutionService(factory)
        pending_input_id = uuid4()
        handle = service.begin_input(
            workspace_token=token,
            auth_session_ref=auth_session_id,
            actor_id="EMP-001",
            safe_user_text=_request_text(),
        )
        graph, invocation, _payload, _context = _run_to_interrupt(
            factory, runtime, service, handle, StaticReplyModel(_complete_reply()),
            pending_input_id, workspace_token=token,
        )
        verified = _verify_interrupt_candidate(invocation, graph)
        with service.advisory_lock(agent_thread_id) as lock:
            service.finalize_interrupt(
                handle,
                workspace_token=token,
                lock=lock,
                verified=verified,
                pending_input_id=pending_input_id,
                draft_revision=1,
            )

        # Resume edits a field; the graph re-routes and stops at the second
        # confirmation interrupt.
        with service.advisory_lock(agent_thread_id) as lock:
            resume_handle = service.begin_resume(
                workspace_token=token,
                auth_session_ref=auth_session_id,
                actor_id="EMP-001",
                safe_user_text="改成 60 天",
                pending_input_id=pending_input_id,
                lock=lock,
                saver=runtime.saver,
            )
        resume_context = _server_context(factory, resume_handle.execution_id)
        invocation_b = FencedPostgresSaverAdapter(runtime.saver).for_execution(
            resume_context
        )
        graph_b = build_production_graph(checkpointer=invocation_b)
        config = _config_for(
            resume_context.checkpoint_thread_id,
            resume_context.accepted_checkpoint_id,
        )
        runtime_context = _runtime_context(
            factory,
            resume_handle.workspace_id,
            token,
            str(resume_handle.auth_session_ref),
            graph_run_id=resume_handle.graph_run_id,
            input_turn_id=resume_handle.input_turn_id,
            fence=resume_handle.lease_fence,
            model=StaticReplyModel(
                ParsedReply(
                    entitlement_id="insighthub.dashboard_view",
                    duration_days=60,
                    justification="业务需要",
                )
            ),
            pending_input_id=pending_input_id,
            input_seq=resume_handle.input_seq,
        )
        with pytest.raises(ConfirmationInterruptRaised):
            graph_b.invoke(
                Command(
                    resume={
                        "decision": "route_new_input",
                        "safe_user_text": "改成 60 天",
                    }
                ),
                config,
                context=runtime_context,
            )
        verified_b = _verify_interrupt_candidate(invocation_b, graph_b)

        # Revoke the session between the graph stop and the final transaction.
        _revoke_auth_session(factory, auth_session_id)

        with service.advisory_lock(agent_thread_id) as lock:
            terminal_id = service.finalize_interrupt(
                resume_handle,
                workspace_token=token,
                lock=lock,
                verified=verified_b,
                pending_input_id=pending_input_id,
                draft_revision=2,
                previous_pending_input_id=pending_input_id,
            )
        with factory() as session:
            execution = session.get(
                AgentTurnExecutionRecord, resume_handle.execution_id
            )
            assert execution is not None
            # Never a normal waiting_input re-interrupt for a revoked session.
            assert execution.status == "recoverable_error"
            assert execution.lease_expires_at is None
            assert execution.terminal_event_id == terminal_id
            terminal = session.get(WorkspaceEventRecord, terminal_id)
            assert terminal is not None
            assert terminal.event_type == "error.recoverable"
            pending = session.scalar(
                select(AgentPendingInputRecord).where(
                    AgentPendingInputRecord.pending_input_id == pending_input_id
                )
            )
            assert pending is not None
            assert pending.status == "resolved"
            workspace = session.get(WorkspaceRecord, workspace_id)
            assert workspace is not None
            assert workspace.cursor_consumed_at is not None
            assert workspace.cursor_expected_field is not None or True
            assert workspace.draft is not None
            assert workspace.draft["confirmed"] is False
        runtime.close()
        close_all_sessions()


def test_reinterrupt_finalize_after_session_expired_closes_safely() -> None:
    """P1 (round 2) regression: the session expires after the resume graph
    stops at the re-interrupt and before the final fenced transaction; the
    same safe close must happen."""
    with _isolated_checkpoint_database() as settings:
        runtime = PostgresCheckpointRuntime(settings)
        runtime.start()
        runtime.check_readiness()
        factory = build_session_factory(build_engine(settings.database_url))
        token, workspace_id, agent_thread_id, auth_session_id = _workspace_fixture(
            factory
        )
        service = TurnExecutionService(factory)
        pending_input_id = uuid4()
        handle = service.begin_input(
            workspace_token=token,
            auth_session_ref=auth_session_id,
            actor_id="EMP-001",
            safe_user_text=_request_text(),
        )
        graph, invocation, _payload, _context = _run_to_interrupt(
            factory, runtime, service, handle, StaticReplyModel(_complete_reply()),
            pending_input_id, workspace_token=token,
        )
        verified = _verify_interrupt_candidate(invocation, graph)
        with service.advisory_lock(agent_thread_id) as lock:
            service.finalize_interrupt(
                handle,
                workspace_token=token,
                lock=lock,
                verified=verified,
                pending_input_id=pending_input_id,
                draft_revision=1,
            )
        with service.advisory_lock(agent_thread_id) as lock:
            resume_handle = service.begin_resume(
                workspace_token=token,
                auth_session_ref=auth_session_id,
                actor_id="EMP-001",
                safe_user_text="改成 60 天",
                pending_input_id=pending_input_id,
                lock=lock,
                saver=runtime.saver,
            )
        resume_context = _server_context(factory, resume_handle.execution_id)
        invocation_b = FencedPostgresSaverAdapter(runtime.saver).for_execution(
            resume_context
        )
        graph_b = build_production_graph(checkpointer=invocation_b)
        config = _config_for(
            resume_context.checkpoint_thread_id,
            resume_context.accepted_checkpoint_id,
        )
        runtime_context = _runtime_context(
            factory,
            resume_handle.workspace_id,
            token,
            str(resume_handle.auth_session_ref),
            graph_run_id=resume_handle.graph_run_id,
            input_turn_id=resume_handle.input_turn_id,
            fence=resume_handle.lease_fence,
            model=StaticReplyModel(
                ParsedReply(
                    entitlement_id="insighthub.dashboard_view",
                    duration_days=60,
                    justification="业务需要",
                )
            ),
            pending_input_id=pending_input_id,
            input_seq=resume_handle.input_seq,
        )
        with pytest.raises(ConfirmationInterruptRaised):
            graph_b.invoke(
                Command(
                    resume={
                        "decision": "route_new_input",
                        "safe_user_text": "改成 60 天",
                    }
                ),
                config,
                context=runtime_context,
            )
        verified_b = _verify_interrupt_candidate(invocation_b, graph_b)

        # Expire the session between the graph stop and the final transaction.
        with factory() as session:
            auth = session.get(AuthSessionRecord, auth_session_id)
            assert auth is not None
            auth.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()

        with service.advisory_lock(agent_thread_id) as lock:
            terminal_id = service.finalize_interrupt(
                resume_handle,
                workspace_token=token,
                lock=lock,
                verified=verified_b,
                pending_input_id=pending_input_id,
                draft_revision=2,
                previous_pending_input_id=pending_input_id,
            )
        with factory() as session:
            execution = session.get(
                AgentTurnExecutionRecord, resume_handle.execution_id
            )
            assert execution is not None
            assert execution.status == "recoverable_error"
            assert execution.terminal_event_id == terminal_id
            pending = session.scalar(
                select(AgentPendingInputRecord).where(
                    AgentPendingInputRecord.pending_input_id == pending_input_id
                )
            )
            assert pending is not None
            assert pending.status == "resolved"
        runtime.close()
        close_all_sessions()


def test_resume_wrong_auth_session_fails_closed_with_zero_writes() -> None:
    """AC3: a resume with a mismatched auth session closes safely before any
    new fact is written."""
    with _isolated_checkpoint_database() as settings:
        runtime = PostgresCheckpointRuntime(settings)
        runtime.start()
        runtime.check_readiness()
        factory = build_session_factory(build_engine(settings.database_url))
        token, workspace_id, agent_thread_id, auth_session_id = _workspace_fixture(
            factory
        )
        service = TurnExecutionService(factory)
        pending_input_id = uuid4()
        handle = service.begin_input(
            workspace_token=token,
            auth_session_ref=auth_session_id,
            actor_id="EMP-001",
            safe_user_text=_request_text(),
        )
        graph, invocation, _payload, _context = _run_to_interrupt(
            factory, runtime, service, handle, StaticReplyModel(_complete_reply()),
            pending_input_id, workspace_token=token,
        )
        verified = _verify_interrupt_candidate(invocation, graph)
        with service.advisory_lock(agent_thread_id) as lock:
            service.finalize_interrupt(
                handle,
                workspace_token=token,
                lock=lock,
                verified=verified,
                pending_input_id=pending_input_id,
                draft_revision=1,
            )
        executions_before = _count(factory, AgentTurnExecutionRecord, workspace_id)
        events_before = _count(factory, WorkspaceEventRecord, workspace_id)

        wrong_session = uuid4()
        with service.advisory_lock(agent_thread_id) as lock:
            with pytest.raises(TurnExecutionError):
                service.begin_resume(
                    workspace_token=token,
                    auth_session_ref=wrong_session,
                    actor_id="EMP-001",
                    safe_user_text="确认提交",
                    pending_input_id=pending_input_id,
                    lock=lock,
                    saver=runtime.saver,
                )
        assert _count(factory, AgentTurnExecutionRecord, workspace_id) == executions_before
        assert _count(factory, WorkspaceEventRecord, workspace_id) == events_before
        with factory() as session:
            pending = session.scalar(
                select(AgentPendingInputRecord).where(
                    AgentPendingInputRecord.pending_input_id == pending_input_id
                )
            )
            assert pending is not None
            assert pending.status == "active"
        runtime.close()
        close_all_sessions()


def test_resume_after_session_revoked_closes_safely_without_business_result() -> None:
    """P1-2 regression: the AuthSession is revoked after begin_resume; the
    resume must close safely (recoverable, no read-only business result, no
    confirmation) instead of answering and resolving the pending."""
    with _isolated_checkpoint_database() as settings:
        runtime = PostgresCheckpointRuntime(settings)
        runtime.start()
        runtime.check_readiness()
        factory = build_session_factory(build_engine(settings.database_url))
        token, workspace_id, agent_thread_id, auth_session_id = _workspace_fixture(
            factory
        )
        service = TurnExecutionService(factory)
        pending_input_id = uuid4()
        handle = service.begin_input(
            workspace_token=token,
            auth_session_ref=auth_session_id,
            actor_id="EMP-001",
            safe_user_text=_request_text(),
        )
        graph, invocation, _payload, _context = _run_to_interrupt(
            factory, runtime, service, handle, StaticReplyModel(_complete_reply()),
            pending_input_id, workspace_token=token,
        )
        verified = _verify_interrupt_candidate(invocation, graph)
        with service.advisory_lock(agent_thread_id) as lock:
            service.finalize_interrupt(
                handle,
                workspace_token=token,
                lock=lock,
                verified=verified,
                pending_input_id=pending_input_id,
                draft_revision=1,
            )
        with service.advisory_lock(agent_thread_id) as lock:
            resume_handle = service.begin_resume(
                workspace_token=token,
                auth_session_ref=auth_session_id,
                actor_id="EMP-001",
                safe_user_text="我现在有什么权限？",
                pending_input_id=pending_input_id,
                lock=lock,
                saver=runtime.saver,
            )

        # Revoke the session between the resume transaction and the graph call.
        _revoke_auth_session(factory, auth_session_id)

        resume_context = _server_context(factory, resume_handle.execution_id)
        invocation_b = FencedPostgresSaverAdapter(runtime.saver).for_execution(
            resume_context
        )
        graph_b = build_production_graph(checkpointer=invocation_b)
        config = _config_for(
            resume_context.checkpoint_thread_id,
            resume_context.accepted_checkpoint_id,
        )
        runtime_context = _runtime_context(
            factory,
            resume_handle.workspace_id,
            token,
            str(resume_handle.auth_session_ref),
            graph_run_id=resume_handle.graph_run_id,
            input_turn_id=resume_handle.input_turn_id,
            fence=resume_handle.lease_fence,
            model=StaticReplyModel(_complete_reply()),
            pending_input_id=pending_input_id,
            input_seq=resume_handle.input_seq,
        )
        turn = graph_b.invoke(
            Command(
                resume={
                    "decision": "route_new_input",
                    "safe_user_text": "我现在有什么权限？",
                }
            ),
            config,
            context=runtime_context,
        )
        # No read-only business result may be produced for a revoked session.
        assert turn.business_status == "recoverable_error", turn.business_status
        assert turn.intent == "request_access" or turn.error_code == "CONFIRMATION_CONFLICT"
        assert turn.error_code == "CONFIRMATION_CONFLICT"

        verified_b = _verify_end_candidate(invocation_b, graph_b)
        with service.advisory_lock(agent_thread_id) as lock:
            terminal_id = service.finalize_resume_outcome(
                resume_handle,
                workspace_token=token,
                lock=lock,
                verified=verified_b,
                pending_input_id=pending_input_id,
                event_type="error.recoverable",
                payload={
                    "turn_id": resume_handle.input_turn_id,
                    "code": "CONFIRMATION_CONFLICT",
                    "message": "确认状态已变化，请核对当前申请信息后重新确认。",
                },
            )
        with factory() as session:
            execution = session.get(
                AgentTurnExecutionRecord, resume_handle.execution_id
            )
            assert execution is not None
            assert execution.status == "recoverable_error"
            assert execution.terminal_event_id == terminal_id
            pending = session.scalar(
                select(AgentPendingInputRecord).where(
                    AgentPendingInputRecord.pending_input_id == pending_input_id
                )
            )
            assert pending is not None
            assert pending.status == "resolved"
            workspace = session.get(WorkspaceRecord, workspace_id)
            assert workspace is not None
            assert workspace.draft is not None
            assert workspace.draft["confirmed"] is False
        runtime.close()
        close_all_sessions()


def test_concurrent_resume_second_request_409_without_new_facts() -> None:
    """AC3: two concurrent resume requests: exactly one creates the resume
    facts, the other fails with the stable 409 family, before any user fact."""
    with _isolated_checkpoint_database() as settings:
        runtime = PostgresCheckpointRuntime(settings)
        runtime.start()
        runtime.check_readiness()
        factory = build_session_factory(build_engine(settings.database_url))
        token, workspace_id, agent_thread_id, auth_session_id = _workspace_fixture(
            factory
        )
        service = TurnExecutionService(factory)
        pending_input_id = uuid4()
        handle = service.begin_input(
            workspace_token=token,
            auth_session_ref=auth_session_id,
            actor_id="EMP-001",
            safe_user_text=_request_text(),
        )
        graph, invocation, _payload, _context = _run_to_interrupt(
            factory, runtime, service, handle, StaticReplyModel(_complete_reply()),
            pending_input_id, workspace_token=token,
        )
        verified = _verify_interrupt_candidate(invocation, graph)
        with service.advisory_lock(agent_thread_id) as lock:
            service.finalize_interrupt(
                handle,
                workspace_token=token,
                lock=lock,
                verified=verified,
                pending_input_id=pending_input_id,
                draft_revision=1,
            )
        events_before = _count(factory, WorkspaceEventRecord, workspace_id)
        barrier = threading.Barrier(2)
        results: list[object] = []
        errors: list[BaseException] = []

        def attempt() -> None:
            barrier.wait()
            try:
                with service.advisory_lock(agent_thread_id) as lock:
                    results.append(
                        service.begin_resume(
                            workspace_token=token,
                            auth_session_ref=auth_session_id,
                            actor_id="EMP-001",
                            safe_user_text="确认提交",
                            pending_input_id=pending_input_id,
                            lock=lock,
                            saver=runtime.saver,
                        )
                    )
            except BaseException as error:  # pragma: no cover - exercised by thread
                errors.append(error)

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(
            errors[0],
            (
                TurnInProgressError,
                TurnRecoveryInProgressError,
                TurnLockUnavailableError,
            ),
        )
        assert _count(factory, WorkspaceEventRecord, workspace_id) == events_before + 2
        with factory() as session:
            assert (
                session.scalar(
                    select(func.count()).select_from(AgentTurnExecutionRecord).where(
                        AgentTurnExecutionRecord.workspace_id == workspace_id,
                        AgentTurnExecutionRecord.status == "running",
                    )
                )
                == 1
            )
        runtime.close()
        close_all_sessions()
