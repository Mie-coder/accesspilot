"""T29 real PostgreSQL proof using disposable database and login roles only."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, NotRequired, TypedDict
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.types import Command, interrupt
from psycopg import sql
from psycopg.errors import InsufficientPrivilege
from psycopg.rows import dict_row
from sqlalchemy import create_engine, select

from accesspilot.agent.checkpoint import (
    AcceptedCheckpointHeadStore,
    CandidateCheckpointRejected,
    CheckpointLocator,
    ExactCheckpointRequired,
    FencedPostgresSaverAdapter,
    PostgresCheckpointRuntime,
    ServerExecutionContext,
    checkpoint_connection_kwargs,
    psycopg_connection_url,
)
from accesspilot.checkpoint_init import initialize_checkpointing
from accesspilot.config import Settings
from accesspilot.db.models import AgentTurnExecutionRecord
from accesspilot.db.seed import seed_catalog
from accesspilot.db.session import build_session_factory
from accesspilot.main import create_app
from support.auth import login_as


class _State(TypedDict):
    request: str
    decision: NotRequired[str]


def _await_confirmation(state: _State) -> dict[str, str]:
    decision = interrupt({"kind": "confirmation", "request": state["request"]})
    return {"decision": str(decision)}


def _graph(checkpointer: object) -> object:
    builder = StateGraph(_State)
    builder.add_node("await_confirmation", _await_confirmation)
    builder.add_edge(START, "await_confirmation")
    builder.add_edge("await_confirmation", END)
    return builder.compile(checkpointer=checkpointer)


class _RecordingSaver:
    def __init__(self, saver: object, *, fail_writes_after_put: bool = False) -> None:
        self._saver = saver
        self._fail_writes_after_put = fail_writes_after_put
        self.trace: list[tuple[str, str, tuple[str, ...]]] = []
        self.version_calls: list[tuple[object | None, str]] = []

    def __getattr__(self, name: str) -> object:
        return getattr(self._saver, name)

    def get_tuple(self, config):  # type: ignore[no-untyped-def]
        return self._saver.get_tuple(config)  # type: ignore[attr-defined]

    def get_next_version(self, current: object | None, channel: None) -> str:
        version = self._saver.get_next_version(current, channel)  # type: ignore[attr-defined]
        assert isinstance(version, str)
        self.version_calls.append((current, version))
        return version

    def put(self, config, checkpoint, metadata, new_versions):  # type: ignore[no-untyped-def]
        saved = self._saver.put(  # type: ignore[attr-defined]
            config, checkpoint, metadata, new_versions
        )
        self.trace.append(("put", saved["configurable"]["checkpoint_id"], ()))
        return saved

    def put_writes(self, config, writes, task_id, task_path=""):  # type: ignore[no-untyped-def]
        if self._fail_writes_after_put and any(entry[0] == "put" for entry in self.trace):
            raise RuntimeError("injected failure after official put")
        self._saver.put_writes(config, writes, task_id, task_path)  # type: ignore[attr-defined]
        self.trace.append(
            (
                "put_writes",
                config["configurable"]["checkpoint_id"],
                tuple(channel for channel, _ in writes),
            )
        )


@contextmanager
def _isolated_database() -> Iterator[tuple[str, str, str, str, str]]:
    configured = os.getenv("ACCESSPILOT_T29_ADMIN_DATABASE_URL")
    if not configured:
        pytest.skip("set ACCESSPILOT_T29_ADMIN_DATABASE_URL for the isolated T29 proof")
    from sqlalchemy.engine import make_url

    admin_url = make_url(configured).set(database="postgres")
    admin_conninfo = psycopg_connection_url(
        admin_url.render_as_string(hide_password=False)
    )
    suffix = uuid4().hex[:12]
    database = f"t29_db_{suffix}"
    migration_role = f"t29_migration_{suffix}"
    runtime_role = f"t29_runtime_{suffix}"
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
    # pgvector is an infrastructure prerequisite whose installation requires
    # superuser on PostgreSQL 16.  The application migration remains owned and
    # executed by the non-superuser migration role below.
    database_admin_url = admin_url.set(database=database).render_as_string(
        hide_password=False
    )
    with psycopg.connect(
        psycopg_connection_url(database_admin_url), autocommit=True
    ) as database_admin:
        database_admin.execute("CREATE EXTENSION vector")
    migration_url = admin_url.set(
        username=migration_role,
        password=migration_password,
        database=database,
    ).render_as_string(hide_password=False)
    runtime_url = admin_url.set(
        username=runtime_role,
        password=runtime_password,
        database=database,
    ).render_as_string(hide_password=False)
    try:
        yield admin_conninfo, database, migration_role, migration_url, runtime_url
    finally:
        with psycopg.connect(admin_conninfo, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database,),
            )
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database)))
            admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(runtime_role)))
            admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(migration_role)))


def _fingerprint(connection: psycopg.Connection[Any], schema: str) -> tuple[Any, ...]:
    tables = connection.execute(
        "SELECT array_agg(tablename ORDER BY tablename) AS tables "
        "FROM pg_tables WHERE schemaname = %s",
        (schema,),
    ).fetchone()
    migrations = connection.execute(
        sql.SQL("SELECT array_agg(v ORDER BY v) AS versions FROM {}.checkpoint_migrations").format(
            sql.Identifier(schema)
        )
    ).fetchone()
    assert isinstance(tables, dict) and isinstance(migrations, dict)
    return tuple(tables["tables"]), tuple(migrations["versions"])


def _application_fingerprint(connection: psycopg.Connection[Any]) -> tuple[Any, ...]:
    rows = connection.execute(
        "SELECT table_name, column_name, data_type, is_nullable "
        "FROM information_schema.columns WHERE table_schema = 'public' "
        "ORDER BY table_name, column_name"
    ).fetchall()
    return tuple(tuple(row.values()) for row in rows)


def _seed_execution(
    migration_url: str,
    *,
    graph_run_id: UUID,
    checkpoint_thread_id: str,
) -> UUID:
    workspace_id = uuid4()
    auth_id = uuid4()
    execution_id = uuid4()
    now = datetime.now(UTC)
    with psycopg.connect(
        psycopg_connection_url(migration_url), autocommit=True, row_factory=dict_row
    ) as connection:
        connection.execute(
            "INSERT INTO employees (employee_id, name, department, manager_id, roles) "
            "VALUES ('EMP-T29', 'T29 Fictional User', 'Testing', NULL, ARRAY[]::varchar[])"
        )
        connection.execute(
            "INSERT INTO workspaces "
            "(id, agent_thread_id, flow_version, lease_fence, token_hash, actor_id, "
            "demo_session_active, model_call_limit, model_calls_used, model_retry_consumed, "
            "created_at, draft_revision) "
            "VALUES (%s, %s, 1, 7, %s, 'EMP-T29', false, 20, 0, 0, %s, 0)",
            (workspace_id, uuid4(), uuid4().hex * 2, now),
        )
        connection.execute(
            "INSERT INTO auth_sessions "
            "(id, token_hash, employee_id, workspace_id, csrf_hash, expires_at, created_at) "
            "VALUES (%s, %s, 'EMP-T29', %s, %s, %s, %s)",
            (auth_id, "a" * 64, workspace_id, "b" * 64, now + timedelta(hours=1), now),
        )
        event = connection.execute(
            "INSERT INTO workspace_events (workspace_id, event_type, payload, created_at) "
            "VALUES (%s, 'message.user', '{}'::jsonb, %s) RETURNING id",
            (workspace_id, now),
        ).fetchone()
        assert isinstance(event, dict)
        connection.execute(
            "INSERT INTO agent_turn_executions "
            "(id, workspace_id, graph_run_id, checkpoint_thread_id, input_seq, "
            "input_turn_id, input_event_id, auth_session_ref, actor_id, engine, attempt, "
            "lease_fence, lease_expires_at, status, checkpoint_ns, accepted_checkpoint_id, "
            "terminal_event_id, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, 0, %s, %s, %s, 'EMP-T29', 'langgraph', 1, "
            "7, %s, 'running', '', NULL, NULL, %s, %s)",
            (
                execution_id,
                workspace_id,
                graph_run_id,
                checkpoint_thread_id,
                f"turn-{uuid4()}",
                event["id"],
                auth_id,
                now + timedelta(minutes=5),
                now,
                now,
            ),
        )
    return execution_id


def test_t29_real_postgres_roles_lifecycle_exact_head_and_no_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _isolated_database() as (
        _admin_conninfo,
        _database,
        migration_role,
        migration_url,
        runtime_url,
    ):
        schema = f"t29_checkpoint_{uuid4().hex[:12]}"
        settings = Settings(
            database_url=migration_url,
            migration_database_url=migration_url,
            checkpoint_migration_database_url=migration_url,
            checkpoint_database_url=runtime_url,
            checkpoint_schema=schema,
            orchestrator_mode="mixed",
            langgraph_canary_percent=0,
            _env_file=None,
        )
        preinit_engine = create_engine(migration_url)
        preinit_app = create_app(
            settings=settings,
            session_factory=build_session_factory(preinit_engine),
        )
        with TestClient(preinit_app) as preinit_client:
            assert preinit_client.get("/health").status_code == 200
            assert preinit_client.get("/ready").status_code == 503
        preinit_engine.dispose()

        initialize_checkpointing(settings)
        with psycopg.connect(
            psycopg_connection_url(migration_url),
            **checkpoint_connection_kwargs(schema),
        ) as migration:
            first_fingerprint = _fingerprint(migration, schema)
            first_application_fingerprint = _application_fingerprint(migration)
        initialize_checkpointing(settings)
        with psycopg.connect(
            psycopg_connection_url(migration_url),
            **checkpoint_connection_kwargs(schema),
        ) as migration:
            assert _fingerprint(migration, schema) == first_fingerprint
            assert _application_fingerprint(migration) == first_application_fingerprint
            migration.execute(
                sql.SQL("CREATE TABLE {}.migration_ddl_proof (id integer)").format(
                    sql.Identifier(schema)
                )
            )
            migration.execute(
                sql.SQL("DROP TABLE {}.migration_ddl_proof").format(sql.Identifier(schema))
            )
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
        alembic_config = Config(os.path.join(root, "alembic.ini"))
        alembic_config.attributes["database_url"] = migration_url
        command.check(alembic_config)
        command.downgrade(alembic_config, "20260812_0009")
        with psycopg.connect(
            psycopg_connection_url(migration_url),
            **checkpoint_connection_kwargs(schema),
        ) as migration:
            assert _fingerprint(migration, schema) == first_fingerprint
        command.upgrade(alembic_config, "head")
        with psycopg.connect(
            psycopg_connection_url(migration_url),
            **checkpoint_connection_kwargs(schema),
        ) as migration:
            assert _fingerprint(migration, schema) == first_fingerprint
            assert _application_fingerprint(migration) == first_application_fingerprint

        runtime = PostgresCheckpointRuntime(settings)
        runtime.start()
        runtime.check_readiness()
        assert runtime.pool is not None and runtime.saver is not None
        with runtime.pool.connection() as first, runtime.pool.connection() as second:
            assert first is not second
            for connection in (first, second):
                assert connection.autocommit is True
                assert connection.prepare_threshold == 0
                assert connection.row_factory is dict_row
                assert schema in connection.execute("SHOW search_path").fetchone()["search_path"]
            role = first.execute(
                "SELECT current_user AS role, rolsuper, rolcreatedb, rolcreaterole, "
                "has_database_privilege(current_user, current_database(), 'CONNECT') "
                "AS can_connect, "
                "has_database_privilege(current_user, current_database(), 'CREATE') "
                "AS can_create, "
                "has_database_privilege(current_user, current_database(), 'TEMPORARY') "
                "AS can_temp "
                "FROM pg_roles WHERE rolname = current_user"
            ).fetchone()
            assert role == {
                "role": role["role"],
                "rolsuper": False,
                "rolcreatedb": False,
                "rolcreaterole": False,
                "can_connect": True,
                "can_create": False,
                "can_temp": False,
            }
            first.execute(
                "INSERT INTO checkpoints "
                "(thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata) "
                "VALUES ('permission-proof', '', 'head', '{}'::jsonb, '{}'::jsonb)"
            )
            first.execute(
                "UPDATE checkpoints SET metadata = '{\"checked\": true}'::jsonb "
                "WHERE thread_id = 'permission-proof'"
            )
            assert first.execute(
                "SELECT metadata FROM checkpoints WHERE thread_id = 'permission-proof'"
            ).fetchone()["metadata"] == {"checked": True}
            for statement in (
                "DELETE FROM checkpoints WHERE thread_id = 'permission-proof'",
                "CREATE TABLE runtime_ddl_forbidden (id integer)",
                "ALTER TABLE checkpoints ADD COLUMN forbidden integer",
                "DROP TABLE checkpoint_writes",
                "CREATE TEMP TABLE runtime_temp_ddl_forbidden (id integer)",
            ):
                with pytest.raises(InsufficientPrivilege):
                    first.execute(statement)
            with pytest.raises(InsufficientPrivilege):
                first.execute("SELECT * FROM checkpoint_migrations")

        captured: list[PostgresCheckpointRuntime] = []

        def runtime_factory(active: Settings) -> PostgresCheckpointRuntime:
            created = PostgresCheckpointRuntime(active)
            captured.append(created)
            return created

        app_engine = create_engine(migration_url)
        session_factory = build_session_factory(app_engine)
        with session_factory() as session:
            seed_catalog(session)
        from langgraph.checkpoint.postgres import PostgresSaver

        monkeypatch.setattr(
            PostgresSaver,
            "setup",
            lambda self: (_ for _ in ()).throw(AssertionError("runtime called setup")),
        )
        app_settings = settings.model_copy(update={"demo_mode_enabled": True})
        app_a = create_app(
            settings=app_settings,
            session_factory=session_factory,
            checkpoint_runtime_factory=runtime_factory,
        )
        app_b = create_app(
            settings=app_settings,
            session_factory=session_factory,
            checkpoint_runtime_factory=runtime_factory,
        )
        with TestClient(app_a) as client_a:
            with TestClient(app_b) as client_b:
                assert client_a.get("/ready").status_code == 200
                assert client_b.get("/ready").status_code == 200
                assert captured[0].pool is not captured[1].pool
                login_as(client_a, session_factory=session_factory)
                assert client_a.post(
                    "/api/chat/messages", json={"content": "帮助"}
                ).status_code == 200
                assert client_a.post(
                    "/api/chat/messages/stream", json={"content": "帮助"}
                ).status_code == 200
            assert captured[1].pool is not None and captured[1].pool.closed
            assert client_a.get("/ready").status_code == 200
        assert captured[0].pool is not None and captured[0].pool.closed
        app_engine.dispose()

        graph_run_id = uuid4()
        checkpoint_thread_id = f"accesspilot:v1.3:{graph_run_id}"
        execution_id = _seed_execution(
            migration_url,
            graph_run_id=graph_run_id,
            checkpoint_thread_id=checkpoint_thread_id,
        )
        orm_engine = create_engine(migration_url)
        orm_factory = build_session_factory(orm_engine)
        with orm_factory() as session:
            record = session.scalar(
                select(AgentTurnExecutionRecord).where(
                    AgentTurnExecutionRecord.id == execution_id
                )
            )
            assert record is not None
            context = ServerExecutionContext.from_record(record)

        half_recording = _RecordingSaver(runtime.saver, fail_writes_after_put=True)
        half_invocation = FencedPostgresSaverAdapter(half_recording).for_execution(
            context
        )
        half_graph = _graph(half_invocation)
        base_config = {
            "configurable": {"thread_id": checkpoint_thread_id, "checkpoint_ns": ""}
        }
        with pytest.raises(RuntimeError, match="injected failure after official put"):
            half_graph.invoke(
                {"request": "fictional half-written access"},
                base_config,
                durability="sync",
            )
        half_candidate = half_invocation.candidate
        assert half_candidate is not None
        assert runtime.saver.get_tuple(base_config) is not None
        with orm_factory() as session:
            half_execution = session.get(AgentTurnExecutionRecord, execution_id)
            assert half_execution is not None
            assert half_execution.accepted_checkpoint_id is None
        half_recovery = FencedPostgresSaverAdapter(runtime.saver).for_execution(context)
        assert half_recovery.get_tuple(base_config) is None
        with pytest.raises(CandidateCheckpointRejected, match="stopped"):
            half_invocation.verify_candidate(
                half_candidate,
                graph_stopped=False,
                graph_state_reader=half_graph.get_state,
                state_validator=lambda state: True,
            )

        recording = _RecordingSaver(runtime.saver)
        invocation = FencedPostgresSaverAdapter(recording).for_execution(context)
        graph = _graph(invocation)
        result = graph.invoke(
            {"request": "fictional read access"}, base_config, durability="sync"
        )
        assert "__interrupt__" in result
        candidate = invocation.candidate
        assert candidate is not None, recording.trace
        assert recording.version_calls
        assert all(
            re.fullmatch(r"[0-9]{32}\.[0-9]+\.[0-9]+", version)
            for _, version in recording.version_calls
        )
        exact_candidate = invocation.get_exact(candidate.locator)
        candidate_versions = exact_candidate.checkpoint["channel_versions"]
        assert candidate_versions
        assert all(
            isinstance(version, str)
            and re.fullmatch(r"[0-9]{32}\.[0-9]+\.[0-9]+", version)
            for version in candidate_versions.values()
        )
        interrupt_write = next(
            index
            for index, entry in enumerate(recording.trace)
            if entry[0] == "put_writes" and "__interrupt__" in entry[2]
        )
        assert any(
            entry[0] == "put" and entry[1] == recording.trace[interrupt_write][1]
            for entry in recording.trace[:interrupt_write]
        )
        with orm_factory() as session:
            unaccepted = session.get(AgentTurnExecutionRecord, execution_id)
            assert unaccepted is not None
            assert unaccepted.accepted_checkpoint_id is None

        raw_latest = runtime.saver.get_tuple(base_config)
        assert raw_latest is not None
        fresh = FencedPostgresSaverAdapter(runtime.saver).for_execution(context)
        with pytest.raises(ExactCheckpointRequired):
            fresh.get_exact(
                CheckpointLocator(checkpoint_thread_id, "", "missing-head")
            )
        with pytest.raises(CandidateCheckpointRejected, match="stopped"):
            invocation.verify_candidate(
                candidate,
                graph_stopped=False,
                graph_state_reader=graph.get_state,
                state_validator=lambda state: True,
            )

        verified = invocation.verify_candidate(
            candidate,
            graph_stopped=True,
            graph_state_reader=graph.get_state,
            state_validator=lambda state: any(task.interrupts for task in state.tasks),
        )
        with orm_factory.begin() as session:
            assert AcceptedCheckpointHeadStore().promote(session, verified) is True
        with orm_factory() as session:
            accepted = session.get(AgentTurnExecutionRecord, execution_id)
            assert accepted is not None
            assert accepted.accepted_checkpoint_id == candidate.locator.checkpoint_id
            accepted.lease_fence = 8
            session.commit()
        with orm_factory() as session:
            accepted = session.get(AgentTurnExecutionRecord, execution_id)
            assert accepted is not None
            assert accepted.accepted_checkpoint_id == candidate.locator.checkpoint_id
            orphan_context = ServerExecutionContext.from_record(accepted)

        orphan_recording = _RecordingSaver(runtime.saver)
        orphan_invocation = FencedPostgresSaverAdapter(orphan_recording).for_execution(
            orphan_context
        )
        orphan_graph = _graph(orphan_invocation)
        orphan_graph.invoke(
            Command(resume="approved"),
            orphan_context.accepted_locator.as_config(),
            durability="sync",
        )
        orphan_candidate = orphan_invocation.candidate
        assert orphan_candidate is not None
        assert orphan_candidate.locator.checkpoint_id != candidate.locator.checkpoint_id
        orphan_verified = orphan_invocation.verify_candidate(
            orphan_candidate,
            graph_stopped=True,
            graph_state_reader=orphan_graph.get_state,
            state_validator=lambda state: not state.next
            and not any(task.interrupts for task in state.tasks),
        )
        with orm_factory() as session:
            accepted = session.get(AgentTurnExecutionRecord, execution_id)
            assert accepted is not None
            accepted.lease_fence = 9
            session.commit()
        with orm_factory.begin() as session:
            assert AcceptedCheckpointHeadStore().promote(session, orphan_verified) is False
        raw_latest = runtime.saver.get_tuple(base_config)
        assert raw_latest is not None
        assert (
            raw_latest.config["configurable"]["checkpoint_id"]
            == orphan_candidate.locator.checkpoint_id
        )
        with orm_factory() as session:
            accepted = session.get(AgentTurnExecutionRecord, execution_id)
            assert accepted is not None
            assert accepted.accepted_checkpoint_id == candidate.locator.checkpoint_id
            current_context = ServerExecutionContext.from_record(accepted)
        exact_invocation = FencedPostgresSaverAdapter(runtime.saver).for_execution(
            current_context
        )
        assert exact_invocation.get_exact(current_context.accepted_locator) is not None
        with pytest.raises(ExactCheckpointRequired, match="checkpoint_id"):
            exact_invocation.get_tuple(base_config)

        runtime.close()
        assert runtime.pool is not None and runtime.pool.closed
        restarted_runtime = PostgresCheckpointRuntime(settings)
        restarted_runtime.start()
        restarted_runtime.check_readiness()
        assert restarted_runtime.saver is not None
        restarted_exact = FencedPostgresSaverAdapter(
            restarted_runtime.saver
        ).for_execution(current_context)
        assert restarted_exact.get_exact(current_context.accepted_locator) is not None
        restarted_runtime.close()
        assert restarted_runtime.pool is not None and restarted_runtime.pool.closed
        orm_engine.dispose()
        with psycopg.connect(
            psycopg_connection_url(migration_url), row_factory=dict_row
        ) as connection:
            owner = connection.execute(
                "SELECT schema_owner FROM information_schema.schemata WHERE schema_name = %s",
                (schema,),
            ).fetchone()
            assert owner == {"schema_owner": migration_role}
