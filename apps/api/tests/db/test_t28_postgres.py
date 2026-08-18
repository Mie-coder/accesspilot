"""Real PostgreSQL migration and constraint proofs for T28."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.config import Settings
from accesspilot.db.models import WorkspaceRecord
from accesspilot.db.workspace_store import SqlAlchemyWorkspaceStore
from accesspilot.events import append_workspace_event, format_sse_event
from accesspilot.main import create_app
from accesspilot.workspaces import WorkspaceService

ROOT = Path(__file__).parents[4]
WORKSPACE_1 = UUID("10000000-0000-0000-0000-000000000001")
WORKSPACE_2 = UUID("10000000-0000-0000-0000-000000000002")
AUTH_1 = UUID("20000000-0000-0000-0000-000000000001")
AUTH_2 = UUID("20000000-0000-0000-0000-000000000002")
RUN_1 = UUID("30000000-0000-0000-0000-000000000001")
RUN_2 = UUID("30000000-0000-0000-0000-000000000002")


def _config(url: URL, monkeypatch: pytest.MonkeyPatch) -> Config:
    rendered = url.render_as_string(hide_password=False)
    monkeypatch.setenv("ACCESSPILOT_DATABASE_URL", rendered)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", rendered.replace("%", "%%"))
    return config


@contextmanager
def _isolated_database(
    session_factory: sessionmaker[Session],
    *,
    with_checkpoint_sentinel: bool,
) -> Iterator[tuple[Engine, str | None]]:
    base = session_factory.kw["bind"]
    assert isinstance(base, Engine)
    database_name = f"t28_test_{uuid4().hex}"
    checkpoint_schema = (
        f"t28_checkpoint_{uuid4().hex}" if with_checkpoint_sentinel else None
    )
    with base.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}" TEMPLATE template0'))
    isolated = create_engine(base.url.set(database=database_name, query={}), pool_pre_ping=True)
    with isolated.begin() as connection:
        if checkpoint_schema is not None:
            connection.execute(text(f'CREATE SCHEMA "{checkpoint_schema}"'))
            connection.execute(
                text(
                    f'CREATE TABLE "{checkpoint_schema}".sentinel '
                    "(id integer PRIMARY KEY, payload text NOT NULL)"
                )
            )
            connection.execute(
                text(
                    f'INSERT INTO "{checkpoint_schema}".sentinel '
                    "(id, payload) VALUES (1, 'keep-me')"
                )
            )
    try:
        yield isolated, checkpoint_schema
    finally:
        isolated.dispose()
        with base.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(f'DROP DATABASE "{database_name}"'))


def _checkpoint_fingerprint(base: Engine, schema: str) -> tuple[object, ...]:
    with base.connect() as connection:
        row = connection.execute(
            text(
                "SELECT count(*), md5(string_agg(id::text || ':' || payload, "
                f"',' ORDER BY id)) FROM \"{schema}\".sentinel"
            )
        ).one()
    return tuple(row)


def _seed_0009_data(scoped: Engine) -> tuple[int, int, int]:
    token_1 = "t28-existing-workspace-1"
    token_2 = "t28-existing-workspace-2"
    with scoped.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO employees "
                "(employee_id, name, department, manager_id, roles) "
                "VALUES ('EMP-001', 'T28 Applicant', 'Security', NULL, "
                "ARRAY['employee']::varchar[])"
            )
        )
        for workspace_id, token in (
            (WORKSPACE_1, token_1),
            (WORKSPACE_2, token_2),
        ):
            connection.execute(
                text(
                    "INSERT INTO workspaces (id, token_hash, actor_id, created_at) "
                    "VALUES (:id, :token_hash, 'EMP-001', CURRENT_TIMESTAMP)"
                ),
                {
                    "id": workspace_id,
                    "token_hash": sha256(token.encode("utf-8")).hexdigest(),
                },
            )
        for auth_id, workspace_id, suffix in (
            (AUTH_1, WORKSPACE_1, "1"),
            (AUTH_2, WORKSPACE_2, "2"),
        ):
            connection.execute(
                text(
                    "INSERT INTO auth_sessions "
                    "(id, token_hash, employee_id, workspace_id, csrf_hash, "
                    "expires_at, created_at) VALUES "
                    "(:id, :token_hash, 'EMP-001', :workspace_id, :csrf_hash, "
                    "CURRENT_TIMESTAMP + INTERVAL '1 day', CURRENT_TIMESTAMP)"
                ),
                {
                    "id": auth_id,
                    "workspace_id": workspace_id,
                    "token_hash": suffix * 64,
                    "csrf_hash": ("a" if suffix == "1" else "b") * 64,
                },
            )
        event_1 = connection.execute(
            text(
                "INSERT INTO workspace_events "
                "(workspace_id, event_type, payload, created_at) VALUES "
                "(:workspace_id, 'message.user', "
                "CAST(:payload AS jsonb), CURRENT_TIMESTAMP) RETURNING id"
            ),
            {"workspace_id": WORKSPACE_1, "payload": '{"turn_id":"turn-1","content":"one"}'},
        ).scalar_one()
        terminal_1 = connection.execute(
            text(
                "INSERT INTO workspace_events "
                "(workspace_id, event_type, payload, created_at) VALUES "
                "(:workspace_id, 'message.completed', "
                "CAST(:payload AS jsonb), CURRENT_TIMESTAMP) RETURNING id"
            ),
            {"workspace_id": WORKSPACE_1, "payload": '{"turn_id":"turn-1"}'},
        ).scalar_one()
        event_2 = connection.execute(
            text(
                "INSERT INTO workspace_events "
                "(workspace_id, event_type, payload, created_at) VALUES "
                "(:workspace_id, 'message.user', "
                "CAST(:payload AS jsonb), CURRENT_TIMESTAMP) RETURNING id"
            ),
            {"workspace_id": WORKSPACE_2, "payload": '{"turn_id":"turn-2","content":"two"}'},
        ).scalar_one()
    return int(event_1), int(terminal_1), int(event_2)


def _reject(connection: object, statement: str, **params: object) -> None:
    with pytest.raises(DBAPIError), connection.begin_nested():  # type: ignore[attr-defined]
        connection.execute(text(statement), params)  # type: ignore[attr-defined]


def _execution_insert(
    *,
    execution_id: UUID,
    workspace_id: UUID,
    run_id: UUID,
    turn_id: str,
    input_event_id: int,
    auth_id: UUID,
    status: str,
    lease: str,
    terminal_event_id: int | None,
    checkpoint_ns: str = "",
) -> tuple[str, dict[str, object]]:
    statement = (
        "INSERT INTO agent_turn_executions "
        "(id, workspace_id, graph_run_id, checkpoint_thread_id, input_seq, "
        "input_turn_id, input_event_id, auth_session_ref, actor_id, engine, "
        "attempt, lease_fence, lease_expires_at, status, checkpoint_ns, "
        "accepted_checkpoint_id, terminal_event_id, created_at, updated_at) "
        "VALUES (:id, :workspace_id, :run_id, :checkpoint_thread_id, 0, "
        ":turn_id, :input_event_id, :auth_id, 'EMP-001', 'langgraph', 1, 1, "
        f"{lease}, :status, :checkpoint_ns, NULL, :terminal_event_id, "
        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    )
    return statement, {
        "id": execution_id,
        "workspace_id": workspace_id,
        "run_id": run_id,
        "checkpoint_thread_id": f"accesspilot:v1.3:{run_id}",
        "turn_id": turn_id,
        "input_event_id": input_event_id,
        "auth_id": auth_id,
        "status": status,
        "checkpoint_ns": checkpoint_ns,
        "terminal_event_id": terminal_event_id,
    }


def test_fresh_upgrade_has_no_metadata_drift(
    database_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _isolated_database(
        database_session_factory, with_checkpoint_sentinel=False
    ) as (database, _):
        config = _config(database.url, monkeypatch)
        command.upgrade(config, "head")

        assert {
            "agent_turn_executions",
            "agent_pending_inputs",
            "agent_step_executions",
        } <= set(inspect(database).get_table_names())
        command.check(config)


def test_populated_cycle_enforces_runtime_facts_and_preserves_checkpoint_schema(
    database_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _isolated_database(
        database_session_factory, with_checkpoint_sentinel=True
    ) as (database, checkpoint_schema):
        assert checkpoint_schema is not None
        fingerprint = _checkpoint_fingerprint(database, checkpoint_schema)
        config = _config(database.url, monkeypatch)
        command.upgrade(config, "20260812_0009")
        event_1, terminal_1, event_2 = _seed_0009_data(database)

        command.upgrade(config, "20260817_0010")
        assert _checkpoint_fingerprint(database, checkpoint_schema) == fingerprint

        event_indexes = {
            index["name"]: index for index in inspect(database).get_indexes("workspace_events")
        }
        event_predicate = str(
            event_indexes["uq_workspace_events_workspace_event_key_not_null"]
            ["dialect_options"]["postgresql_where"]
        )
        assert "event_key IS NOT NULL" in event_predicate
        execution_indexes = {
            index["name"]: index
            for index in inspect(database).get_indexes("agent_turn_executions")
        }
        execution_predicate = str(
            execution_indexes["uq_agent_turn_executions_running_workspace"]
            ["dialect_options"]["postgresql_where"]
        )
        assert "status" in execution_predicate and "running" in execution_predicate
        pending_indexes = {
            index["name"]: index
            for index in inspect(database).get_indexes("agent_pending_inputs")
        }
        pending_predicate = str(
            pending_indexes["uq_agent_pending_inputs_live_workspace"]
            ["dialect_options"]["postgresql_where"]
        )
        assert all(value in pending_predicate for value in ("status", "active", "resuming"))

        with database.begin() as connection:
            workspaces = connection.execute(
                text(
                    "SELECT id, agent_thread_id, flow_version, lease_fence "
                    "FROM workspaces ORDER BY id"
                )
            ).all()
            assert len(workspaces) == 2
            assert workspaces[0].agent_thread_id != workspaces[1].agent_thread_id
            assert all(
                row.agent_thread_id is not None
                and row.flow_version == 1
                and row.lease_fence == 0
                for row in workspaces
            )
            assert connection.execute(
                text("SELECT count(*) FROM workspace_events WHERE event_key IS NULL")
            ).scalar_one() == 3

            # Nullable keys coexist; non-null keys deduplicate only per Workspace.
            key = "evt_" + "a" * 64
            for workspace_id in (WORKSPACE_1, WORKSPACE_2):
                connection.execute(
                    text(
                        "INSERT INTO workspace_events "
                        "(workspace_id, event_type, event_key, payload, created_at) "
                        "VALUES (:workspace_id, 'security.notice', :event_key, "
                        "'{}'::jsonb, CURRENT_TIMESTAMP)"
                    ),
                    {"workspace_id": workspace_id, "event_key": key},
                )
            _reject(
                connection,
                "INSERT INTO workspace_events "
                "(workspace_id, event_type, event_key, payload, created_at) "
                "VALUES (:workspace_id, 'security.notice', :event_key, "
                "'{}'::jsonb, CURRENT_TIMESTAMP)",
                workspace_id=WORKSPACE_1,
                event_key=key,
            )

            valid_sql, valid_params = _execution_insert(
                execution_id=UUID("40000000-0000-0000-0000-000000000001"),
                workspace_id=WORKSPACE_1,
                run_id=RUN_1,
                turn_id="turn-running-1",
                input_event_id=event_1,
                auth_id=AUTH_1,
                status="running",
                lease="CURRENT_TIMESTAMP + INTERVAL '5 minutes'",
                terminal_event_id=None,
            )
            connection.execute(text(valid_sql), valid_params)

            another_input = connection.execute(
                text(
                    "INSERT INTO workspace_events "
                    "(workspace_id, event_type, payload, created_at) VALUES "
                    "(:workspace_id, 'message.user', '{}'::jsonb, "
                    "CURRENT_TIMESTAMP) RETURNING id"
                ),
                {"workspace_id": WORKSPACE_1},
            ).scalar_one()
            second_running_sql, second_running_params = _execution_insert(
                execution_id=uuid4(),
                workspace_id=WORKSPACE_1,
                run_id=RUN_2,
                turn_id="turn-running-duplicate",
                input_event_id=int(another_input),
                auth_id=AUTH_1,
                status="running",
                lease="CURRENT_TIMESTAMP + INTERVAL '5 minutes'",
                terminal_event_id=None,
            )
            _reject(connection, second_running_sql, **second_running_params)

            cross_sql, cross_params = _execution_insert(
                execution_id=uuid4(),
                workspace_id=WORKSPACE_1,
                run_id=RUN_2,
                turn_id="turn-cross-workspace",
                input_event_id=event_2,
                auth_id=AUTH_1,
                status="completed",
                lease="NULL",
                terminal_event_id=terminal_1,
            )
            _reject(connection, cross_sql, **cross_params)

            invalid_ns_sql, invalid_ns_params = _execution_insert(
                execution_id=uuid4(),
                workspace_id=WORKSPACE_2,
                run_id=RUN_2,
                turn_id="turn-invalid-namespace",
                input_event_id=event_2,
                auth_id=AUTH_2,
                status="running",
                lease="CURRENT_TIMESTAMP + INTERVAL '5 minutes'",
                terminal_event_id=None,
                checkpoint_ns="business-namespace",
            )
            _reject(connection, invalid_ns_sql, **invalid_ns_params)

            invalid_status_sql, invalid_status_params = _execution_insert(
                execution_id=uuid4(),
                workspace_id=WORKSPACE_2,
                run_id=RUN_2,
                turn_id="turn-invalid-status-shape",
                input_event_id=event_2,
                auth_id=AUTH_2,
                status="completed",
                lease="CURRENT_TIMESTAMP + INTERVAL '5 minutes'",
                terminal_event_id=None,
            )
            _reject(connection, invalid_status_sql, **invalid_status_params)

            # Workspace runtime identity is immutable and its fence monotonic.
            _reject(
                connection,
                "UPDATE workspaces SET agent_thread_id = gen_random_uuid() WHERE id = :id",
                id=WORKSPACE_1,
            )
            connection.execute(
                text("UPDATE workspaces SET lease_fence = 1 WHERE id = :id"),
                {"id": WORKSPACE_1},
            )
            _reject(
                connection,
                "UPDATE workspaces SET lease_fence = 0 WHERE id = :id",
                id=WORKSPACE_1,
            )

            agent_thread_1 = workspaces[0].agent_thread_id
            agent_thread_2 = workspaces[1].agent_thread_id
            pending_insert = (
                "INSERT INTO agent_pending_inputs "
                "(id, workspace_id, agent_thread_id, graph_run_id, "
                "checkpoint_thread_id, pending_input_id, kind, draft_revision, "
                "auth_session_ref, actor_id, engine, checkpoint_ns, "
                "accepted_checkpoint_id, status, resume_input_seq, retired_at, "
                "retirement_reason, created_at, updated_at) VALUES "
                "(:id, :workspace_id, :agent_thread_id, :run_id, "
                ":checkpoint_thread_id, :pending_input_id, 'confirmation', 0, "
                ":auth_id, 'EMP-001', 'langgraph', '', 'head-1', :status, "
                ":resume_input_seq, :retired_at, :retirement_reason, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
            connection.execute(
                text(pending_insert),
                {
                    "id": uuid4(),
                    "workspace_id": WORKSPACE_1,
                    "agent_thread_id": agent_thread_1,
                    "run_id": RUN_1,
                    "checkpoint_thread_id": f"accesspilot:v1.3:{RUN_1}",
                    "pending_input_id": uuid4(),
                    "auth_id": AUTH_1,
                    "status": "active",
                    "resume_input_seq": None,
                    "retired_at": None,
                    "retirement_reason": None,
                },
            )
            _reject(
                connection,
                pending_insert,
                id=uuid4(),
                workspace_id=WORKSPACE_1,
                agent_thread_id=agent_thread_1,
                run_id=RUN_1,
                checkpoint_thread_id=f"accesspilot:v1.3:{RUN_1}",
                pending_input_id=uuid4(),
                auth_id=AUTH_1,
                status="resuming",
                resume_input_seq=1,
                retired_at=None,
                retirement_reason=None,
            )
            _reject(
                connection,
                pending_insert,
                id=uuid4(),
                workspace_id=WORKSPACE_2,
                agent_thread_id=agent_thread_2,
                run_id=RUN_2,
                checkpoint_thread_id=f"accesspilot:v1.3:{RUN_2}",
                pending_input_id=uuid4(),
                auth_id=AUTH_2,
                status="active",
                resume_input_seq=1,
                retired_at=None,
                retirement_reason=None,
            )
            tombstone_id = uuid4()
            connection.execute(
                text(pending_insert),
                {
                    "id": tombstone_id,
                    "workspace_id": WORKSPACE_2,
                    "agent_thread_id": agent_thread_2,
                    "run_id": RUN_2,
                    "checkpoint_thread_id": f"accesspilot:v1.3:{RUN_2}",
                    "pending_input_id": uuid4(),
                    "auth_id": AUTH_2,
                    "status": "abandoned_conflict",
                    "resume_input_seq": None,
                    "retired_at": "2026-08-17T00:00:00+00:00",
                    "retirement_reason": "verified revision conflict",
                },
            )
            _reject(
                connection,
                "UPDATE agent_pending_inputs SET accepted_checkpoint_id = 'head-2' "
                "WHERE id = :id",
                id=tombstone_id,
            )

            step_insert = (
                "INSERT INTO agent_step_executions "
                "(id, workspace_id, graph_run_id, input_seq, step_key, "
                "operation_id, status, result_reference, committed_revision, "
                "created_at, completed_at) VALUES "
                "(:id, :workspace_id, :run_id, 0, 'persist_draft', "
                ":operation_id, :status, :result_reference, :revision, "
                "CURRENT_TIMESTAMP, :completed_at)"
            )
            reserved_operation = "op_" + "b" * 64
            connection.execute(
                text(step_insert),
                {
                    "id": uuid4(),
                    "workspace_id": WORKSPACE_1,
                    "run_id": RUN_1,
                    "operation_id": reserved_operation,
                    "status": "reserved",
                    "result_reference": None,
                    "revision": None,
                    "completed_at": None,
                },
            )
            _reject(
                connection,
                step_insert,
                id=uuid4(),
                workspace_id=WORKSPACE_1,
                run_id=RUN_1,
                operation_id="op_" + "c" * 64,
                status="completed",
                result_reference=None,
                revision=None,
                completed_at="2026-08-17T00:00:00+00:00",
            )
            _reject(
                connection,
                step_insert,
                id=uuid4(),
                workspace_id=WORKSPACE_1,
                run_id=RUN_1,
                operation_id=reserved_operation,
                status="reserved",
                result_reference=None,
                revision=None,
                completed_at=None,
            )

        # A normal Store round trip has no write path for private runtime facts.
        scoped_factory = sessionmaker(bind=database, expire_on_commit=False)
        store = SqlAlchemyWorkspaceStore(scoped_factory)
        before = None
        with scoped_factory() as session:
            row = session.get(WorkspaceRecord, WORKSPACE_1)
            assert row is not None
            before = (row.agent_thread_id, row.flow_version, row.lease_fence)
        loaded = store.get("t28-existing-workspace-1")
        assert loaded is not None
        store.save(loaded)
        with scoped_factory() as session:
            row = session.get(WorkspaceRecord, WORKSPACE_1)
            assert row is not None
            assert (row.agent_thread_id, row.flow_version, row.lease_fence) == before
            keyed_event = append_workspace_event(
                session,
                workspace_token="t28-existing-workspace-1",
                event_type="message.assistant",
                event_key="evt_" + "d" * 64,
                payload={"content": "safe replay fact"},
            )
            assert keyed_event.event_key == "evt_" + "d" * 64
            assert "event_key" not in format_sse_event(keyed_event)

        # New service-created Workspace receives private facts in its transaction;
        # public HTTP responses and request bodies never expose/accept their names.
        created = WorkspaceService(store).create()
        persisted_created = store.get(created.token)
        assert persisted_created is not None
        with scoped_factory() as session:
            row = session.get(WorkspaceRecord, persisted_created.workspace_id)
            assert row is not None
            assert row.agent_thread_id is not None
            assert row.flow_version == 1
            assert row.lease_fence == 0
        app = create_app(
            settings=Settings(database_url=database.url.render_as_string(hide_password=False)),
            session_factory=scoped_factory,
        )
        with TestClient(app) as client:
            injected = client.post(
                "/api/auth/login",
                json={"account_id": "EMP-001", "flow_version": 2},
                headers={"Origin": "http://127.0.0.1:5173"},
            )
            assert injected.status_code == 422
            response = client.post(
                "/api/auth/login",
                json={"account_id": "EMP-001"},
                headers={"Origin": "http://127.0.0.1:5173"},
            )
            assert response.status_code == 200
            serialized = response.text
            assert "agent_thread_id" not in serialized
            assert "flow_version" not in serialized
            assert "lease_fence" not in serialized

        command.downgrade(config, "20260812_0009")
        assert _checkpoint_fingerprint(database, checkpoint_schema) == fingerprint
        with database.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM workspaces")).scalar_one() == 4
            assert connection.execute(
                text("SELECT count(*) FROM workspace_events")
            ).scalar_one() >= 5

        command.upgrade(config, "20260817_0010")
        assert _checkpoint_fingerprint(database, checkpoint_schema) == fingerprint
        command.check(config)
