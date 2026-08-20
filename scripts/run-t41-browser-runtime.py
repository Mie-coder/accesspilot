#!/usr/bin/env python3
"""Prepare, serve and clean a deterministic disposable T41 browser runtime."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
import uvicorn
from psycopg import sql
from psycopg.rows import dict_row
from sqlalchemy.engine import make_url

from accesspilot.agent.checkpoint import psycopg_connection_url
from accesspilot.agent.embeddings import DeterministicEmbeddingModel
from accesspilot.bootstrap import bootstrap_database
from accesspilot.checkpoint_init import run_official_checkpoint_setup
from accesspilot.config import Settings
from accesspilot.db.session import build_engine, build_session_factory

_DATABASE_PATTERN = re.compile(r"t41_browser_([a-f0-9]{12})")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "serve", "inspect", "cleanup"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--state", type=Path, required=True)
    return parser


def _admin_url() -> str:
    configured = os.getenv("ACCESSPILOT_T41_ADMIN_DATABASE_URL") or os.getenv(
        "ACCESSPILOT_T29_ADMIN_DATABASE_URL"
    )
    if not configured:
        raise SystemExit("ACCESSPILOT_T41_ADMIN_DATABASE_URL is required")
    return configured


def _load_state(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in payload.items()
    ):
        raise SystemExit("invalid T41 browser runtime state")
    database = payload.get("database", "")
    match = _DATABASE_PATTERN.fullmatch(database)
    if match is None:
        raise SystemExit("refusing non-T41 browser runtime state")
    suffix = match.group(1)
    expected = {
        "migration_role": f"t41bm_{suffix}",
        "runtime_role": f"t41br_{suffix}",
        "checkpoint_schema": f"t41_browser_checkpoint_{suffix}",
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise SystemExit("T41 browser runtime identifiers do not match")
    return payload


def _cleanup_resources(state: dict[str, str], configured: str) -> None:
    admin_url = make_url(configured).set(drivername="postgresql", database="postgres")
    with psycopg.connect(
        psycopg_connection_url(admin_url.render_as_string(hide_password=False)),
        autocommit=True,
    ) as admin:
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (state["database"],),
        )
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {}").format(
                sql.Identifier(state["database"])
            )
        )
        admin.execute(
            sql.SQL("DROP ROLE IF EXISTS {}").format(
                sql.Identifier(state["runtime_role"])
            )
        )
        admin.execute(
            sql.SQL("DROP ROLE IF EXISTS {}").format(
                sql.Identifier(state["migration_role"])
            )
        )


def _prepare(state_path: Path) -> int:
    configured = _admin_url()
    admin_url = make_url(configured).set(drivername="postgresql", database="postgres")
    admin_conninfo = psycopg_connection_url(
        admin_url.render_as_string(hide_password=False)
    )
    suffix = uuid4().hex[:12]
    database = f"t41_browser_{suffix}"
    migration_role = f"t41bm_{suffix}"
    runtime_role = f"t41br_{suffix}"
    migration_password = f"m-{uuid4().hex}"
    runtime_password = f"r-{uuid4().hex}"
    checkpoint_schema = f"t41_browser_checkpoint_{suffix}"
    state = {
        "database": database,
        "migration_role": migration_role,
        "runtime_role": runtime_role,
        "checkpoint_schema": checkpoint_schema,
        "migration_url": admin_url.set(
            drivername="postgresql+psycopg",
            username=migration_role,
            password=migration_password,
            database=database,
        ).render_as_string(hide_password=False),
        "runtime_url": admin_url.set(
            drivername="postgresql+psycopg",
            username=runtime_role,
            password=runtime_password,
            database=database,
        ).render_as_string(hide_password=False),
    }
    created = False
    try:
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
        created = True
        with psycopg.connect(
            psycopg_connection_url(
                admin_url.set(database=database).render_as_string(hide_password=False)
            ),
            autocommit=True,
        ) as database_admin:
            database_admin.execute("CREATE EXTENSION vector")

        env = os.environ.copy()
        env["ACCESSPILOT_DATABASE_URL"] = state["migration_url"]
        env["LANGGRAPH_STRICT_MSGPACK"] = "true"
        env.pop("DEEPSEEK_API_KEY", None)
        env.pop("DASHSCOPE_API_KEY", None)
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            check=True,
            env=env,
        )
        os.environ["LANGGRAPH_STRICT_MSGPACK"] = "true"
        settings = Settings(
            database_url=state["migration_url"],
            checkpoint_migration_database_url=state["migration_url"],
            checkpoint_database_url=state["runtime_url"],
            checkpoint_schema=checkpoint_schema,
            orchestrator_mode="langgraph",
            demo_mode_enabled=True,
            _env_file=None,
        )
        run_official_checkpoint_setup(settings)
        result = bootstrap_database(
            build_session_factory(build_engine(state["migration_url"])),
            embedding_model=DeterministicEmbeddingModel(),
            embedding_mode="deterministic-offline",
        )
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        state_path.chmod(0o600)
        print(
            json.dumps(
                {
                    "database_kind": "disposable",
                    "database_prefix": "t41_browser_",
                    "checkpoint_kind": "official_postgres_saver",
                    "embedding_mode": result.embedding_mode,
                    "policy_count": result.policy_count,
                },
                sort_keys=True,
            )
        )
        return 0
    except BaseException:
        if created:
            _cleanup_resources(state, configured)
        state_path.unlink(missing_ok=True)
        raise


def _serve(state_path: Path) -> int:
    state = _load_state(state_path)
    os.environ.update(
        {
            "ACCESSPILOT_DATABASE_URL": state["migration_url"],
            "ACCESSPILOT_MIGRATION_DATABASE_URL": state["migration_url"],
            "ACCESSPILOT_CHECKPOINT_MIGRATION_DATABASE_URL": state["migration_url"],
            "ACCESSPILOT_CHECKPOINT_DATABASE_URL": state["runtime_url"],
            "ACCESSPILOT_CHECKPOINT_SCHEMA": state["checkpoint_schema"],
            "ACCESSPILOT_ORCHESTRATOR_MODE": "langgraph",
            "ACCESSPILOT_DEMO_MODE_ENABLED": "true",
            "ACCESSPILOT_WEB_ORIGIN": "http://127.0.0.1:5173",
            "LANGGRAPH_STRICT_MSGPACK": "true",
        }
    )
    os.environ.pop("DEEPSEEK_API_KEY", None)
    os.environ.pop("DASHSCOPE_API_KEY", None)
    # Settings' supported no-key fallback is selected without reading the
    # developer .env; the editable package remains importable after chdir.
    os.chdir("/private/tmp")
    uvicorn.run("accesspilot.main:app", host="127.0.0.1", port=8000)
    return 0


def _inspect(state_path: Path) -> int:
    """Print non-sensitive acceptance counts from the disposable runtime."""

    state = _load_state(state_path)
    conninfo = psycopg_connection_url(state["migration_url"])
    with psycopg.connect(conninfo, row_factory=dict_row) as connection:
        facts = connection.execute(
            """
            SELECT
              (SELECT count(*) FROM workspaces) AS workspaces,
              (SELECT count(*) FROM auth_sessions) AS auth_sessions,
              (SELECT count(*) FROM access_requests) AS formal_requests,
              (SELECT count(*) FROM decision_packets) AS decision_packets,
              (SELECT count(*) FROM approval_cases) AS approval_cases,
              (SELECT count(*) FROM approval_steps) AS approval_steps,
              (SELECT count(*) FROM approval_steps WHERE step_status = 'approved')
                AS approved_steps,
              (SELECT count(*) FROM access_grants) AS access_grants,
              (SELECT count(*) FROM provisioning_attempts) AS provisioning_attempts,
              (SELECT count(*) FROM provisioning_attempts
                 WHERE provisioning_status = 'succeeded')
                AS succeeded_provisioning_attempts,
              (SELECT count(*) FROM workspace_events) AS workspace_events,
              (SELECT count(*) FROM agent_turn_executions) AS agent_turns,
              (SELECT count(*) FROM agent_step_executions) AS agent_steps,
              (SELECT count(*) FROM agent_step_executions
                 WHERE status <> 'completed') AS incomplete_agent_steps,
              (SELECT count(*) FROM agent_pending_inputs
                 WHERE status IN ('active', 'resuming')) AS live_pending_inputs,
              (SELECT count(*) FROM agent_pending_inputs
                 WHERE status = 'resolved') AS resolved_pending_inputs,
              (SELECT count(*) FROM agent_turn_executions execution
                 LEFT JOIN workspace_events terminal
                   ON terminal.workspace_id = execution.workspace_id
                  AND terminal.id = execution.terminal_event_id
                 WHERE execution.status <> 'running'
                   AND terminal.id IS NULL) AS missing_terminal_events
            """
        ).fetchone()
        if facts is None:
            raise SystemExit("T41 browser runtime facts query returned no row")
        request_states = {
            str(row["request_status"]): int(row["row_count"])
            for row in connection.execute(
                "SELECT request_status, count(*) AS row_count "
                "FROM access_requests GROUP BY request_status ORDER BY request_status"
            ).fetchall()
        }
        approval_states = {
            str(row["approval_status"]): int(row["row_count"])
            for row in connection.execute(
                "SELECT approval_status, count(*) AS row_count "
                "FROM approval_cases GROUP BY approval_status "
                "ORDER BY approval_status"
            ).fetchall()
        }
        turn_states = {
            str(row["status"]): int(row["row_count"])
            for row in connection.execute(
                "SELECT status, count(*) AS row_count FROM agent_turn_executions "
                "GROUP BY status ORDER BY status"
            ).fetchall()
        }
        checkpoint_counts: dict[str, int] = {}
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            count = connection.execute(
                sql.SQL("SELECT count(*) AS row_count FROM {}.{}").format(
                    sql.Identifier(state["checkpoint_schema"]),
                    sql.Identifier(table),
                )
            ).fetchone()
            if count is None:
                raise SystemExit(f"T41 checkpoint count missing for {table}")
            checkpoint_counts[table] = int(count["row_count"])

    payload = {key: int(value) for key, value in facts.items()}
    payload.update(
        {
            "request_states": request_states,
            "approval_states": approval_states,
            "turn_states": turn_states,
            "checkpoint_rows": checkpoint_counts,
        }
    )
    print(json.dumps(payload, sort_keys=True))
    return 0


def _cleanup(state_path: Path) -> int:
    state = _load_state(state_path)
    _cleanup_resources(state, _admin_url())
    state_path.unlink(missing_ok=True)
    print('{"cleanup":"passed","database_prefix":"t41_browser_"}')
    return 0


def main() -> int:
    args = _parser().parse_args()
    if args.command == "prepare":
        return _prepare(args.state)
    if args.command == "serve":
        return _serve(args.state)
    if args.command == "inspect":
        return _inspect(args.state)
    return _cleanup(args.state)


if __name__ == "__main__":
    raise SystemExit(main())
