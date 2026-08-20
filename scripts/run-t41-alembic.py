#!/usr/bin/env python3
"""Run Alembic upgrade/check against a uniquely named disposable database."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from accesspilot.agent.checkpoint import psycopg_connection_url


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _args()
    configured = os.getenv("ACCESSPILOT_T29_ADMIN_DATABASE_URL")
    if not configured:
        raise SystemExit("ACCESSPILOT_T29_ADMIN_DATABASE_URL is required")

    admin_url = make_url(configured).set(drivername="postgresql", database="postgres")
    admin_conninfo = psycopg_connection_url(
        admin_url.render_as_string(hide_password=False)
    )
    database = f"t41_alembic_{uuid4().hex[:12]}"
    database_url = admin_url.set(
        drivername="postgresql+psycopg", database=database
    ).render_as_string(hide_password=False)
    env = os.environ.copy()
    env["ACCESSPILOT_DATABASE_URL"] = database_url
    commands = (
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        [sys.executable, "-m", "alembic", "check"],
    )

    try:
        with psycopg.connect(admin_conninfo, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        for command in commands:
            subprocess.run(command, check=True, env=env)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                {
                    "database_kind": "disposable",
                    "database_prefix": "t41_alembic_",
                    "upgrade": "passed",
                    "check": "passed",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
