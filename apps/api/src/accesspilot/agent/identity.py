"""Versioned deterministic identities for replay-safe agent facts.

Only stable logical-input coordinates belong in these tuples.  Execution
attempts, lease fences, HTTP retries and random per-attempt identifiers are
intentionally absent from every public function signature in this module.
"""

import json
from collections.abc import Sequence
from hashlib import sha256
from typing import TypeAlias
from uuid import UUID

CanonicalValue: TypeAlias = str | int | UUID

LIFECYCLE_PHASES = frozenset(
    {
        "started",
        "completed",
        "selected",
        "required",
        "resumed",
        "updated",
        "status",
        "terminal",
    }
)


def canonical_json_array(values: Sequence[CanonicalValue]) -> bytes:
    """Encode one flat canonical tuple as compact, ASCII JSON UTF-8 bytes."""

    if isinstance(values, (str, bytes, bytearray)):
        raise TypeError("canonical identity must be a JSON array")
    normalized: list[str | int] = []
    for value in values:
        if isinstance(value, UUID):
            normalized.append(str(value))
        elif isinstance(value, str):
            normalized.append(value)
        elif type(value) is int:
            normalized.append(value)
        else:
            raise TypeError("canonical identity values must be strings, UUIDs, or integers")
    return json.dumps(
        normalized,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _uuid(value: UUID | str, *, label: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError(f"{label} must be a UUID") from error


def _non_negative(value: int, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _key(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _digest(values: Sequence[CanonicalValue]) -> str:
    return sha256(canonical_json_array(values)).hexdigest()


def operation_id(
    *,
    workspace_id: UUID | str,
    graph_run_id: UUID | str,
    input_seq: int,
    step_key: str,
) -> str:
    """Return the stable identity for one ordinary side-effecting step."""

    digest = _digest(
        [
            "accesspilot-operation-v1",
            _uuid(workspace_id, label="workspace_id"),
            _uuid(graph_run_id, label="graph_run_id"),
            _non_negative(input_seq, label="input_seq"),
            _key(step_key, label="step_key"),
        ]
    )
    return f"op_{digest}"


def confirm_operation_id(
    *,
    workspace_id: UUID | str,
    pending_input_id: UUID | str,
) -> str:
    """Return the cross-resume identity for the one confirmation CAS."""

    digest = _digest(
        [
            "accesspilot-confirm-v1",
            _uuid(workspace_id, label="workspace_id"),
            _uuid(pending_input_id, label="pending_input_id"),
            "apply_confirmation",
        ]
    )
    return f"op_{digest}"


def step_id(
    *,
    workspace_id: UUID | str,
    graph_run_id: UUID | str,
    input_seq: int,
    step_key: str,
) -> str:
    """Return the compact public identity for one logical graph step."""

    digest = _digest(
        [
            "accesspilot-step-v1",
            _uuid(workspace_id, label="workspace_id"),
            _uuid(graph_run_id, label="graph_run_id"),
            _non_negative(input_seq, label="input_seq"),
            _key(step_key, label="step_key"),
        ]
    )
    return f"stp_{digest[:32]}"


def event_key(
    *,
    workspace_id: UUID | str,
    graph_run_id: UUID | str,
    input_seq: int,
    step_key: str,
    lifecycle_phase: str,
    ordinal: int,
) -> str:
    """Return the replay-deduplication key for one safe public event."""

    if lifecycle_phase not in LIFECYCLE_PHASES:
        raise ValueError("lifecycle_phase is not part of the v1 identity contract")
    digest = _digest(
        [
            "accesspilot-event-v1",
            _uuid(workspace_id, label="workspace_id"),
            _uuid(graph_run_id, label="graph_run_id"),
            _non_negative(input_seq, label="input_seq"),
            _key(step_key, label="step_key"),
            lifecycle_phase,
            _non_negative(ordinal, label="ordinal"),
        ]
    )
    return f"evt_{digest}"


def tool_call_id(
    *,
    workspace_id: UUID | str,
    graph_run_id: UUID | str,
    input_seq: int,
    tool_step_key: str,
) -> str:
    """Return the compact identity shared by tool started/completed events."""

    digest = _digest(
        [
            "accesspilot-tool-v1",
            _uuid(workspace_id, label="workspace_id"),
            _uuid(graph_run_id, label="graph_run_id"),
            _non_negative(input_seq, label="input_seq"),
            _key(tool_step_key, label="tool_step_key"),
        ]
    )
    return f"tool_{digest[:32]}"


def model_attempt_to_event_ordinal(attempt: int) -> int:
    """Map the model payload's one-based attempt to event identity ordinal."""

    if type(attempt) is not int or attempt < 1:
        raise ValueError("model attempt must be an integer starting at 1")
    return attempt - 1
