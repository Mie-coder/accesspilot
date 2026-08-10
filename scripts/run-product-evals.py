#!/usr/bin/env python3
"""Run the fixed v1.1 evaluation registry and write a redacted JSON report."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from accesspilot.evaluation import (
    T17_SCENARIOS,
    build_evaluation_report,
    parse_junit_suite,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run AccessPilot's deterministic productized evaluation suite.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/private/tmp/accesspilot-t17-evaluation.json"),
        help="Redacted JSON report destination.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List fixed scenario IDs without running tests.",
    )
    return parser.parse_args()


def _git_revision(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def main() -> int:
    args = _parse_args()
    if args.list:
        for scenario in T17_SCENARIOS:
            print(f"{scenario.scenario_id}\t{scenario.category}")
        return 0

    # A failed or interrupted run must never leave an older report looking current.
    args.output.unlink(missing_ok=True)

    repo_root = Path(__file__).resolve().parents[1]
    results = []
    failed_scenarios: list[str] = []
    with tempfile.TemporaryDirectory(prefix="accesspilot-t17-") as temp_dir:
        temp_root = Path(temp_dir)
        for scenario in T17_SCENARIOS:
            junit_path = temp_root / f"{scenario.scenario_id}.xml"
            command = [
                sys.executable,
                "-m",
                "pytest",
                *scenario.selectors,
                "-q",
                f"--junitxml={junit_path}",
            ]
            completed = subprocess.run(
                command,
                cwd=repo_root,
                check=False,
                capture_output=True,
                text=True,
            )
            if not junit_path.exists():
                print(f"{scenario.scenario_id}: no JUnit evidence produced", file=sys.stderr)
                return 2
            result = parse_junit_suite(junit_path, scenario)
            results.append(result)
            passed = (
                completed.returncode == 0
                and not result.failed_cases
                and result.case_count == scenario.expected_case_count
            )
            if not passed:
                failed_scenarios.append(scenario.scenario_id)
            print(f"{scenario.scenario_id}: {result.passed}/{result.case_count} fixed cases passed")

    if failed_scenarios:
        print(
            "Failed scenarios: " + ", ".join(failed_scenarios),
            file=sys.stderr,
        )
        return 1

    report = build_evaluation_report(
        results,
        git_revision=_git_revision(repo_root),
        command="./scripts/run-evals.sh",
        adapter_mode="deterministic_offline",
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(f"Redacted report: {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
