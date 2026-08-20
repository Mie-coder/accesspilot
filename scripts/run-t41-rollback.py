#!/usr/bin/env python3
"""Run the disposable PostgreSQL T41 rollback drill with zero-skip enforcement."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["LANGGRAPH_STRICT_MSGPACK"] = "true"
    if env["LANGGRAPH_STRICT_MSGPACK"] != "true":
        raise SystemExit("T41 rollback requires strict msgpack")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "apps/api/tests/db/test_t41_rollback_drill.py",
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
