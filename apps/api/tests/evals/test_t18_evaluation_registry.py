"""T18 fixed-evaluation registry and product runner red tests."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from accesspilot.evaluation import (
    ACTIVE_T17_SCENARIOS,
    CURRENT_T08_COMPATIBLE_SELECTORS,
    DEFERRED_T08_SELECTORS,
    PRODUCT_SCENARIOS,
    SUPERSEDED_T08_SELECTORS,
    SUPERSEDED_T17_SCENARIO_IDS,
    T08_BASELINE_CASE_COUNT,
    T08_BASELINE_GIT_REVISION,
    T17_SCENARIOS,
    T18_SCENARIOS,
    T19_SCENARIOS,
    EvaluationScenario,
)

EXPECTED_CURRENT_T08_COMPATIBLE_SELECTORS = (
    "apps/api/tests/evals/test_fixed_scenarios.py::"
    "test_eval_02_missing_fields_remain_a_draft",
    "apps/api/tests/evals/test_fixed_scenarios.py::"
    "test_eval_03_complete_but_unconfirmed_cannot_submit",
    "apps/api/tests/evals/test_fixed_scenarios.py::"
    "test_eval_04_policy_failure_is_recoverable_without_approval",
    "apps/api/tests/evals/test_fixed_scenarios.py::"
    "test_eval_07_timeout_recovers_by_querying_original_operation",
    "apps/api/tests/evals/test_fixed_scenarios.py::"
    "test_eval_08_duplicate_retry_reuses_one_attempt_and_grant",
    "apps/api/tests/evals/test_fixed_scenarios.py::"
    "test_eval_09_sse_reconnect_only_replays_newer_events",
    "apps/api/tests/evals/test_fixed_scenarios.py::"
    "test_eval_10_quota_exhaustion_keeps_history_readable",
    "apps/api/tests/evals/test_fixed_scenarios.py::"
    "test_eval_12_two_malformed_replies_fail_closed",
)

EXPECTED_DEFERRED_T08_SELECTORS = (
    "apps/api/tests/evals/test_fixed_scenarios.py::"
    "test_eval_01_golden_path_creates_auditable_grant",
    "apps/api/tests/evals/test_fixed_scenarios.py::"
    "test_eval_05_data_owner_cannot_approve_out_of_order",
    "apps/api/tests/evals/test_fixed_scenarios.py::test_eval_06_rejection_is_terminal",
)

EXPECTED_SUPERSEDED_T08_SELECTORS = (
    "apps/api/tests/evals/test_fixed_scenarios.py::"
    "test_eval_11_workspace_cannot_read_another_request",
)


def _load_product_eval_runner():
    script_path = Path(__file__).resolve().parents[4] / "scripts" / "run-product-evals.py"
    spec = importlib.util.spec_from_file_location(
        "accesspilot_run_product_evals_t18_test_module",
        script_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_junit(path: Path, scenario: EvaluationScenario) -> None:
    cases = "".join(
        f'<testcase classname="{scenario.scenario_id}" name="case-{index}" time="0.01" />'
        for index in range(scenario.expected_case_count)
    )
    path.write_text(
        f"<testsuites><testsuite name=\"product-fixed\">{cases}</testsuite></testsuites>",
        encoding="utf-8",
    )


def test_current_registry_preserves_history_without_double_counting_superseded_t17() -> None:
    assert tuple(scenario.scenario_id for scenario in T17_SCENARIOS) == tuple(
        f"T17-{index:02d}" for index in range(1, 11)
    )
    assert tuple(scenario.scenario_id for scenario in T18_SCENARIOS) == ("T18-01",)
    assert T18_SCENARIOS[0].category == "numeric_context"
    assert T18_SCENARIOS[0].expected_case_count == 11
    selectors = "\n".join(T18_SCENARIOS[0].selectors)
    for required in (
        "test_duration_cursor_accepts_111_up_to_catalog_limit_without_model",
        "test_duration_cursor_rejects_catalog_over_limit_without_mutation",
        "test_numeric_follow_up_respects_non_duration_cursor",
        "test_numeric_message_without_active_cursor_needs_clarification_without_model_call",
        "test_help_has_priority_and_clears_active_cursor",
        "test_invalid_duration_keeps_cursor_and_revision",
    ):
        assert required in selectors

    assert tuple(scenario.scenario_id for scenario in T19_SCENARIOS) == ("T19-01",)
    assert T19_SCENARIOS[0].category == "auth_session_isolation"
    assert T19_SCENARIOS[0].expected_case_count == 23
    selectors = "\n".join(T19_SCENARIOS[0].selectors)
    assert "test_t19_auth.py" in selectors

    assert SUPERSEDED_T17_SCENARIO_IDS == frozenset({"T17-01", "T17-09"})
    assert tuple(scenario.scenario_id for scenario in ACTIVE_T17_SCENARIOS) == (
        "T17-02",
        "T17-03",
        "T17-04",
        "T17-05",
        "T17-06",
        "T17-07",
        "T17-08",
        "T17-10",
    )
    assert sum(scenario.expected_case_count for scenario in T17_SCENARIOS) == 30
    assert sum(scenario.expected_case_count for scenario in ACTIVE_T17_SCENARIOS) == 24
    assert PRODUCT_SCENARIOS == ACTIVE_T17_SCENARIOS + T18_SCENARIOS + T19_SCENARIOS
    assert tuple(scenario.scenario_id for scenario in PRODUCT_SCENARIOS)[-1] == "T19-01"
    assert sum(scenario.expected_case_count for scenario in PRODUCT_SCENARIOS) == 58

    all_selectors = [selector for scenario in PRODUCT_SCENARIOS for selector in scenario.selectors]
    assert len(all_selectors) == len(set(all_selectors))
    assert not any(
        "test_t19_auth.py" in selector
        for scenario in ACTIVE_T17_SCENARIOS
        for selector in scenario.selectors
    )


def test_current_registry_collects_exactly_58_unique_pytest_cases() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    selectors = [selector for scenario in PRODUCT_SCENARIOS for selector in scenario.selectors]

    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *selectors],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    node_ids = tuple(line for line in completed.stdout.splitlines() if "::" in line)
    assert len(node_ids) == 58
    assert len(node_ids) == len(set(node_ids))


def test_t08_history_and_current_compatible_evidence_are_explicitly_separated() -> None:
    assert T08_BASELINE_GIT_REVISION == "c683d84"
    assert T08_BASELINE_CASE_COUNT == 12
    assert DEFERRED_T08_SELECTORS == EXPECTED_DEFERRED_T08_SELECTORS
    assert SUPERSEDED_T08_SELECTORS == EXPECTED_SUPERSEDED_T08_SELECTORS
    assert CURRENT_T08_COMPATIBLE_SELECTORS == (
        EXPECTED_CURRENT_T08_COMPATIBLE_SELECTORS
    )
    assert (
        len(DEFERRED_T08_SELECTORS)
        + len(SUPERSEDED_T08_SELECTORS)
        + len(CURRENT_T08_COMPATIBLE_SELECTORS)
        == T08_BASELINE_CASE_COUNT
    )


def test_fixed_scenario_file_collects_only_the_eight_current_compatible_cases() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "apps/api/tests/evals/test_fixed_scenarios.py",
        ],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    node_ids = tuple(line for line in completed.stdout.splitlines() if "::" in line)
    assert node_ids == CURRENT_T08_COMPATIBLE_SELECTORS


def test_product_runner_uses_aggregate_registry_and_product_output_defaults(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = _load_product_eval_runner()
    scenario = T18_SCENARIOS[0]
    output_path = tmp_path / "product-evaluation.json"
    monkeypatch.setattr(sys, "argv", ["run-product-evals.py"])
    default_args = runner._parse_args()
    assert default_args.output.name == "accesspilot-product-evaluation.json"

    def fake_subprocess_run(command: list[str], **_: object) -> SimpleNamespace:
        junit_argument = next(
            argument for argument in command if argument.startswith("--junitxml=")
        )
        _write_junit(Path(junit_argument.removeprefix("--junitxml=")), scenario)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runner, "PRODUCT_SCENARIOS", (scenario,))
    monkeypatch.setattr(runner.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(runner, "_git_revision", lambda _: "abc123")
    monkeypatch.setattr(
        runner,
        "_parse_args",
        lambda: SimpleNamespace(output=output_path, list=False),
    )

    assert runner.main() == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["summary"]["scenarios_total"] == 1
    assert payload["scenarios"][0]["scenario_id"] == "T18-01"


def test_product_wrapper_uses_product_report_default() -> None:
    wrapper = Path(__file__).resolve().parents[4] / "scripts" / "run-evals.sh"
    content = wrapper.read_text(encoding="utf-8")
    assert "/private/tmp/accesspilot-product-evaluation.json" in content
    assert "/private/tmp/accesspilot-t17-evaluation.json" not in content
