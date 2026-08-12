"""Deterministic product evaluation registry, SSE observations, and safe reports."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Literal, Self, TypeAlias, cast
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field, model_validator

EvaluationCategory = Literal[
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
    "numeric_context",
    "auth_session_isolation",
    "case_resource_acl",
]
JsonScalar: TypeAlias = str | int | float | bool | None
TerminalEvent = Literal[
    "message.completed",
    "error.recoverable",
    "turn.interrupted",
]

T17_BASELINE_GIT_REVISION = "c683d84"
T17_BASELINE_CASE_COUNT = 30

# T08 remains a historical twelve-case record at the v1.1 snapshot.  T19 can
# still run eight cases without changing their original meaning.  Three
# cross-role cases wait for shared Case/approval work in T20/T22, while the old
# Workspace-isolation case is superseded by T19's AuthSession evidence.
T08_BASELINE_GIT_REVISION = "c683d84"
T08_BASELINE_CASE_COUNT = 12
DEFERRED_T08_SELECTORS: tuple[str, ...] = (
    "apps/api/tests/evals/test_fixed_scenarios.py::"
    "test_eval_01_golden_path_creates_auditable_grant",
    "apps/api/tests/evals/test_fixed_scenarios.py::"
    "test_eval_05_data_owner_cannot_approve_out_of_order",
    "apps/api/tests/evals/test_fixed_scenarios.py::test_eval_06_rejection_is_terminal",
)
SUPERSEDED_T08_SELECTORS: tuple[str, ...] = (
    "apps/api/tests/evals/test_fixed_scenarios.py::"
    "test_eval_11_workspace_cannot_read_another_request",
)
CURRENT_T08_COMPATIBLE_SELECTORS: tuple[str, ...] = (
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


class EvaluationScenario(BaseModel):
    """One fixed, deterministic evaluation category and its pytest evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str = Field(pattern=r"^T\d{2}-\d{2}$")
    category: EvaluationCategory
    selectors: tuple[str, ...]
    expected_case_count: int = Field(ge=1)
    adapter_mode: Literal["deterministic_offline"] = "deterministic_offline"


def _scenario(
    scenario_id: str,
    category: EvaluationCategory,
    expected_case_count: int,
    *selectors: str,
) -> EvaluationScenario:
    return EvaluationScenario(
        scenario_id=scenario_id,
        category=category,
        selectors=selectors,
        expected_case_count=expected_case_count,
    )


T17_SCENARIOS: tuple[EvaluationScenario, ...] = (
    _scenario(
        "T17-01",
        "identity_demo",
        3,
        "apps/api/tests/api/test_demo_boundary.py::"
        "test_product_identity_ignores_legacy_workspace_actor_and_hides_controls",
        "apps/api/tests/api/test_demo_boundary.py::"
        "test_demo_session_requires_explicit_entry_and_exit_restores_product_identity",
        "apps/api/tests/api/test_demo_boundary.py::"
        "test_product_and_demo_facts_are_isolated_across_cookie_switches",
    ),
    _scenario(
        "T17-02",
        "resolution",
        4,
        "apps/api/tests/tools/test_entitlement_resolution.py::"
        "test_resolution_matches_code_name_and_controlled_alias",
        "apps/api/tests/tools/test_entitlement_resolution.py::"
        "test_system_name_can_return_typed_ambiguous_candidates_without_draft_mutation",
        "apps/api/tests/tools/test_entitlement_resolution.py::"
        "test_no_match_returns_current_eligible_access_and_never_fuzzy_matches",
        "apps/api/tests/api/test_t16_business_facts.py::"
        "test_selected_entitlement_is_revalidated_and_invalidates_old_confirmation",
    ),
    _scenario(
        "T17-03",
        "current_turn_stream",
        2,
        "apps/api/tests/api/test_chat_stream.py::"
        "test_current_turn_sse_emits_ordered_real_deltas_and_persists_before_completed",
        "apps/api/tests/evals/test_t17_stream_measurement.py::"
        "test_direct_asgi_stream_records_observed_latency_and_model_calls",
    ),
    _scenario(
        "T17-04",
        "workspace_reconnect",
        2,
        "apps/api/tests/api/test_events.py::"
        "test_sse_reconnect_only_replays_events_after_last_event_id",
        "apps/api/tests/evals/test_t17_stream_measurement.py::"
        "test_reconnect_observation_has_no_duplicate_persisted_event_ids",
    ),
    _scenario(
        "T17-05",
        "turn_interrupted",
        2,
        "apps/api/tests/api/test_chat_stream.py::"
        "test_current_turn_cancellation_persists_interrupted_without_completed",
        "apps/api/tests/events/test_t14_contract.py::"
        "test_terminal_event_is_compare_and_set_and_second_terminal_is_a_noop",
    ),
    _scenario(
        "T17-06",
        "policy_states",
        4,
        "apps/api/tests/rag/test_t15_policy_service.py::"
        "test_policy_catalog_returns_all_eight_fact_source_records",
        "apps/api/tests/rag/test_t15_policy_service.py::"
        "test_policy_query_is_grounded_only_in_current_matches",
        "apps/api/tests/rag/test_t15_policy_service.py::"
        "test_low_similarity_returns_insufficient_without_policy_body",
        "apps/api/tests/rag/test_t15_policy_service.py::"
        "test_partial_index_maps_to_retrieval_unavailable",
    ),
    _scenario(
        "T17-07",
        "self_approval",
        4,
        "apps/api/tests/conversation/test_t15_policy_conversation.py::"
        "test_self_approval_conversation_is_explicitly_forbidden",
    ),
    _scenario(
        "T17-08",
        "budget_degradation",
        2,
        "apps/api/tests/evals/test_t17_productized_behaviors.py::"
        "test_budget_exhaustion_keeps_read_only_facts_and_confirmed_submit_available",
        "apps/api/tests/conversation/test_service.py::"
        "test_access_consultation_uses_read_only_tool_without_mutating_draft_or_quota",
    ),
    _scenario(
        "T17-09",
        "draft_isolation",
        3,
        "apps/api/tests/api/test_demo_boundary.py::"
        "test_other_demo_identity_draft_is_not_exposed_or_rebound",
        "apps/api/tests/api/test_demo_boundary.py::"
        "test_product_draft_is_hidden_during_demo_and_restored_after_exit",
        "apps/api/tests/evals/test_fixed_scenarios.py::"
        "test_eval_11_workspace_cannot_read_another_request",
    ),
    _scenario(
        "T17-10",
        "compound_security",
        4,
        "apps/api/tests/conversation/test_t15_policy_conversation.py::"
        "test_compound_security_inputs_preserve_business_boundary",
    ),
)

# T17 is immutable v1.1 evidence.  T19 permanently closes the Demo/anonymous
# identity surface, so those historical selectors remain recorded but must not
# be collected as current proof or silently replaced with new 404 assertions.
SUPERSEDED_T17_SCENARIO_IDS = frozenset({"T17-01", "T17-09"})
ACTIVE_T17_SCENARIOS: tuple[EvaluationScenario, ...] = tuple(
    scenario
    for scenario in T17_SCENARIOS
    if scenario.scenario_id not in SUPERSEDED_T17_SCENARIO_IDS
)


T18_SCENARIOS: tuple[EvaluationScenario, ...] = (
    _scenario(
        "T18-01",
        "numeric_context",
        11,
        "apps/api/tests/conversation/test_t18_cursor.py::"
        "test_duration_cursor_accepts_111_up_to_catalog_limit_without_model",
        "apps/api/tests/conversation/test_t18_cursor.py::"
        "test_duration_cursor_rejects_catalog_over_limit_without_mutation",
        "apps/api/tests/conversation/test_t18_cursor.py::"
        "test_numeric_follow_up_respects_non_duration_cursor",
        "apps/api/tests/conversation/test_t18_cursor.py::"
        "test_numeric_message_without_active_cursor_needs_clarification_without_model_call",
        "apps/api/tests/conversation/test_t18_cursor.py::"
        "test_help_has_priority_and_clears_active_cursor",
        "apps/api/tests/conversation/test_t18_cursor.py::"
        "test_invalid_duration_keeps_cursor_and_revision",
    ),
)


T19_SCENARIOS: tuple[EvaluationScenario, ...] = (
    _scenario(
        "T19-01",
        "auth_session_isolation",
        23,
        "apps/api/tests/api/test_t19_auth.py",
        "apps/api/tests/db/test_t19_migration.py",
    ),
)


T20_SCENARIOS: tuple[EvaluationScenario, ...] = (
    _scenario(
        "T20-01",
        "case_resource_acl",
        14,
        "apps/api/tests/api/test_t20_case_acl.py",
    ),
)


# PRODUCT_SCENARIOS is current compatible/new proof.  The full T17_SCENARIOS
# tuple above is historical v1.1 metadata and therefore has a different
# denominator; T19 evidence is counted only in T19_SCENARIOS.
PRODUCT_SCENARIOS: tuple[EvaluationScenario, ...] = (
    ACTIVE_T17_SCENARIOS + T18_SCENARIOS + T19_SCENARIOS + T20_SCENARIOS
)


class ScenarioResult(BaseModel):
    """Redacted facts extracted from one scenario's JUnit file."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    category: EvaluationCategory
    expected_case_count: int
    case_count: int = Field(ge=0)
    passed: int = Field(ge=0)
    passed_cases: list[str]
    failed_cases: list[str]
    duration_ms: float = Field(ge=0)
    observations: dict[str, JsonScalar]


class EvaluationSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    git_revision: str
    command: str
    adapter_mode: str


class SseLatencyObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    first_event_ms: float = Field(ge=0)
    first_token_ms: float | None = Field(default=None, ge=0)
    completion_ms: float = Field(ge=0)
    terminal_event: TerminalEvent

    @model_validator(mode="after")
    def validate_observed_order(self) -> Self:
        if self.first_event_ms > self.completion_ms:
            raise ValueError("invalid latency observation order")
        if self.first_token_ms is not None and not (
            self.first_event_ms <= self.first_token_ms <= self.completion_ms
        ):
            raise ValueError("invalid latency observation order")
        return self


class EvaluationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenarios_passed: int = Field(ge=0)
    scenarios_total: int = Field(ge=0)
    fixed_scenario_pass_rate: float = Field(ge=0, le=1)
    cases_passed: int = Field(ge=0)
    cases_total: int = Field(ge=0)
    case_pass_rate: float = Field(ge=0, le=1)
    security_attack_cases: int = Field(ge=0)
    security_blocked_cases: int = Field(ge=0)
    security_block_rate: float = Field(ge=0, le=1)
    latency_sample: SseLatencyObservation | None
    charged_model_calls: int | None = Field(default=None, ge=0)
    reconnect_replayed_events: int | None = Field(default=None, ge=0)
    reconnect_duplicate_count: int | None = Field(default=None, ge=0)
    reconnect_duplicate_rate: float | None = Field(default=None, ge=0, le=1)
    unmeasured_metrics: list[str]


class EvaluationReport(BaseModel):
    """Portable evidence report containing no request text or raw command output."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["accesspilot.eval.v1"] = "accesspilot.eval.v1"
    source: EvaluationSource
    scenarios: list[ScenarioResult]
    summary: EvaluationSummary


_FLOAT_OBSERVATIONS = frozenset({"first_event_ms", "first_token_ms", "completion_ms"})
_INTEGER_OBSERVATIONS = frozenset(
    {"model_calls", "reconnect_replayed_events", "reconnect_duplicate_count"}
)
_STRING_OBSERVATIONS: dict[str, frozenset[str]] = {
    "terminal_event": frozenset({"message.completed", "error.recoverable", "turn.interrupted"}),
    "adapter_mode": frozenset({"deterministic_offline"}),
}
_OBSERVATION_NAMES = _FLOAT_OBSERVATIONS | _INTEGER_OBSERVATIONS | frozenset(_STRING_OBSERVATIONS)


def _json_scalar(raw_value: str) -> JsonScalar:
    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError:
        return raw_value
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return raw_value


def _safe_observation(name: str, raw_value: str) -> JsonScalar:
    """Parse one explicitly allowlisted, non-secret evaluation observation."""

    if name == "first_token_ms" and raw_value in {"None", "null"}:
        return None
    parsed = _json_scalar(raw_value)
    if name in _FLOAT_OBSERVATIONS:
        if (
            isinstance(parsed, bool)
            or not isinstance(parsed, int | float)
            or not math.isfinite(parsed)
            or parsed < 0
        ):
            raise ValueError(f"invalid observation: {name}")
        return parsed
    if name in _INTEGER_OBSERVATIONS:
        if isinstance(parsed, bool) or not isinstance(parsed, int) or parsed < 0:
            raise ValueError(f"invalid observation: {name}")
        return parsed
    allowed_values = _STRING_OBSERVATIONS.get(name)
    if allowed_values is None or not isinstance(parsed, str) or parsed not in allowed_values:
        raise ValueError(f"invalid observation: {name}")
    return parsed


def parse_junit_suite(
    xml_path: str | Path,
    scenario: EvaluationScenario,
) -> ScenarioResult:
    """Parse only test names, status, duration, and allowlisted scalar properties."""

    root = ElementTree.parse(xml_path).getroot()
    passed_cases: list[str] = []
    failed_cases: list[str] = []
    observations: dict[str, JsonScalar] = {}
    duration_seconds = 0.0
    testcases = list(root.iter("testcase"))
    case_name_counts: dict[str, int] = {}
    for testcase in testcases:
        raw_case_name = testcase.attrib.get("name", "unnamed-case")
        base_case_name = raw_case_name.split("[", 1)[0]
        occurrence = case_name_counts.get(base_case_name, 0) + 1
        case_name_counts[base_case_name] = occurrence
        case_name = base_case_name if occurrence == 1 else f"{base_case_name}#{occurrence}"
        duration_seconds += max(0.0, float(testcase.attrib.get("time", "0")))
        failed = any(testcase.find(tag) is not None for tag in ("failure", "error", "skipped"))
        (failed_cases if failed else passed_cases).append(case_name)
        properties = testcase.find("properties")
        if properties is None:
            continue
        for prop in properties.findall("property"):
            name = prop.attrib.get("name")
            value = prop.attrib.get("value")
            if name is None or value is None:
                continue
            if name not in _OBSERVATION_NAMES:
                continue
            parsed = _safe_observation(name, value)
            if name in observations and observations[name] != parsed:
                raise ValueError(f"conflicting observation: {name}")
            observations[name] = parsed
    return ScenarioResult(
        scenario_id=scenario.scenario_id,
        category=scenario.category,
        expected_case_count=scenario.expected_case_count,
        case_count=len(testcases),
        passed=len(passed_cases),
        passed_cases=passed_cases,
        failed_cases=failed_cases,
        duration_ms=duration_seconds * 1000,
        observations=observations,
    )


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _first_observation(
    results: list[ScenarioResult],
    category: EvaluationCategory,
) -> dict[str, JsonScalar]:
    for result in results:
        if result.category == category:
            return result.observations
    return {}


def _number(
    observations: dict[str, JsonScalar],
    name: str,
) -> float | None:
    value = observations.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _integer(
    observations: dict[str, JsonScalar],
    name: str,
) -> int | None:
    value = observations.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def build_evaluation_report(
    results: list[ScenarioResult],
    *,
    git_revision: str,
    command: str,
    adapter_mode: str,
) -> EvaluationReport:
    """Aggregate observed facts without inventing unavailable provider metrics."""

    scenario_passed = sum(
        not result.failed_cases and result.case_count == result.expected_case_count
        for result in results
    )
    cases_total = sum(result.case_count for result in results)
    cases_passed = sum(result.passed for result in results)
    security = next(
        (result for result in results if result.category == "compound_security"),
        None,
    )
    attack_cases = security.case_count if security is not None else 0
    blocked_cases = security.passed if security is not None else 0

    stream_observations = _first_observation(results, "current_turn_stream")
    first_event_ms = _number(stream_observations, "first_event_ms")
    completion_ms = _number(stream_observations, "completion_ms")
    terminal = stream_observations.get("terminal_event")
    latency_sample = None
    if (
        first_event_ms is not None
        and completion_ms is not None
        and terminal in {"message.completed", "error.recoverable", "turn.interrupted"}
    ):
        latency_sample = SseLatencyObservation(
            first_event_ms=first_event_ms,
            first_token_ms=_number(stream_observations, "first_token_ms"),
            completion_ms=completion_ms,
            terminal_event=terminal,
        )

    reconnect = _first_observation(results, "workspace_reconnect")
    replayed_events = _integer(reconnect, "reconnect_replayed_events")
    duplicate_count = _integer(reconnect, "reconnect_duplicate_count")
    duplicate_rate = None
    if replayed_events is not None and duplicate_count is not None:
        duplicate_rate = _rate(duplicate_count, replayed_events)

    return EvaluationReport(
        source=EvaluationSource(
            git_revision=git_revision,
            command=command,
            adapter_mode=adapter_mode,
        ),
        scenarios=results,
        summary=EvaluationSummary(
            scenarios_passed=scenario_passed,
            scenarios_total=len(results),
            fixed_scenario_pass_rate=_rate(scenario_passed, len(results)),
            cases_passed=cases_passed,
            cases_total=cases_total,
            case_pass_rate=_rate(cases_passed, cases_total),
            security_attack_cases=attack_cases,
            security_blocked_cases=blocked_cases,
            security_block_rate=_rate(blocked_cases, attack_cases),
            latency_sample=latency_sample,
            charged_model_calls=_integer(stream_observations, "model_calls"),
            reconnect_replayed_events=replayed_events,
            reconnect_duplicate_count=duplicate_count,
            reconnect_duplicate_rate=duplicate_rate,
            unmeasured_metrics=[
                "provider_token_latency",
                "policy_recall_at_k",
                "production_sla",
            ],
        ),
    )


class SseLatencyResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    first_event_ms: float
    first_token_ms: float | None
    completion_ms: float
    terminal_event: TerminalEvent
    event_count: int = Field(ge=1)


class SseLatencyRecorder:
    """Incrementally observe SSE delivery at an ASGI send boundary."""

    _TERMINALS = frozenset({"message.completed", "error.recoverable", "turn.interrupted"})
    _EVENT_TYPES = frozenset(
        {
            "turn.started",
            "intent.detected",
            "tool.started",
            "tool.completed",
            "draft.updated",
            "business.status",
            "message.delta",
            "message.completed",
            "error.recoverable",
            "turn.interrupted",
        }
    )

    def __init__(self, *, start_ns: int) -> None:
        self._start_ns = start_ns
        self._buffer = b""
        self._first_event_ns: int | None = None
        self._first_token_ns: int | None = None
        self._completion_ns: int | None = None
        self._terminal_event: TerminalEvent | None = None
        self._turn_id: str | None = None
        self._last_seq = 0
        self._last_observed_ns = start_ns
        self._event_count = 0

    def feed(self, chunk: bytes, *, observed_ns: int) -> None:
        if isinstance(observed_ns, bool) or not isinstance(observed_ns, int):
            raise TypeError("observed time must be an integer monotonic timestamp")
        if observed_ns < self._last_observed_ns:
            raise ValueError("observed time must be monotonic")
        if not isinstance(chunk, bytes):
            raise TypeError("SSE chunks must be bytes")
        self._last_observed_ns = observed_ns
        self._buffer += chunk
        normalized = self._buffer.replace(b"\r\n", b"\n")
        while b"\n\n" in normalized:
            raw_frame, normalized = normalized.split(b"\n\n", 1)
            self._consume_frame(raw_frame, observed_ns=observed_ns)
        self._buffer = normalized

    def _consume_frame(self, raw_frame: bytes, *, observed_ns: int) -> None:
        stripped = raw_frame.strip()
        if not stripped or stripped.startswith(b":"):
            return
        if self._terminal_event is not None:
            raise ValueError("event observed after terminal")
        event_type: str | None = None
        data_lines: list[bytes] = []
        for line in stripped.splitlines():
            if line.startswith(b"event:"):
                event_type = line.partition(b":")[2].strip().decode("utf-8")
            elif line.startswith(b"data:"):
                data_lines.append(line.partition(b":")[2].lstrip())
        if event_type is None or not data_lines:
            raise ValueError("malformed SSE frame")
        if event_type not in self._EVENT_TYPES:
            raise ValueError("unknown SSE event type")
        try:
            envelope = json.loads(b"\n".join(data_lines))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("malformed SSE data") from error
        if not isinstance(envelope, dict):
            raise ValueError("SSE envelope must be an object")
        turn_id = envelope.get("turn_id")
        seq = envelope.get("seq")
        payload = envelope.get("payload")
        if not isinstance(turn_id, str) or isinstance(seq, bool) or not isinstance(seq, int):
            raise ValueError("SSE envelope identity is invalid")
        if not isinstance(payload, dict):
            raise ValueError("SSE payload must be an object")
        if self._event_count == 0 and event_type != "turn.started":
            raise ValueError("turn.started must be the first event")
        if self._turn_id is None:
            self._turn_id = turn_id
        if turn_id != self._turn_id or seq != self._last_seq + 1:
            raise ValueError("SSE turn or sequence is invalid")

        self._last_seq = seq
        self._event_count += 1
        if self._first_event_ns is None:
            self._first_event_ns = observed_ns
        if event_type == "message.delta":
            text = payload.get("text")
            if not isinstance(text, str):
                raise ValueError("message.delta text is invalid")
            if text and self._first_token_ns is None:
                self._first_token_ns = observed_ns
        if event_type in self._TERMINALS:
            self._terminal_event = cast(TerminalEvent, event_type)
            self._completion_ns = observed_ns

    def finish(self) -> SseLatencyResult:
        if self._buffer.strip():
            raise ValueError("incomplete SSE frame")
        if (
            self._first_event_ns is None
            or self._completion_ns is None
            or self._terminal_event is None
        ):
            raise ValueError("SSE stream has no complete terminal contract")
        return SseLatencyResult(
            first_event_ms=(self._first_event_ns - self._start_ns) / 1_000_000,
            first_token_ms=(
                None
                if self._first_token_ns is None
                else (self._first_token_ns - self._start_ns) / 1_000_000
            ),
            completion_ms=(self._completion_ns - self._start_ns) / 1_000_000,
            terminal_event=self._terminal_event,
            event_count=self._event_count,
        )
