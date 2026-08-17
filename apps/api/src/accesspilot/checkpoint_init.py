"""Explicit deploy-time initialization for application and checkpoint schemas."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql
from psycopg.rows import dict_row

from accesspilot.agent.checkpoint import (
    CheckpointRuntimeLike,
    _assert_strict_msgpack_active,
    build_checkpoint_runtime,
    checkpoint_connection_kwargs,
    psycopg_connection_url,
)
from accesspilot.config import Settings


def run_application_alembic(settings: Settings) -> None:
    """Upgrade only AccessPilot-owned objects with the migration role."""

    if not settings.migration_database_url:
        raise ValueError("migration_database_url is required for initialization")
    root = Path(__file__).resolve().parents[4]
    config = Config(str(root / "alembic.ini"))
    config.attributes["database_url"] = settings.migration_database_url
    command.upgrade(config, "head")


def _runtime_role(database_url: str) -> str:
    with psycopg.connect(
        psycopg_connection_url(database_url),
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
    ) as connection:
        row = connection.execute("SELECT current_user AS role").fetchone()
    if not isinstance(row, dict):
        raise RuntimeError("could not resolve checkpoint runtime role")
    role = row.get("role")
    if not isinstance(role, str):
        raise RuntimeError("could not resolve checkpoint runtime role")
    return role


def run_official_checkpoint_setup(settings: Settings) -> None:
    """Create the isolated schema, run official migrations, then grant DML."""

    if not settings.checkpoint_migration_database_url:
        raise ValueError(
            "checkpoint_migration_database_url is required for initialization"
        )
    if not settings.checkpoint_database_url:
        raise ValueError("checkpoint_database_url is required for initialization")
    _assert_strict_msgpack_active()
    from langgraph.checkpoint.postgres import PostgresSaver

    runtime_role = _runtime_role(settings.checkpoint_database_url)
    conninfo = psycopg_connection_url(settings.checkpoint_migration_database_url)
    options = str(checkpoint_connection_kwargs(settings.checkpoint_schema)["options"])
    with psycopg.connect(
        conninfo,
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
        options=options,
    ) as connection:
        migration_row = connection.execute(
            "SELECT current_user AS role, current_database() AS database"
        ).fetchone()
        if not isinstance(migration_row, dict):
            raise RuntimeError("could not resolve checkpoint migration role")
        migration_role = migration_row["role"]
        database_name = migration_row["database"]
        if not isinstance(migration_role, str) or not isinstance(database_name, str):
            raise RuntimeError("invalid checkpoint migration identity")
        if runtime_role == migration_role:
            raise ValueError("checkpoint migration and runtime roles must be distinct")
        connection.execute(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {} AUTHORIZATION {}").format(
                sql.Identifier(settings.checkpoint_schema),
                sql.Identifier(migration_role),
            )
        )
        # PostgresSaver remains the sole owner of checkpoint table/index DDL.
        PostgresSaver(connection).setup()

        schema = sql.Identifier(settings.checkpoint_schema)
        role = sql.Identifier(runtime_role)
        database = sql.Identifier(database_name)
        connection.execute(
            sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM PUBLIC").format(database)
        )
        connection.execute(
            sql.SQL("REVOKE CREATE ON DATABASE {} FROM {}").format(database, role)
        )
        connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, role)
        )
        connection.execute(
            sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(schema)
        )
        connection.execute(
            sql.SQL("REVOKE ALL ON SCHEMA {} FROM {}").format(schema, role)
        )
        connection.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema, role)
        )
        connection.execute(
            sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}").format(
                schema, role
            )
        )
        runtime_tables = [
            sql.Identifier(settings.checkpoint_schema, table)
            for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
        ]
        connection.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE ON {} TO {}").format(
                sql.SQL(", ").join(runtime_tables), role
            )
        )


def initialize_checkpointing(
    settings: Settings,
    *,
    alembic_upgrade: Callable[[Settings], None] = run_application_alembic,
    checkpoint_setup: Callable[[Settings], None] = run_official_checkpoint_setup,
    runtime_factory: Callable[[Settings], CheckpointRuntimeLike] = build_checkpoint_runtime,
) -> None:
    """Run the one authorized DDL sequence, then prove runtime readiness."""

    if not settings.migration_database_url:
        raise ValueError("migration_database_url is required for initialization")
    if not settings.checkpoint_migration_database_url:
        raise ValueError(
            "checkpoint_migration_database_url is required for initialization"
        )
    if not settings.checkpoint_database_url:
        raise ValueError("checkpoint_database_url is required for initialization")
    alembic_upgrade(settings)
    checkpoint_setup(settings)
    runtime = runtime_factory(settings)
    runtime.start()
    try:
        runtime.check_readiness()
    finally:
        runtime.close()


def main() -> None:
    initialize_checkpointing(Settings())


if __name__ == "__main__":
    main()
