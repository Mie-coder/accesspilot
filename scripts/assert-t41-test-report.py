#!/usr/bin/env python3
"""Fail closed when a T41 JUnit/Vitest report is empty, failed, or skipped."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from xml.etree import ElementTree


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("--format", choices=("junit", "vitest"), required=True)
    parser.add_argument("--require-no-skips", action="store_true")
    return parser.parse_args()


def _junit_counts(path: Path) -> dict[str, int]:
    root = ElementTree.parse(path).getroot()
    cases = list(root.iter("testcase"))
    return {
        "total": len(cases),
        "passed": sum(
            not any(case.find(tag) is not None for tag in ("failure", "error", "skipped"))
            for case in cases
        ),
        "failed": sum(
            any(case.find(tag) is not None for tag in ("failure", "error"))
            for case in cases
        ),
        "skipped": sum(case.find("skipped") is not None for case in cases),
    }


def _vitest_counts(path: Path) -> dict[str, int]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        "total": int(payload.get("numTotalTests", 0)),
        "passed": int(payload.get("numPassedTests", 0)),
        "failed": int(payload.get("numFailedTests", 0)),
        "skipped": int(payload.get("numPendingTests", 0)),
    }


def main() -> int:
    args = _args()
    counts = (
        _junit_counts(args.report)
        if args.format == "junit"
        else _vitest_counts(args.report)
    )
    if counts["total"] <= 0:
        raise SystemExit("T41 test report is empty")
    if counts["failed"] or counts["passed"] != counts["total"] - counts["skipped"]:
        raise SystemExit(f"T41 test report contains failures: {counts}")
    if args.require_no_skips and counts["skipped"]:
        raise SystemExit(f"T41 test report contains skips: {counts}")
    print(json.dumps(counts, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
