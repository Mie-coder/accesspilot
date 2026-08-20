#!/usr/bin/env python3
"""Run the complete API suite on a uniquely named disposable PostgreSQL DB."""

from __future__ import annotations

import argparse
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
    parser.add_argument(
        "selectors",
        nargs="*",
        default=["apps/api/tests"],
        help="Optional pytest selectors; the release gate leaves this at the full suite.",
    )
    return parser.parse_args()


def main() -> int:
    args = _args()
    configured = os.getenv("ACCESSPILOT_T41_ADMIN_DATABASE_URL") or os.getenv(
        "ACCESSPILOT_T29_ADMIN_DATABASE_URL"
    )
    if not configured:
        raise SystemExit("ACCESSPILOT_T41_ADMIN_DATABASE_URL is required")

    admin_url = make_url(configured).set(drivername="postgresql", database="postgres")
    admin_conninfo = psycopg_connection_url(
        admin_url.render_as_string(hide_password=False)
    )
    database = f"t41_suite_{uuid4().hex[:12]}"
    database_url = admin_url.set(
        drivername="postgresql+psycopg", database=database
    ).render_as_string(hide_password=False)
    env = os.environ.copy()
    env.update(
        {
            "ACCESSPILOT_DATABASE_URL": database_url,
            "ACCESSPILOT_TEST_DATABASE_URL": database_url,
            "ACCESSPILOT_T26_DATABASE_URL": database_url,
            "ACCESSPILOT_T29_ADMIN_DATABASE_URL": configured,
            "ACCESSPILOT_T30_ADMIN_DATABASE_URL": configured,
            "ACCESSPILOT_T33_ADMIN_DATABASE_URL": configured,
            "ACCESSPILOT_T35_ADMIN_DATABASE_URL": configured,
            "ACCESSPILOT_T36_ADMIN_DATABASE_URL": configured,
            "ACCESSPILOT_T41_ADMIN_DATABASE_URL": configured,
            "LANGGRAPH_STRICT_MSGPACK": "true",
        }
    )
    # T41 is an offline deterministic gate. Tests construct Settings with
    # ``_env_file=None``; removing inherited keys therefore selects the
    # repository's supported deterministic adapters without an empty SecretStr.
    env.pop("DEEPSEEK_API_KEY", None)
    env.pop("DASHSCOPE_API_KEY", None)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    try:
        with psycopg.connect(admin_conninfo, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        with psycopg.connect(
            psycopg_connection_url(
                admin_url.set(database=database).render_as_string(hide_password=False)
            ),
            autocommit=True,
        ) as database_admin:
            database_admin.execute("CREATE EXTENSION vector")
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            check=True,
            env=env,
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                *args.selectors,
                "-q",
                f"--junitxml={args.output}",
            ],
            check=True,
            env=env,
        )
        subprocess.run(
            [
                sys.executable,
                "scripts/assert-t41-test-report.py",
                str(args.output),
                "--format",
                "junit",
                "--require-no-skips",
            ],
            check=True,
            env=env,
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
