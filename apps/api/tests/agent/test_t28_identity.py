"""T28 deterministic identity contract."""

from inspect import signature
from uuid import UUID

import pytest

from accesspilot.agent.identity import (
    canonical_json_array,
    confirm_operation_id,
    event_key,
    model_attempt_to_event_ordinal,
    operation_id,
    step_id,
    tool_call_id,
)

WORKSPACE_ID = UUID("12345678-1234-5678-1234-567812345678")
GRAPH_RUN_ID = UUID("87654321-4321-8765-4321-876543218765")
PENDING_INPUT_ID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


def test_canonical_json_array_is_ascii_compact_and_normalizes_uuid() -> None:
    assert canonical_json_array(
        ["identity-中文", UUID("AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"), 7]
    ) == (
        b'["identity-\\u4e2d\\u6587",'
        b'"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",7]'
    )


@pytest.mark.parametrize("invalid", [None, 1.5, True, ["nested"], {"nested": "value"}])
def test_canonical_json_array_rejects_non_contract_values(invalid: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        canonical_json_array(["valid", invalid])  # type: ignore[list-item]

    with pytest.raises((TypeError, ValueError)):
        canonical_json_array("not-an-array")  # type: ignore[arg-type]


def test_t28_identity_golden_vectors() -> None:
    assert operation_id(
        workspace_id=WORKSPACE_ID,
        graph_run_id=GRAPH_RUN_ID,
        input_seq=7,
        step_key="hydrate_authoritative_snapshot",
    ) == "op_5f272e1f732b065ceab412aedae7a8bb18365424502b616d98fe0e2533176a89"
    assert confirm_operation_id(
        workspace_id=WORKSPACE_ID,
        pending_input_id=PENDING_INPUT_ID,
    ) == "op_0692efb2ee89b90d4f61c70f5238408d8ab552df58185710473fb3599df692b7"
    assert step_id(
        workspace_id=WORKSPACE_ID,
        graph_run_id=GRAPH_RUN_ID,
        input_seq=7,
        step_key="hydrate_authoritative_snapshot",
    ) == "stp_c676db198891d89a0c75606140138a3d"
    assert event_key(
        workspace_id=WORKSPACE_ID,
        graph_run_id=GRAPH_RUN_ID,
        input_seq=7,
        step_key="model:parse_input",
        lifecycle_phase="completed",
        ordinal=2,
    ) == "evt_a0caff3f00cf4036fadafb9e07d41cf019dab9a443f7b5d9aaa4712c9dfa9f1e"
    assert tool_call_id(
        workspace_id=WORKSPACE_ID,
        graph_run_id=GRAPH_RUN_ID,
        input_seq=7,
        tool_step_key="tool:search_entitlements",
    ) == "tool_9a5b8f0d25e42f1bdbaa229a56deaa3e"


def test_model_attempt_maps_to_stable_zero_based_event_ordinal() -> None:
    assert model_attempt_to_event_ordinal(1) == 0
    assert model_attempt_to_event_ordinal(3) == 2
    for invalid in (0, -1, 1.5, True):
        with pytest.raises(ValueError, match="attempt"):
            model_attempt_to_event_ordinal(invalid)  # type: ignore[arg-type]


def test_stable_identity_has_no_execution_attempt_or_fence_input() -> None:
    first = event_key(
        workspace_id=WORKSPACE_ID,
        graph_run_id=GRAPH_RUN_ID,
        input_seq=7,
        step_key="tool:lookup",
        lifecycle_phase="started",
        ordinal=0,
    )
    replay = event_key(
        workspace_id=WORKSPACE_ID,
        graph_run_id=GRAPH_RUN_ID,
        input_seq=7,
        step_key="tool:lookup",
        lifecycle_phase="started",
        ordinal=0,
    )

    assert replay == first
    for function in (operation_id, confirm_operation_id, step_id, event_key, tool_call_id):
        parameters = signature(function).parameters
        assert "attempt" not in parameters
        assert "lease_fence" not in parameters
        assert "execution_id" not in parameters


def test_identity_rejects_whitespace_only_step_keys() -> None:
    with pytest.raises(ValueError, match="step_key"):
        operation_id(
            workspace_id=WORKSPACE_ID,
            graph_run_id=GRAPH_RUN_ID,
            input_seq=7,
            step_key="   ",
        )


@pytest.mark.parametrize("phase", ["unknown", "", "STARTED"])
def test_event_key_rejects_unknown_lifecycle_phase(phase: str) -> None:
    with pytest.raises(ValueError, match="lifecycle_phase"):
        event_key(
            workspace_id=WORKSPACE_ID,
            graph_run_id=GRAPH_RUN_ID,
            input_seq=7,
            step_key="route_intent",
            lifecycle_phase=phase,
            ordinal=0,
        )


def test_identity_rejects_negative_sequence_and_ordinal() -> None:
    with pytest.raises(ValueError, match="input_seq"):
        step_id(
            workspace_id=WORKSPACE_ID,
            graph_run_id=GRAPH_RUN_ID,
            input_seq=-1,
            step_key="route_intent",
        )
    with pytest.raises(ValueError, match="ordinal"):
        event_key(
            workspace_id=WORKSPACE_ID,
            graph_run_id=GRAPH_RUN_ID,
            input_seq=0,
            step_key="route_intent",
            lifecycle_phase="selected",
            ordinal=-1,
        )
