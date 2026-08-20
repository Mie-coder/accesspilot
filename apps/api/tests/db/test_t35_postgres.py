"""T35 real-PostgreSQL rollback bridge and publish preflight proofs.

Every rollback claim is proven against a real PostgreSQL checkpointer and real
application transactions on disposable databases: the interrupt is produced by
the production graph, the accepted head/pending/Cursor/terminal are promoted by
the fenced ``finalize_interrupt`` transaction, and only then the T35 bridge
maps the workspace to Legacy.  Also proves the global publish preflight gate on
the isolated deployment (no other workspaces exist there).
"""

from __future__ import annotations

import os
import re
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
from accesspilot.agent.rollback import (
    CheckpointTaskReader,
    LegacyRollbackBridge,
    PublishPreflightGate,
    RollbackBlockedError,
    WorkspaceRollbackReport,
)
from accesspilot.agent.routing import DeterministicIntentRouter
from accesspilot.agent.turn_execution import (
    TurnExecutionError,
    TurnExecutionHandle,
    TurnExecutionService,
)
from accesspilot.auth import Principal
from accesspilot.checkpoint_init import run_official_checkpoint_setup
from accesspilot.config import Settings
from accesspilot.db.models import (
    AgentPendingInputRecord,
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

_ADMIN_URL_ENV = "ACCESSPILOT_T35_ADMIN_DATABASE_URL"
_FALLBACK_URL_ENVS = (
    "ACCESSPILOT_T33_ADMIN_DATABASE_URL",
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
def _isolated_checkpoint_database(
    *,
    resource_prefix: str = "t35",
    _fail_at: str | None = None,
) -> Iterator[Settings]:
    configured = _admin_url()
    if not configured:
        pytest.skip(
            f"set {_ADMIN_URL_ENV} to run the destructive-isolated T35 PostgreSQL proof"
        )
    admin_url = make_url(configured).set(
        drivername="postgresql", database="postgres"
    )
    admin_conninfo = admin_url.render_as_string(hide_password=False)
    if re.fullmatch(r"[a-z][a-z0-9]{1,7}", resource_prefix) is None:
        raise ValueError("resource_prefix must be 2-8 lowercase alphanumeric characters")
    suffix = uuid4().hex[:12]
    database = f"{resource_prefix}chk_{suffix}"
    migration_role = f"{resource_prefix}m_{suffix}"
    runtime_role = f"{resource_prefix}r_{suffix}"
    migration_password = f"m-{uuid4().hex}"
    runtime_password = f"r-{uuid4().hex}"
    schema = f"{resource_prefix}_checkpoint_{suffix}"
    exact_identifier = re.compile(
        rf"{re.escape(resource_prefix)}(?:chk_|m_|r_|_checkpoint_)[0-9a-f]{{12}}"
    )
    for identifier in (database, migration_role, runtime_role, schema):
        if exact_identifier.fullmatch(identifier) is None:
            raise AssertionError("generated disposable PostgreSQL identifier is invalid")

    migration_role_created = False
    runtime_role_created = False
    database_created = False

    def fail_after(phase: str) -> None:
        if _fail_at == phase:
            raise RuntimeError(f"injected isolated database failure after {phase}")

    try:
        with psycopg.connect(admin_conninfo, autocommit=True) as admin:
            admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(migration_role), sql.Literal(migration_password)
                )
            )
            migration_role_created = True
            fail_after("migration_role")
            admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(runtime_role), sql.Literal(runtime_password)
                )
            )
            runtime_role_created = True
            fail_after("runtime_role")
            admin.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(database), sql.Identifier(migration_role)
                )
            )
            database_created = True
            fail_after("database")

        database_admin_url = make_url(configured).set(
            drivername="postgresql", database=database
        ).render_as_string(hide_password=False)
        with psycopg.connect(database_admin_url, autocommit=True) as database_admin:
            database_admin.execute("CREATE EXTENSION vector")
        fail_after("extension")

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
        settings = Settings(
            database_url=migration_url,
            checkpoint_migration_database_url=migration_url,
            checkpoint_database_url=runtime_url,
            checkpoint_schema=schema,
            orchestrator_mode="mixed",
            langgraph_canary_percent=0,
            _env_file=None,
        )
        fail_after("settings")

        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
        alembic_config = Config(os.path.join(root, "alembic.ini"))
        alembic_config.attributes["database_url"] = migration_url
        command.upgrade(alembic_config, "head")
        fail_after("alembic")
        run_official_checkpoint_setup(settings)
        fail_after("checkpoint")
        yield settings
    finally:
        if database_created or runtime_role_created or migration_role_created:
            with psycopg.connect(admin_conninfo, autocommit=True) as admin:
                if database_created:
                    admin.execute(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = %s AND pid <> pg_backend_pid()",
                        (database,),
                    )
                    admin.execute(
                        sql.SQL("DROP DATABASE {}").format(sql.Identifier(database))
                    )
                if runtime_role_created:
                    admin.execute(
                        sql.SQL("DROP ROLE {}").format(sql.Identifier(runtime_role))
                    )
                if migration_role_created:
                    admin.execute(
                        sql.SQL("DROP ROLE {}").format(sql.Identifier(migration_role))
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
) -> tuple[str, UUID, UUID, UUID]:
    token = f"t35-pg-{uuid4()}"
    auth_session_id = uuid4()
    with factory() as session:
        seed_catalog(session)
        workspace = WorkspaceRecord(
            token_hash=hash_workspace_token(token),
            actor_id="EMP-001",
            flow_version=2,
            lease_fence=0,
            draft={
                "employee_id": "EMP-001",
                "entitlement_id": "insighthub.dashboard_view",
                "duration_days": 14,
                "justification": "业务需要",
                "confirmed": False,
            },
            draft_revision=1,
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


def _config_for(
    thread_id: str, checkpoint_id: str | None = None
) -> dict[str, object]:
    config: dict[str, object] = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
        }
    }
    if checkpoint_id is not None:
        configurable = config["configurable"]
        assert isinstance(configurable, dict)
        configurable["checkpoint_id"] = checkpoint_id
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


def _verify_interrupt_candidate(invocation: Any, graph: Any) -> Any:
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


def _run_to_interrupt(
    factory: sessionmaker[Session],
    runtime: PostgresCheckpointRuntime,
    service: TurnExecutionService,
    handle: TurnExecutionHandle,
    model: object,
    pending_input_id: UUID,
    *,
    workspace_token: str,
) -> tuple[Any, Any, Any]:
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
    return graph, invocation, raised.value.payload


def _finalize(
    factory: sessionmaker[Session],
    runtime: PostgresCheckpointRuntime,
    service: TurnExecutionService,
    handle: TurnExecutionHandle,
    pending_input_id: UUID,
    *,
    workspace_token: str,
    agent_thread_id: UUID,
) -> UUID:
    graph, invocation, payload = _run_to_interrupt(
        factory,
        runtime,
        service,
        handle,
        StaticReplyModel(_complete_reply()),
        pending_input_id,
        workspace_token=workspace_token,
    )
    verified = _verify_interrupt_candidate(invocation, graph)
    with service.advisory_lock(agent_thread_id) as lock:
        return service.finalize_interrupt(
            handle,
            workspace_token=workspace_token,
            lock=lock,
            verified=verified,
            pending_input_id=pending_input_id,
            draft_revision=payload["draft_revision"],
        )


def _bridge(
    factory: sessionmaker[Session],
    runtime: PostgresCheckpointRuntime,
) -> LegacyRollbackBridge:
    assert runtime.saver is not None
    return LegacyRollbackBridge(
        factory, CheckpointTaskReader(runtime.saver)
    )


def test_real_pg_rollback_reconciled_confirmation_to_legacy_and_preflight() -> None:
    """The full drain path on real PostgreSQL: interrupt -> finalize ->
    reconcile -> atomically map to flow 1 with a kept Cursor and an immutable
    tombstone, while the accepted head stays readable and the global publish
    preflight passes afterwards."""
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
        terminal_id = _finalize(
            factory,
            runtime,
            service,
            handle,
            pending_input_id,
            workspace_token=token,
            agent_thread_id=agent_thread_id,
        )

        with factory() as session:
            execution = session.get(AgentTurnExecutionRecord, handle.execution_id)
            assert execution is not None
            head_id = execution.accepted_checkpoint_id
            assert head_id is not None
            workspace = session.get(WorkspaceRecord, workspace_id)
            assert workspace is not None
            assert workspace.flow_version == 2
            assert workspace.cursor_expected_field == "confirmation"

        # Before rollback the global preflight must block: one live pending.
        gate = PublishPreflightGate(factory, CheckpointTaskReader(runtime.saver))
        before = gate.check()
        assert not before.passed
        assert before.live_pending_count == 1

        report = _bridge(factory, runtime).execute(workspace_token=token)
        assert not report.blocked
        assert report.pending_outcomes[0].mapping == "abandoned_to_legacy"

        with factory() as session:
            workspace = session.get(WorkspaceRecord, workspace_id)
            assert workspace is not None
            assert workspace.flow_version == 1
            assert workspace.cursor_expected_field == "confirmation"
            assert workspace.cursor_consumed_at is None
            pending = session.scalar(
                select(AgentPendingInputRecord).where(
                    AgentPendingInputRecord.pending_input_id == pending_input_id
                )
            )
            assert pending is not None
            assert pending.status == "abandoned_to_legacy"
            assert pending.retired_at is not None
            assert pending.retirement_reason
            execution = session.get(AgentTurnExecutionRecord, handle.execution_id)
            assert execution is not None
            assert execution.status == "waiting_input"
            assert execution.terminal_event_id == terminal_id

        # The old accepted head is retained and still exact-readable.
        assert runtime.saver is not None
        assert (
            runtime.saver.get_tuple(
                {
                    "configurable": {
                        "thread_id": execution.checkpoint_thread_id,
                        "checkpoint_ns": "",
                        "checkpoint_id": head_id,
                    }
                }
            )
            is not None
        )

        # After rollback the global preflight passes on the isolated deployment.
        after = gate.check()
        assert after.passed
        assert after.live_pending_count == 0
        assert after.unfinished_execution_count == 0
        assert after.live_accepted_task_count == 0
        assert after.retained_task_count == 1


def test_real_pg_rollback_revoked_principal_marks_abandoned_conflict() -> None:
    """A reconciled head whose Principal no longer matches maps to a stable,
    recoverable conflict: flow 1, abandoned_conflict tombstone, Cursor cleared."""
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
        _finalize(
            factory,
            runtime,
            service,
            handle,
            pending_input_id,
            workspace_token=token,
            agent_thread_id=agent_thread_id,
        )
        with factory() as session:
            auth = session.get(AuthSessionRecord, auth_session_id)
            assert auth is not None
            auth.revoked_at = datetime.now(UTC)
            session.commit()

        report = _bridge(factory, runtime).execute(workspace_token=token)

        assert not report.blocked
        assert report.conflict is True
        assert report.pending_outcomes[0].mapping == "abandoned_conflict"
        with factory() as session:
            workspace = session.get(WorkspaceRecord, workspace_id)
            assert workspace is not None
            assert workspace.flow_version == 1
            assert workspace.cursor_expected_field is None
            pending = session.scalar(
                select(AgentPendingInputRecord).where(
                    AgentPendingInputRecord.pending_input_id == pending_input_id
                )
            )
            assert pending is not None
            assert pending.status == "abandoned_conflict"
            assert pending.retired_at is not None


def _resume_accept(
    factory: sessionmaker[Session],
    runtime: PostgresCheckpointRuntime,
    service: TurnExecutionService,
    *,
    workspace_token: str,
    agent_thread_id: UUID,
    auth_session_id: UUID,
    pending_input_id: UUID,
    safe_user_text: str = "确认提交",
) -> TurnExecutionHandle:
    """App B accepts one resume input (commits pending=resuming + a new
    running execution + message.user), exactly like the T34 resume path."""
    assert runtime.saver is not None
    with service.advisory_lock(agent_thread_id) as lock:
        return service.begin_resume(
            workspace_token=workspace_token,
            auth_session_ref=auth_session_id,
            actor_id="EMP-001",
            safe_user_text=safe_user_text,
            pending_input_id=pending_input_id,
            lock=lock,
            saver=runtime.saver,
        )


def test_real_pg_rollback_blocks_after_resume_committed() -> None:
    """R01 resume-first order: the resume transaction commits pending=resuming
    plus a running execution; the rollback re-reads those facts under the same
    advisory lock and blocks with zero writes."""
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
        _finalize(
            factory,
            runtime,
            service,
            handle,
            pending_input_id,
            workspace_token=token,
            agent_thread_id=agent_thread_id,
        )
        _resume_accept(
            factory,
            runtime,
            service,
            workspace_token=token,
            agent_thread_id=agent_thread_id,
            auth_session_id=auth_session_id,
            pending_input_id=pending_input_id,
        )

        with pytest.raises(RollbackBlockedError, match="unfinished"):
            _bridge(factory, runtime).execute(workspace_token=token)

        # Zero writes: the accepted resume stays resuming with its running
        # execution on flow 2; no tombstone, no flow flip, no partial state.
        with factory() as session:
            workspace = session.get(WorkspaceRecord, workspace_id)
            assert workspace is not None
            assert workspace.flow_version == 2
            assert workspace.cursor_expected_field == "confirmation"
            pending = session.scalar(
                select(AgentPendingInputRecord).where(
                    AgentPendingInputRecord.pending_input_id == pending_input_id
                )
            )
            assert pending is not None
            assert pending.status == "resuming"
            assert pending.retired_at is None
            running = session.scalar(
                select(AgentTurnExecutionRecord).where(
                    AgentTurnExecutionRecord.workspace_id == workspace_id,
                    AgentTurnExecutionRecord.status == "running",
                )
            )
            assert running is not None


def test_real_pg_rollback_committed_first_fails_later_resume() -> None:
    """R01 rollback-first order: after the atomic flow-1 + tombstone mapping
    commits, a later resume fails on the tombstone with zero new facts (the
    accepted input is never lost because it was never accepted)."""
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
        _finalize(
            factory,
            runtime,
            service,
            handle,
            pending_input_id,
            workspace_token=token,
            agent_thread_id=agent_thread_id,
        )
        with factory() as session:
            event_count = session.scalar(
                select(func.count()).select_from(WorkspaceEventRecord).where(
                    WorkspaceEventRecord.workspace_id == workspace_id
                )
            )
            execution_count = session.scalar(
                select(func.count()).select_from(AgentTurnExecutionRecord).where(
                    AgentTurnExecutionRecord.workspace_id == workspace_id
                )
            )
        assert event_count is not None and execution_count is not None

        report = _bridge(factory, runtime).execute(workspace_token=token)
        assert not report.blocked
        assert report.flow_version_after == 1

        with pytest.raises(TurnExecutionError, match="not active"):
            _resume_accept(
                factory,
                runtime,
                service,
                workspace_token=token,
                agent_thread_id=agent_thread_id,
                auth_session_id=auth_session_id,
                pending_input_id=pending_input_id,
            )

        with factory() as session:
            workspace = session.get(WorkspaceRecord, workspace_id)
            assert workspace is not None
            assert workspace.flow_version == 1
            pending = session.scalar(
                select(AgentPendingInputRecord).where(
                    AgentPendingInputRecord.pending_input_id == pending_input_id
                )
            )
            assert pending is not None
            assert pending.status == "abandoned_to_legacy"
            # The failed resume wrote nothing: same event/execution counts.
            assert (
                session.scalar(
                    select(func.count()).select_from(WorkspaceEventRecord).where(
                        WorkspaceEventRecord.workspace_id == workspace_id
                    )
                )
                == event_count
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AgentTurnExecutionRecord)
                    .where(AgentTurnExecutionRecord.workspace_id == workspace_id)
                )
                == execution_count
            )
            assert (
                session.scalar(
                    select(AgentTurnExecutionRecord).where(
                        AgentTurnExecutionRecord.workspace_id == workspace_id,
                        AgentTurnExecutionRecord.status == "running",
                    )
                )
                is None
            )


@pytest.mark.parametrize("resume_first", [False, True])
def test_real_pg_rollback_resume_concurrent_race_never_leaks_running_flow1(
    resume_first: bool,
) -> None:
    """R01 concurrent order: rollback and resume both need the same
    agent-thread advisory lock, so exactly one side commits; the invariants
    (no lost accepted input, never a running execution on flow 1, no partial
    writes) hold for every interleaving and for both start orders."""
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
        _finalize(
            factory,
            runtime,
            service,
            handle,
            pending_input_id,
            workspace_token=token,
            agent_thread_id=agent_thread_id,
        )

        results: dict[str, Any] = {}

        def do_rollback() -> None:
            try:
                results["rollback"] = _bridge(factory, runtime).execute(
                    workspace_token=token
                )
            except BaseException as error:  # noqa: BLE001 - race outcome capture
                results["rollback"] = error

        def do_resume() -> None:
            try:
                results["resume"] = _resume_accept(
                    factory,
                    runtime,
                    service,
                    workspace_token=token,
                    agent_thread_id=agent_thread_id,
                    auth_session_id=auth_session_id,
                    pending_input_id=pending_input_id,
                )
            except BaseException as error:  # noqa: BLE001 - race outcome capture
                results["resume"] = error

        rollback_thread = threading.Thread(target=do_rollback)
        resume_thread = threading.Thread(target=do_resume)
        if resume_first:
            resume_thread.start()
            rollback_thread.start()
        else:
            rollback_thread.start()
            resume_thread.start()
        rollback_thread.join(timeout=30)
        resume_thread.join(timeout=30)

        rollback_won = isinstance(results.get("rollback"), WorkspaceRollbackReport)
        resume_won = isinstance(results.get("resume"), TurnExecutionHandle)
        assert rollback_won or resume_won
        assert not (rollback_won and resume_won)

        with factory() as session:
            workspace = session.get(WorkspaceRecord, workspace_id)
            assert workspace is not None
            pending = session.scalar(
                select(AgentPendingInputRecord).where(
                    AgentPendingInputRecord.pending_input_id == pending_input_id
                )
            )
            assert pending is not None
            running = session.scalar(
                select(AgentTurnExecutionRecord).where(
                    AgentTurnExecutionRecord.workspace_id == workspace_id,
                    AgentTurnExecutionRecord.status == "running",
                )
            )
            if resume_won:
                # The accepted input is intact and still on the LangGraph flow.
                assert workspace.flow_version == 2
                assert pending.status == "resuming"
                assert running is not None
                assert (
                    session.scalar(
                        select(func.count())
                        .select_from(WorkspaceEventRecord)
                        .where(
                            WorkspaceEventRecord.workspace_id == workspace_id,
                            WorkspaceEventRecord.event_type == "message.user",
                        )
                    )
                    >= 2  # original input + the accepted resume input
                )
            if rollback_won:
                assert workspace.flow_version == 1
                assert pending.status in ("abandoned_to_legacy", "abandoned_conflict")
                assert pending.retired_at is not None
            # The forbidden state never exists in any interleaving.
            assert not (running is not None and workspace.flow_version == 1)


def test_real_pg_preflight_passes_after_successful_resume() -> None:
    """R02 real T34 shape: after a genuine interrupt -> resume -> confirm chain
    on the real checkpointer, the resolved pending's retained interrupt task is
    provably superseded by the accepted END head, so the global publish
    preflight passes with the supersession fact reported."""
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
        graph, invocation, _payload = _run_to_interrupt(
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

        # App B resumes with one Command(resume) through the real graph.
        resume_handle = _resume_accept(
            factory,
            runtime,
            service,
            workspace_token=token,
            agent_thread_id=agent_thread_id,
            auth_session_id=auth_session_id,
            pending_input_id=pending_input_id,
        )
        resume_context = _server_context(factory, resume_handle.execution_id)
        assert runtime.saver is not None
        invocation_b = FencedPostgresSaverAdapter(runtime.saver).for_execution(
            resume_context
        )
        graph_b = build_production_graph(checkpointer=invocation_b)
        config = _config_for(
            resume_context.checkpoint_thread_id,
            resume_context.accepted_checkpoint_id,
        )
        resume_runtime = _runtime_context(
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
            context=resume_runtime,
        )
        if turn.business_status != "ready_to_submit":
            print("DEBUG TURN:", turn.model_dump())
        assert turn.business_status == "ready_to_submit"
        candidate_b = invocation_b.candidate
        assert candidate_b is not None
        verified_b = invocation_b.verify_candidate(
            candidate_b,
            graph_stopped=True,
            graph_state_reader=graph_b.compiled.get_state,
            state_validator=lambda state: not state.next,
        )
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
                    "content": "申请信息已明确确认，可以提交正式申请。",
                    "intent": "request_access",
                    "business_status": "ready_to_submit",
                    "draft_revision": 1,
                },
            )

        with factory() as session:
            pending = session.scalar(
                select(AgentPendingInputRecord).where(
                    AgentPendingInputRecord.pending_input_id == pending_input_id
                )
            )
            assert pending is not None
            assert pending.status == "resolved"

        report = PublishPreflightGate(
            factory, CheckpointTaskReader(runtime.saver)
        ).check()
        assert report.passed
        assert report.live_pending_count == 0
        assert report.live_accepted_task_count == 0
        assert report.superseded_task_count == 1


def test_real_pg_takeover_historical_branch_never_resumes_tombstoned_head() -> None:
    """R04 on the real checkpointer: with a historical waiting_input execution
    holding the tombstoned interrupt head and an expired running execution
    with no head of its own, takeover must fall back to the safe input event
    instead of rehydrating the retired head."""
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
        _finalize(
            factory,
            runtime,
            service,
            handle,
            pending_input_id,
            workspace_token=token,
            agent_thread_id=agent_thread_id,
        )
        with factory() as session:
            pending = session.scalar(
                select(AgentPendingInputRecord).where(
                    AgentPendingInputRecord.pending_input_id == pending_input_id
                )
            )
            assert pending is not None
            head_id = pending.accepted_checkpoint_id
            pending.status = "abandoned_to_legacy"
            pending.retired_at = datetime.now(UTC)
            pending.retirement_reason = "test tombstone"
            # The historical execution keeps its accepted head (real rollback
            # shape); the crashed next input holds an expired lease and no head.
            historical = session.scalar(
                select(AgentTurnExecutionRecord).where(
                    AgentTurnExecutionRecord.workspace_id == workspace_id,
                    AgentTurnExecutionRecord.input_seq == 0,
                )
            )
            assert historical is not None
            assert historical.accepted_checkpoint_id == head_id
            input_event = WorkspaceEventRecord(
                workspace_id=workspace_id,
                event_type="message.user",
                payload={"content": "x", "turn_id": f"turn-{uuid4()}"},
            )
            session.add(input_event)
            session.flush()
            session.add(
                AgentTurnExecutionRecord(
                    workspace_id=workspace_id,
                    graph_run_id=handle.graph_run_id,
                    checkpoint_thread_id=handle.checkpoint_thread_id,
                    input_seq=1,
                    input_turn_id=f"turn-{uuid4()}",
                    input_event_id=input_event.id,
                    auth_session_ref=auth_session_id,
                    actor_id="EMP-001",
                    engine="langgraph",
                    attempt=1,
                    lease_fence=1,
                    lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
                    status="running",
                    checkpoint_ns="",
                    accepted_checkpoint_id=None,
                    terminal_event_id=None,
                )
            )
            session.commit()

        with service.advisory_lock(agent_thread_id) as lock:
            plan = service.takeover(
                workspace_token=token,
                graph_run_id=handle.graph_run_id,
                input_seq=1,
                auth_session_ref=auth_session_id,
                actor_id="EMP-001",
                lock=lock,
                saver=runtime.saver,
            )

        assert plan.source == "input_event"
        assert plan.accepted_checkpoint_id is None
