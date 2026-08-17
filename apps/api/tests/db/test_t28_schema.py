"""T28 application-owned runtime schema contract."""

from importlib import import_module

import pytest
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, Index, UniqueConstraint

from accesspilot.db.base import Base
from accesspilot.main import ChatMessageBody, LoginBody
from accesspilot.workspaces import Workspace


def _tables():  # type: ignore[no-untyped-def]
    import_module("accesspilot.db.models")
    return Base.metadata.tables


def _check_sql(table_name: str) -> set[str]:
    return {
        str(constraint.sqltext)
        for constraint in _tables()[table_name].constraints
        if isinstance(constraint, CheckConstraint)
    }


def _unique_columns(table_name: str) -> set[tuple[str, ...]]:
    return {
        tuple(constraint.columns.keys())
        for constraint in _tables()[table_name].constraints
        if isinstance(constraint, UniqueConstraint)
    }


def _partial_unique_indexes(table_name: str) -> dict[tuple[str, ...], str]:
    result: dict[tuple[str, ...], str] = {}
    for index in _tables()[table_name].indexes:
        if not isinstance(index, Index) or not index.unique:
            continue
        predicate = index.dialect_options["postgresql"].get("where")
        if predicate is not None:
            result[tuple(column.name for column in index.columns)] = str(predicate)
    return result


def test_workspace_has_private_thread_flow_and_monotonic_fence_facts() -> None:
    workspace = _tables()["workspaces"]

    assert {
        "agent_thread_id",
        "flow_version",
        "lease_fence",
    } <= set(workspace.c.keys())
    assert workspace.c.agent_thread_id.nullable is False
    assert workspace.c.agent_thread_id.unique is True
    assert workspace.c.flow_version.nullable is False
    assert workspace.c.flow_version.server_default is not None
    assert workspace.c.lease_fence.nullable is False
    assert workspace.c.lease_fence.server_default is not None
    checks = _check_sql("workspaces")
    assert "flow_version IN (1, 2)" in checks
    assert "lease_fence >= 0" in checks


def test_internal_workspace_runtime_fields_are_not_client_dto_fields() -> None:
    internal = {"agent_thread_id", "flow_version", "lease_fence"}

    assert internal.isdisjoint(Workspace.__dataclass_fields__)
    for dto, payload in (
        (LoginBody, {"account_id": "EMP-001"}),
        (ChatMessageBody, {"content": "查询权限"}),
    ):
        for field in internal:
            with pytest.raises(ValidationError):
                dto.model_validate({**payload, field: "client-value"})


def test_execution_record_has_complete_locator_uniques_and_status_checks() -> None:
    execution = _tables()["agent_turn_executions"]

    assert {
        "id",
        "workspace_id",
        "graph_run_id",
        "checkpoint_thread_id",
        "input_seq",
        "input_turn_id",
        "input_event_id",
        "auth_session_ref",
        "actor_id",
        "engine",
        "attempt",
        "lease_fence",
        "lease_expires_at",
        "status",
        "checkpoint_ns",
        "accepted_checkpoint_id",
        "terminal_event_id",
        "created_at",
        "updated_at",
    } == set(execution.c.keys())
    uniques = _unique_columns("agent_turn_executions")
    assert ("workspace_id", "graph_run_id", "input_seq") in uniques
    assert ("input_turn_id",) in uniques
    assert ("input_event_id",) in uniques
    assert ("terminal_event_id",) in uniques
    partials = _partial_unique_indexes("agent_turn_executions")
    assert partials[("workspace_id",)] == "status = 'running'"
    checks = _check_sql("agent_turn_executions")
    assert "checkpoint_ns = ''" in checks
    assert (
        "checkpoint_thread_id = 'accesspilot:v1.3:' || graph_run_id::text"
        in checks
    )
    assert "input_seq >= 0" in checks
    assert "attempt >= 1" in checks
    assert "lease_fence >= 1" in checks
    assert any(
        all(status in check for status in (
            "running",
            "waiting_input",
            "completed",
            "recoverable_error",
            "interrupted",
        ))
        for check in checks
    )
    assert any(
        "status = 'running'" in check
        and "lease_expires_at IS NOT NULL" in check
        and "terminal_event_id IS NULL" in check
        and "status <> 'running'" in check
        and "lease_expires_at IS NULL" in check
        and "terminal_event_id IS NOT NULL" in check
        for check in checks
    )


def test_pending_record_has_live_partial_unique_and_retirement_checks() -> None:
    pending = _tables()["agent_pending_inputs"]

    assert {
        "id",
        "workspace_id",
        "agent_thread_id",
        "graph_run_id",
        "checkpoint_thread_id",
        "pending_input_id",
        "kind",
        "draft_revision",
        "auth_session_ref",
        "actor_id",
        "engine",
        "checkpoint_ns",
        "accepted_checkpoint_id",
        "status",
        "resume_input_seq",
        "retired_at",
        "retirement_reason",
        "created_at",
        "updated_at",
    } == set(pending.c.keys())
    assert ("pending_input_id",) in _unique_columns("agent_pending_inputs")
    partials = _partial_unique_indexes("agent_pending_inputs")
    assert partials[("workspace_id",)] == "status IN ('active', 'resuming')"
    checks = _check_sql("agent_pending_inputs")
    assert "checkpoint_ns = ''" in checks
    assert (
        "checkpoint_thread_id = 'accesspilot:v1.3:' || graph_run_id::text"
        in checks
    )
    assert "kind = 'confirmation'" in checks
    assert "draft_revision >= 0" in checks
    assert any(
        all(status in check for status in (
            "active",
            "resuming",
            "resolved",
            "abandoned_to_legacy",
            "abandoned_conflict",
        ))
        for check in checks
    )
    assert any(
        "abandoned_to_legacy" in check
        and "abandoned_conflict" in check
        and "retired_at IS NOT NULL" in check
        and "retirement_reason IS NOT NULL" in check
        for check in checks
    )


def test_step_execution_fact_is_unique_per_workspace_operation() -> None:
    step = _tables()["agent_step_executions"]

    assert {
        "id",
        "workspace_id",
        "graph_run_id",
        "input_seq",
        "step_key",
        "operation_id",
        "status",
        "result_reference",
        "committed_revision",
        "created_at",
        "completed_at",
    } == set(step.c.keys())
    assert ("workspace_id", "operation_id") in _unique_columns(
        "agent_step_executions"
    )
    checks = _check_sql("agent_step_executions")
    assert "input_seq >= 0" in checks
    assert "committed_revision IS NULL OR committed_revision >= 0" in checks
    assert any("reserved" in check and "completed" in check for check in checks)
    assert any(
        "status = 'reserved'" in check
        and "completed_at IS NULL" in check
        and "status = 'completed'" in check
        and "completed_at IS NOT NULL" in check
        and "NULLIF(btrim(result_reference), '') IS NOT NULL" in check
        and "committed_revision IS NOT NULL" in check
        for check in checks
    )


def test_workspace_event_key_is_nullable_and_partial_unique() -> None:
    event = _tables()["workspace_events"]

    assert event.c.event_key.nullable is True
    partials = _partial_unique_indexes("workspace_events")
    assert partials[("workspace_id", "event_key")] == "event_key IS NOT NULL"
