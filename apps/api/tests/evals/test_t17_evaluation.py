"""T17 产品化评测合同的红测。

本文件只锁定评测清单、JUnit 解析和证据报告的公开合同；真正的业务场景
仍由各 Ticket 的确定性测试负责。T17 的报告只能汇总已经观察到的结果，
不能把未测的召回率、P95 或供应商 token 延迟填成看似精确的数字。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import pytest

from accesspilot.evaluation import (
    T17_SCENARIOS,
    EvaluationReport,
    EvaluationScenario,
    build_evaluation_report,
    parse_junit_suite,
)

EXPECTED_SCENARIO_IDS = tuple(f"T17-{index:02d}" for index in range(1, 11))
EXPECTED_CATEGORIES = {
    "identity_demo",
    "resolution",
    "current_turn_stream",
    "workspace_reconnect",
    "turn_interrupted",
    "policy_states",
    "self_approval",
    "budget_degradation",
    "draft_isolation",
    "compound_security",
}


def _scenario(scenario_id: str) -> EvaluationScenario:
    return next(scenario for scenario in T17_SCENARIOS if scenario.scenario_id == scenario_id)


def _write_junit(
    path: Path,
    *,
    scenario_id: str,
    case_names: tuple[str, ...],
    failed_cases: frozenset[str] = frozenset(),
    include_stream_observations: bool = False,
    stream_observation_overrides: dict[str, str] | None = None,
) -> Path:
    """写 pytest record_property 形态的最小 JUnit XML。"""

    properties = ""
    if include_stream_observations:
        stream_observations = {
            "first_event_ms": "12.5",
            "first_token_ms": "34.75",
            "completion_ms": "78.25",
            "terminal_event": "message.completed",
            "model_calls": "1",
        }
        if stream_observation_overrides:
            stream_observations.update(stream_observation_overrides)
        properties = dedent(
            f"""
            <properties>
              <property name="first_event_ms" value="{stream_observations["first_event_ms"]}" />
              <property name="first_token_ms" value="{stream_observations["first_token_ms"]}" />
              <property name="completion_ms" value="{stream_observations["completion_ms"]}" />
              <property name="terminal_event" value="{stream_observations["terminal_event"]}" />
              <property name="model_calls" value="{stream_observations["model_calls"]}" />
            </properties>
            """
        ).strip()
    testcases: list[str] = []
    for index, case_name in enumerate(case_names, start=1):
        failure = (
            '<failure message="fixed assertion failed">expected fact missing</failure>'
            if case_name in failed_cases
            else ""
        )
        testcases.append(
            f'<testcase classname="{scenario_id}" name="{case_name}" '
            f'time="0.0{index}0">{properties}{failure}</testcase>'
        )
    path.write_text(
        '<testsuites><testsuite name="t17-fixed">'
        + "".join(testcases)
        + "</testsuite></testsuites>",
        encoding="utf-8",
    )
    return path


def _load_product_eval_runner():
    script_path = Path(__file__).resolve().parents[4] / "scripts" / "run-product-evals.py"
    spec = importlib.util.spec_from_file_location(
        "accesspilot_run_product_evals_test_module",
        script_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_t17_registry_freezes_ten_scenarios_and_safe_selectors() -> None:
    scenarios = tuple(T17_SCENARIOS)

    assert tuple(scenario.scenario_id for scenario in scenarios) == EXPECTED_SCENARIO_IDS
    assert {scenario.category for scenario in scenarios} == EXPECTED_CATEGORIES
    assert len({scenario.scenario_id for scenario in scenarios}) == 10
    for scenario in scenarios:
        assert scenario.selectors
        assert len(scenario.selectors) == len(set(scenario.selectors))
        assert scenario.expected_case_count >= 1
        assert scenario.adapter_mode == "deterministic_offline"

    compound = _scenario("T17-10")
    assert compound.category == "compound_security"
    assert compound.expected_case_count == 4


def test_t17_runner_removes_stale_output_when_a_scenario_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_product_eval_runner()
    output_path = tmp_path / "evaluation.json"
    output_path.write_text("stale report", encoding="utf-8")

    scenario = _scenario("T17-01")

    def fake_subprocess_run(command: list[str], **_: object) -> SimpleNamespace:
        junit_argument = next(
            argument for argument in command if argument.startswith("--junitxml=")
        )
        junit_path = Path(junit_argument.removeprefix("--junitxml="))
        junit_path.write_text(
            '<testsuites><testsuite name="t17-fixed"><testcase '
            'name="forced_failure" time="0.01"><failure '
            'message="fixed assertion failed">expected fact missing</failure>'
            "</testcase></testsuite></testsuites>",
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=1, stdout="", stderr="forced failure")

    monkeypatch.setattr(runner, "T17_SCENARIOS", (scenario,))
    monkeypatch.setattr(runner.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(
        runner,
        "_parse_args",
        lambda: SimpleNamespace(output=output_path, list=False),
    )

    assert runner.main() == 1
    assert not output_path.exists()


def test_t17_junit_parser_aggregates_pass_fail_cases_and_observations(
    tmp_path: Path,
) -> None:
    xml_path = _write_junit(
        tmp_path / "T17-03.xml",
        scenario_id="T17-03",
        case_names=("ordered_stream", "terminal_is_unique"),
        failed_cases=frozenset({"terminal_is_unique"}),
        include_stream_observations=True,
    )

    result = parse_junit_suite(xml_path, _scenario("T17-03"))

    assert result.scenario_id == "T17-03"
    assert result.category == "current_turn_stream"
    assert result.case_count == 2
    assert result.passed == 1
    assert result.passed_cases == ["ordered_stream"]
    assert result.failed_cases == ["terminal_is_unique"]
    assert result.duration_ms == pytest.approx(30.0)
    assert result.observations == {
        "first_event_ms": 12.5,
        "first_token_ms": 34.75,
        "completion_ms": 78.25,
        "terminal_event": "message.completed",
        "model_calls": 1,
    }


def test_t17_report_summarizes_fixed_cases_security_and_raw_stream_timing(
    tmp_path: Path,
) -> None:
    results = []
    for scenario in T17_SCENARIOS:
        case_count = scenario.expected_case_count
        xml_path = _write_junit(
            tmp_path / f"{scenario.scenario_id}.xml",
            scenario_id=scenario.scenario_id,
            case_names=tuple(f"case-{index}" for index in range(1, case_count + 1)),
            include_stream_observations=scenario.scenario_id == "T17-03",
        )
        results.append(parse_junit_suite(xml_path, scenario))

    report = build_evaluation_report(
        results,
        git_revision="a45b5f7",
        command="pytest apps/api/tests/evals",
        adapter_mode="deterministic_offline",
    )
    payload = report.model_dump(mode="json")

    assert payload["schema_version"] == "accesspilot.eval.v1"
    assert payload["source"] == {
        "git_revision": "a45b5f7",
        "command": "pytest apps/api/tests/evals",
        "adapter_mode": "deterministic_offline",
    }
    assert payload["summary"]["scenarios_passed"] == 10
    assert payload["summary"]["scenarios_total"] == 10
    assert payload["summary"]["fixed_scenario_pass_rate"] == pytest.approx(1.0)
    expected_case_total = sum(scenario.expected_case_count for scenario in T17_SCENARIOS)
    assert payload["summary"]["cases_passed"] == expected_case_total
    assert payload["summary"]["cases_total"] == expected_case_total
    assert payload["summary"]["security_attack_cases"] == 4
    assert payload["summary"]["security_blocked_cases"] == 4
    assert payload["summary"]["security_block_rate"] == pytest.approx(1.0)
    assert payload["summary"]["latency_sample"] == {
        "first_event_ms": 12.5,
        "first_token_ms": 34.75,
        "completion_ms": 78.25,
        "terminal_event": "message.completed",
    }

    unmeasured = set(payload["summary"]["unmeasured_metrics"])
    assert {"provider_token_latency", "policy_recall_at_k", "production_sla"} <= unmeasured
    assert not any(
        key in payload["summary"]
        for key in ("p95_ms", "p95_latency_ms", "provider_token_latency_ms", "policy_recall_at_k")
    )


@pytest.mark.parametrize(
    "stream_observation_overrides",
    (
        {"first_event_ms": "40.0"},
        {"first_token_ms": "90.0"},
        {"completion_ms": "10.0"},
    ),
)
def test_t17_report_requires_ordered_latency_and_rejects_corrupt_junit(
    tmp_path: Path,
    stream_observation_overrides: dict[str, str],
) -> None:
    scenario = _scenario("T17-03")
    valid_result = parse_junit_suite(
        _write_junit(
            tmp_path / "T17-03-valid.xml",
            scenario_id=scenario.scenario_id,
            case_names=("ordered_stream",),
            include_stream_observations=True,
        ),
        scenario,
    )
    report = build_evaluation_report(
        [valid_result],
        git_revision="a45b5f7",
        command="pytest apps/api/tests/evals",
        adapter_mode="deterministic_offline",
    )
    latency_sample = report.summary.latency_sample
    assert latency_sample is not None
    assert latency_sample.first_token_ms is not None
    assert (
        latency_sample.first_event_ms
        <= latency_sample.first_token_ms
        <= latency_sample.completion_ms
    )

    corrupt_result = parse_junit_suite(
        _write_junit(
            tmp_path / "T17-03-corrupt.xml",
            scenario_id=scenario.scenario_id,
            case_names=("ordered_stream",),
            include_stream_observations=True,
            stream_observation_overrides=stream_observation_overrides,
        ),
        scenario,
    )
    with pytest.raises(ValueError, match="invalid latency observation order"):
        build_evaluation_report(
            [corrupt_result],
            git_revision="a45b5f7",
            command="pytest apps/api/tests/evals",
            adapter_mode="deterministic_offline",
        )


def test_t17_report_is_pydantic_json_roundtrip_and_has_no_private_evidence(
    tmp_path: Path,
) -> None:
    scenario = _scenario("T17-03")
    xml_path = _write_junit(
        tmp_path / "T17-03.xml",
        scenario_id=scenario.scenario_id,
        case_names=("ordered_stream",),
        include_stream_observations=True,
    )
    result = parse_junit_suite(xml_path, scenario)
    report = build_evaluation_report(
        [result],
        git_revision="a45b5f7",
        command="pytest apps/api/tests/evals",
        adapter_mode="deterministic_offline",
    )

    round_tripped = EvaluationReport.model_validate_json(report.model_dump_json())
    assert round_tripped == report

    encoded = json.dumps(report.model_dump(mode="json"), ensure_ascii=False)
    for forbidden in (
        "stdout",
        str(tmp_path),
        "ACCESSPILOT_TEST_DATABASE_URL",
        "http://",
        "https://",
        "sk-demo-secret",
        "api_key",
    ):
        assert forbidden not in encoded


def test_t17_junit_parser_removes_parameter_values_from_case_names(
    tmp_path: Path,
) -> None:
    scenario = _scenario("T17-10")
    xml_path = _write_junit(
        tmp_path / "T17-10.xml",
        scenario_id=scenario.scenario_id,
        case_names=(
            "test_compound_security[API_KEY=sk-demo-secret-123456]",
            "test_compound_security[employee_id=EMP-003]",
        ),
    )

    result = parse_junit_suite(xml_path, scenario)
    encoded = json.dumps(result.model_dump(mode="json"), ensure_ascii=False)

    assert result.passed_cases == [
        "test_compound_security",
        "test_compound_security#2",
    ]
    assert "sk-demo-secret" not in encoded
    assert "EMP-003" not in encoded


def test_t17_junit_parser_drops_unknown_properties_and_rejects_secret_metrics(
    tmp_path: Path,
) -> None:
    scenario = _scenario("T17-03")
    xml_path = tmp_path / "T17-03.xml"
    xml_path.write_text(
        """
        <testsuites><testsuite name="t17-fixed"><testcase name="safe" time="0.01">
          <properties>
            <property name="api_key" value="sk-secret-123456" />
            <property name="first_event_ms" value="7.5" />
          </properties>
        </testcase></testsuite></testsuites>
        """.strip(),
        encoding="utf-8",
    )

    result = parse_junit_suite(xml_path, scenario)

    assert result.observations == {"first_event_ms": 7.5}
    assert "sk-secret" not in result.model_dump_json()

    unsafe_path = tmp_path / "T17-03-unsafe.xml"
    unsafe_path.write_text(
        """
        <testsuites><testsuite name="t17-fixed"><testcase name="unsafe" time="0.01">
          <properties>
            <property name="first_event_ms" value="sk-secret-123456" />
          </properties>
        </testcase></testsuite></testsuites>
        """.strip(),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid observation"):
        parse_junit_suite(unsafe_path, scenario)


def test_t17_report_requires_exact_registered_case_count(tmp_path: Path) -> None:
    scenario = _scenario("T17-01")
    case_names = tuple(f"case-{index}" for index in range(1, scenario.expected_case_count + 2))
    result = parse_junit_suite(
        _write_junit(
            tmp_path / "T17-01.xml",
            scenario_id=scenario.scenario_id,
            case_names=case_names,
        ),
        scenario,
    )

    report = build_evaluation_report(
        [result],
        git_revision="a45b5f7",
        command="pytest apps/api/tests/evals",
        adapter_mode="deterministic_offline",
    )

    assert result.failed_cases == []
    assert report.summary.scenarios_passed == 0
