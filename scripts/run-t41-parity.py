#!/usr/bin/env python3
"""Run the fixed v1.3 parity matrix twice and emit observed evidence."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from accesspilot.agent.checkpoint import psycopg_connection_url

READ_ONLY_IDS = (
    "readonly.help",
    "readonly.security_probe",
    "readonly.numeric_no_cursor",
    "readonly.numeric_non_duration",
    "readonly.numeric_invalid",
    "readonly.numeric_over_limit",
    "readonly.eligible_access",
    "readonly.active_access",
    "readonly.request_status",
    "readonly.policy_catalog",
    "readonly.self_approval",
    "readonly.policy_grounded",
    "readonly.policy_insufficient",
    "readonly.policy_unavailable",
)
REQUEST_IDS = (
    "request.missing_fields",
    "request.canonical_eligible",
    "request.alias_matched",
    "request.old_draft_without_current_entitlement",
    "request.safe_justification_cursor",
    "request.ambiguous_entitlement",
    "request.canonical_but_ineligible",
    "request.invalid_resolver_argument",
    "request.validation_failed",
    "request.malformed_then_retry_success",
    "request.malformed_twice",
    "request.provider_http_failure",
    "request.revision_race",
    "request.numeric_duration",
    "request.quota_primary",
    "request.quota_retry",
)
CONFIRMATION_IDS = (
    "confirmation.explicit_confirm",
    "confirmation.explicit_reject",
    "confirmation.non_confirmation",
)
SCENARIO_IDS = READ_ONLY_IDS + REQUEST_IDS + CONFIRMATION_IDS

SELECTORS = (
    "apps/api/tests/agent/test_t31_readonly_graph.py::"
    "test_fixed_readonly_route_and_outcome_parity_is_100_percent_twice",
    "apps/api/tests/agent/test_t32_request_graph.py::"
    "test_request_collection_normalized_outcome_phase_quota_and_cursor_parity_twice",
    "apps/api/tests/agent/test_t32_request_graph.py::"
    "test_revision_race_matches_legacy_twice_and_leaves_latest_draft_authoritative",
    "apps/api/tests/agent/test_t32_request_graph.py::"
    "test_legal_numeric_duration_matches_legacy_twice_and_replays_before_cursor",
    "apps/api/tests/agent/test_t32_request_graph.py::"
    "test_quota_exception_matrix_matches_legacy_twice_without_partial_writes",
    "apps/api/tests/conversation/test_t27_orchestrator.py::"
    "test_legacy_orchestrator_freezes_explicit_confirmation_tristate",
    "apps/api/tests/agent/test_t34_graph_interrupt.py::"
    "test_resume_confirm_applies_confirmation_cas_exactly_once_in_one_call",
    "apps/api/tests/agent/test_t34_graph_interrupt.py::"
    "test_resume_explicit_rejection_is_not_a_confirmation",
    "apps/api/tests/agent/test_t34_graph_interrupt.py::"
    "test_resume_route_new_input_reclassifies_in_same_call_without_confirmation",
)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _case_results(path: Path) -> tuple[tuple[str, str], ...]:
    root = ElementTree.parse(path).getroot()
    results: list[tuple[str, str]] = []
    for case in root.iter("testcase"):
        name = case.attrib.get("name", "unnamed")
        status = (
            "skipped"
            if case.find("skipped") is not None
            else "failed"
            if case.find("failure") is not None or case.find("error") is not None
            else "passed"
        )
        results.append((name, status))
    return tuple(results)


def main() -> int:
    args = _args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    configured = os.getenv("ACCESSPILOT_T41_ADMIN_DATABASE_URL") or os.getenv(
        "ACCESSPILOT_T29_ADMIN_DATABASE_URL"
    )
    if not configured:
        raise SystemExit("ACCESSPILOT_T41_ADMIN_DATABASE_URL is required")
    admin_url = make_url(configured).set(drivername="postgresql", database="postgres")
    admin_conninfo = psycopg_connection_url(
        admin_url.render_as_string(hide_password=False)
    )
    database = f"t41_parity_{uuid4().hex[:12]}"
    database_url = admin_url.set(
        drivername="postgresql+psycopg", database=database
    ).render_as_string(hide_password=False)
    env = os.environ.copy()
    env["ACCESSPILOT_DATABASE_URL"] = database_url
    env["ACCESSPILOT_TEST_DATABASE_URL"] = database_url

    round_results: list[tuple[tuple[str, str], ...]] = []
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
        for round_number in (1, 2):
            junit = args.output.with_name(f"t41-parity-round-{round_number}.xml")
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    *SELECTORS,
                    "-q",
                    f"--junitxml={junit}",
                ],
                check=True,
                env=env,
            )
            results = _case_results(junit)
            if not results or any(status != "passed" for _name, status in results):
                raise SystemExit(f"parity round {round_number} was not all-pass")
            round_results.append(results)
        if round_results[0] != round_results[1]:
            raise SystemExit("parity rounds differ")
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

    report = {
        "schema_version": "accesspilot.t41.parity.v1",
        "rounds": 2,
        "scenario_count": len(SCENARIO_IDS),
        "scenario_ids": list(SCENARIO_IDS),
        "route_outcome_path_scenarios": len(READ_ONLY_IDS) + 14,
        "confirmation_semantics_scenarios": len(CONFIRMATION_IDS),
        "pytest_cases_per_round": len(round_results[0]),
        "zero_difference_rounds": 2,
        "route_outcome_match_rate": 1.0,
        "path_match_rate": 1.0,
    }
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
