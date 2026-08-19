"""T36 real-PostgreSQL replay-safety and fenced finalize event identity.

证明确定性 event key 在真实 checkpointer 重放时不产生第二组事件，且 fenced
最终化事务中的 agent.input.required / terminal 事件携带 Spec §8.2 的
step_id 与 finalize event key；turn.started 携带 orchestrator/flow_version/
graph_version。
"""

from __future__ import annotations

import os
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
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.checkpoint import (
    FencedPostgresSaverAdapter,
    PostgresCheckpointRuntime,
    ServerExecutionContext,
)
from accesspilot.agent.embeddings import DeterministicEmbeddingModel
from accesspilot.agent.identity import event_key as identity_event_key
from accesspilot.agent.identity import step_id as identity_step_id
from accesspilot.agent.production_graph import (
    ConfirmationInterruptRaised,
    GraphInput,
    GraphRuntimeContext,
    build_production_graph,
)
from accesspilot.agent.routing import DeterministicIntentRouter
from accesspilot.agent.trace import (
    INPUT_STEP_KEY,
    LANGGRAPH_GRAPH_VERSION,
)
from accesspilot.agent.turn_execution import (
    TurnExecutionHandle,
    TurnExecutionService,
)
from accesspilot.auth import Principal
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
from accesspilot.db.workspace_store import SqlAlchemyWorkspaceStore, hash_workspace_token
from accesspilot.domain.models import ParsedReply
from accesspilot.tools.policies import PolicyService
from accesspilot.workspaces import WorkspaceService

_ADMIN_URL_ENV = "ACCESSPILOT_T36_ADMIN_DATABASE_URL"
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
def _isolated_checkpoint_database() -> Iterator[Settings]:
    configured = _admin_url()
    if not configured:
        pytest.skip(
            f"set {_ADMIN_URL_ENV} to run the destructive-isolated T36 PostgreSQL proof"
        )
    admin_url = make_url(configured).set(
        drivername="postgresql", database="postgres"
    )
    admin_conninfo = admin_url.render_as_string(hide_password=False)
    suffix = uuid4().hex[:12]
    database = f"t36chk_{suffix}"
    migration_role = f"t36m_{suffix}"
    runtime_role = f"t36r_{suffix}"
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
    schema = f"t36_checkpoint_{suffix}"
    settings = Settings(
        database_url=migration_url,
        checkpoint_migration_database_url=migration_url,
        checkpoint_database_url=runtime_url,
        checkpoint_schema=schema,
        orchestrator_mode="mixed",
        langgraph_canary_percent=0,
        langgraph_strict_msgpack=True,
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
) -> tuple[str, UUID, UUID, UUID]:
    token = f"t36-pg-{uuid4()}"
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


def _request_text() -> str:
    return "申请客户数据导出 30 天，用于业务需要"


def _complete_reply() -> ParsedReply:
    return ParsedReply(
        entitlement_id="insighthub.customer_export",
        duration_days=30,
        justification="业务需要",
    )


def _runtime_context(
    factory: sessionmaker[Session],
    handle: TurnExecutionHandle,
    workspace_token: str,
    *,
    model: object,
    pending_input_id: UUID,
    trace_enabled: bool = True,
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
        "current_turn_id": handle.input_turn_id,
        "current_fence": handle.lease_fence,
        "workspace_token": workspace_token,
        "auth_session_id": str(handle.auth_session_ref),
        "cookie": "runtime-cookie",
        "csrf_token": "runtime-csrf",
        "api_key": "runtime-key",
        "pending_input_id": str(pending_input_id),
        "current_input_seq": handle.input_seq,
        "trace_enabled": trace_enabled,
    }


def _graph_input(
    handle: TurnExecutionHandle,
    *,
    safe_user_text: str,
) -> GraphInput:
    return GraphInput(
        schema_version=1,
        flow_version=2,
        workspace_ref=handle.workspace_id,
        graph_run_id=handle.graph_run_id,
        input_seq=handle.input_seq,
        input_turn_id=handle.input_turn_id,
        safe_user_text=safe_user_text,
        input_kind="new_input",
    )


def _config_for(thread_id: str) -> dict[str, object]:
    return {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
        }
    }


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


def _events(
    factory: sessionmaker[Session],
    workspace_id: UUID,
) -> list[WorkspaceEventRecord]:
    with factory() as session:
        return list(
            session.scalars(
                select(WorkspaceEventRecord)
                .where(WorkspaceEventRecord.workspace_id == workspace_id)
                .order_by(WorkspaceEventRecord.id)
            ).all()
        )


def test_begin_input_writes_enriched_turn_started_facts() -> None:
    with _isolated_checkpoint_database() as settings:
        factory = build_session_factory(build_engine(settings.database_url))
        token, workspace_id, _agent_thread_id, auth_session_id = _workspace_fixture(
            factory
        )
        service = TurnExecutionService(factory)
        handle = service.begin_input(
            workspace_token=token,
            auth_session_ref=auth_session_id,
            actor_id="EMP-001",
            safe_user_text=_request_text(),
        )

        events = _events(factory, workspace_id)
        assert [event.event_type for event in events] == [
            "turn.started",
            "message.user",
        ]
        started = events[0].payload
        assert started["turn_id"] == handle.input_turn_id
        assert started["orchestrator"] == "langgraph"
        assert started["flow_version"] == 2
        assert started["graph_version"] == LANGGRAPH_GRAPH_VERSION
        # 事件 key 不进入 v1.2 turn.started 投影：仍由 fenced begin 事务保证唯一。
        assert events[0].event_key is None


def test_checkpoint_replay_emits_single_event_set_and_fenced_finalize_keys() -> None:
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

        context = _server_context(factory, handle.execution_id)
        invocation = FencedPostgresSaverAdapter(runtime.saver).for_execution(context)
        graph = build_production_graph(checkpointer=invocation)
        config = _config_for(context.checkpoint_thread_id)
        runtime_context = _runtime_context(
            factory,
            handle,
            token,
            model=StaticReplyModel(_complete_reply()),
            pending_input_id=pending_input_id,
        )
        with pytest.raises(ConfirmationInterruptRaised):
            graph.invoke(
                _graph_input(handle, safe_user_text=_request_text()),
                config,
                context=runtime_context,
            )
        first_set = _events(factory, workspace_id)
        keyed_events = [
            event
            for event in first_set
            if event.event_type not in {"message.user", "turn.started"}
        ]
        first_keys = {event.event_key for event in keyed_events}
        assert all(key is not None for key in first_keys)
        assert len(first_keys) == len(keyed_events)
        assert len(first_set) > 2  # 真实边界事件已写入
        model_completed = [
            event
            for event in first_set
            if event.event_type == "model.completed"
        ]
        assert len(model_completed) == 1

        # 崩溃重放：同一逻辑输入、同一执行坐标，用全新 saver 调用从 START
        # 重跑整张图；重复的节点/模型/草稿边界必须被确定性 event key 去重。
        replay_invocation = FencedPostgresSaverAdapter(runtime.saver).for_execution(
            context
        )
        replay_graph = build_production_graph(checkpointer=replay_invocation)
        with pytest.raises(ConfirmationInterruptRaised):
            replay_graph.invoke(
                _graph_input(handle, safe_user_text=_request_text()),
                config,
                context=runtime_context,
            )
        replay_set = _events(factory, workspace_id)
        assert len(replay_set) == len(first_set)
        assert {
            event.event_key
            for event in replay_set
            if event.event_type not in {"message.user", "turn.started"}
        } == first_keys

        # fenced finalize：agent.input.required 带 step_id 与 required key，
        # message.completed 带 finalize:terminal key，business.status 保持
        # v1.2 无 key 投影。
        candidate = replay_invocation.candidate
        assert candidate is not None
        verified = replay_invocation.verify_candidate(
            candidate,
            graph_stopped=True,
            graph_state_reader=replay_graph.compiled.get_state,
            state_validator=lambda state: any(
                task.interrupts for task in state.tasks
            ),
        )
        with service.advisory_lock(agent_thread_id) as lock:
            terminal_id = service.finalize_interrupt(
                handle,
                workspace_token=token,
                lock=lock,
                verified=verified,
                pending_input_id=pending_input_id,
                draft_revision=1,
            )

        final_events = _events(factory, workspace_id)
        required = next(
            event
            for event in final_events
            if event.event_type == "agent.input.required"
        )
        assert required.payload["step_id"] == identity_step_id(
            workspace_id=handle.workspace_id,
            graph_run_id=handle.graph_run_id,
            input_seq=handle.input_seq,
            step_key=INPUT_STEP_KEY,
        )
        assert required.event_key == identity_event_key(
            workspace_id=handle.workspace_id,
            graph_run_id=handle.graph_run_id,
            input_seq=handle.input_seq,
            step_key=INPUT_STEP_KEY,
            lifecycle_phase="required",
            ordinal=0,
        )
        terminal = next(
            event for event in final_events if event.id == terminal_id
        )
        assert terminal.event_type == "message.completed"
        assert terminal.event_key == identity_event_key(
            workspace_id=handle.workspace_id,
            graph_run_id=handle.graph_run_id,
            input_seq=handle.input_seq,
            step_key="finalize:message.completed",
            lifecycle_phase="terminal",
            ordinal=0,
        )
        status_event = next(
            event for event in final_events if event.event_type == "business.status"
        )
        assert status_event.event_key is None

        runtime.close()


def test_complete_turn_terminal_event_carries_finalize_key() -> None:
    with _isolated_checkpoint_database() as settings:
        factory = build_session_factory(build_engine(settings.database_url))
        token, workspace_id, agent_thread_id, auth_session_id = _workspace_fixture(
            factory
        )
        service = TurnExecutionService(factory)
        handle = service.begin_input(
            workspace_token=token,
            auth_session_ref=auth_session_id,
            actor_id="EMP-001",
            safe_user_text="帮我看看我有哪些权限",
        )
        with service.advisory_lock(agent_thread_id) as lock:
            terminal_id = service.complete_turn_with_event(
                handle,
                workspace_token=token,
                lock=lock,
                event_type="message.completed",
                payload={
                    "turn_id": handle.input_turn_id,
                    "message_id": f"msg-{uuid4()}",
                    "content": "已为你查询可申请权限。",
                },
            )
        assert terminal_id is not None
        events = _events(factory, workspace_id)
        terminal = next(event for event in events if event.id == terminal_id)
        assert terminal.event_key == identity_event_key(
            workspace_id=handle.workspace_id,
            graph_run_id=handle.graph_run_id,
            input_seq=handle.input_seq,
            step_key="finalize:message.completed",
            lifecycle_phase="terminal",
            ordinal=0,
        )
