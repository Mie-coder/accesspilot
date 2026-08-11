"""T18 fixed-evaluation registry and product runner red tests."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from accesspilot.evaluation import (
    PRODUCT_SCENARIOS,
    T17_SCENARIOS,
    T18_SCENARIOS,
    EvaluationScenario,
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


def test_t18_registry_covers_numeric_context_without_mutating_t17() -> None:
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

    assert PRODUCT_SCENARIOS == T17_SCENARIOS + T18_SCENARIOS
    assert tuple(scenario.scenario_id for scenario in PRODUCT_SCENARIOS)[-1] == "T18-01"


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
