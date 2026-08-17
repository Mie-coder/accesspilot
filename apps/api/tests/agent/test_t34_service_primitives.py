"""T34 service primitives: confirmation operation and interrupt event contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.identity import confirm_operation_id
from accesspilot.agent.step_operations import (
    AgentStepContext,
    AgentStepOperationService,
    CompletedStepOperation,
)
from accesspilot.db.models import (
    AgentPendingInputRecord,
    AgentStepExecutionRecord,
    AgentTurnExecutionRecord,
    AuthSessionRecord,
    WorkspaceEventRecord,
    WorkspaceRecord,
)
from accesspilot.db.seed import seed_catalog
from accesspilot.events import (
    UnsafeEventError,
    validate_event_payload,
)
from accesspilot.workspaces import DraftRevisionConflictError


def _fixture(
    factory: sessionmaker[Session],
) -> tuple[str, UUID, UUID, UUID, AgentStepContext, UUID]:
    token = f"t34-svc-{uuid4()}"
    auth_session_id = uuid4()
    graph_run_id = uuid4()
    input_turn_id = f"turn-{uuid4()}"
    pending_input_id = uuid4()
    with factory() as session:
        seed_catalog(session)
        workspace = WorkspaceRecord(
            token_hash=sha256(token.encode()).hexdigest(),
            actor_id="EMP-001",
            flow_version=2,
            lease_fence=1,
            draft={
                "employee_id": "EMP-001",
                "entitlement_id": "insighthub.customer_export",
                "duration_days": 30,
                "justification": "业务需要",
                "confirmed": False,
            },
            draft_revision=1,
            model_call_limit=20,
        )
        session.add(workspace)
        session.flush()
        workspace_id = workspace.id
        session.add(
            AuthSessionRecord(
                id=auth_session_id,
                token_hash=sha256(f"auth-{auth_session_id}".encode()).hexdigest(),
                csrf_hash=sha256(f"csrf-{auth_session_id}".encode()).hexdigest(),
                employee_id="EMP-001",
                workspace_id=workspace_id,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        event = WorkspaceEventRecord(
            workspace_id=workspace_id,
            event_type="message.user",
            payload={"content": "确认提交", "turn_id": input_turn_id},
        )
        session.add(event)
        session.flush()
        session.add(
            AgentTurnExecutionRecord(
                id=uuid4(),
                workspace_id=workspace_id,
                graph_run_id=graph_run_id,
                checkpoint_thread_id=f"accesspilot:v1.3:{graph_run_id}",
                input_seq=0,
                input_turn_id=input_turn_id,
                input_event_id=event.id,
                auth_session_ref=auth_session_id,
                actor_id="EMP-001",
                engine="langgraph",
                attempt=1,
                lease_fence=1,
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                status="running",
                checkpoint_ns="",
            )
        )
        session.add(
            AgentPendingInputRecord(
                id=uuid4(),
                workspace_id=workspace_id,
                agent_thread_id=workspace.agent_thread_id,
                graph_run_id=graph_run_id,
                checkpoint_thread_id=f"accesspilot:v1.3:{graph_run_id}",
                pending_input_id=pending_input_id,
                kind="confirmation",
                draft_revision=1,
                auth_session_ref=auth_session_id,
                actor_id="EMP-001",
                engine="langgraph",
                checkpoint_ns="",
                accepted_checkpoint_id="0" * 32,
                status="active",
            )
        )
        session.commit()
    context = AgentStepContext(
        workspace_id=workspace_id,
        graph_run_id=graph_run_id,
        input_seq=0,
        input_turn_id=input_turn_id,
        actor_id="EMP-001",
        auth_session_ref=auth_session_id,
        lease_fence=1,
    )
    return token, workspace_id, auth_session_id, graph_run_id, context, pending_input_id


def test_agent_input_required_event_is_valid_and_rejects_extra_fields() -> None:
    payload = validate_event_payload(
        "agent.input.required",
        {
            "pending_input_id": str(uuid4()),
            "kind": "confirmation",
            "draft_revision": 3,
            "turn_id": "turn-123",
        },
    )
    assert payload["kind"] == "confirmation"
    assert payload["draft_revision"] == 3
    with pytest.raises(UnsafeEventError):
        validate_event_payload(
            "agent.input.required",
            {
                "pending_input_id": str(uuid4()),
                "kind": "confirmation",
                "draft_revision": 3,
                "turn_id": "turn-123",
                "extra": True,
            },
        )


def test_confirm_draft_is_idempotent_and_advances_revision_once(
    database_session_factory: sessionmaker[Session],
) -> None:
    token, workspace_id, _auth, _graph_run, context, pending_input_id = _fixture(
        database_session_factory
    )
    service = AgentStepOperationService(database_session_factory)

    first = service.confirm_draft(
        context,
        workspace_token=token,
        pending_input_id=pending_input_id,
        expected_revision=1,
    )
    replay = service.confirm_draft(
        context,
        workspace_token=token,
        pending_input_id=pending_input_id,
        expected_revision=2,
    )

    assert isinstance(first, CompletedStepOperation)
    assert first.replayed is False
    assert first.committed_revision == 2
    assert replay.replayed is True
    assert replay.committed_revision == 2

    with database_session_factory() as session:
        workspace = session.get(WorkspaceRecord, workspace_id)
        assert workspace is not None
        assert workspace.draft_revision == 2
        assert workspace.draft is not None
        assert workspace.draft["confirmed"] is True
        operation = confirm_operation_id(
            workspace_id=workspace_id,
            pending_input_id=pending_input_id,
        )
        count = len(
            session.scalars(
                select(AgentStepExecutionRecord).where(
                    AgentStepExecutionRecord.workspace_id == workspace_id,
                    AgentStepExecutionRecord.operation_id == operation,
                )
            ).all()
        )
        assert count == 1


def test_confirm_draft_rejects_revision_conflict_without_write(
    database_session_factory: sessionmaker[Session],
) -> None:
    token, workspace_id, _auth, _graph_run, context, pending_input_id = _fixture(
        database_session_factory
    )
    service = AgentStepOperationService(database_session_factory)

    with pytest.raises(DraftRevisionConflictError):
        service.confirm_draft(
            context,
            workspace_token=token,
            pending_input_id=pending_input_id,
            expected_revision=99,
        )
    with database_session_factory() as session:
        workspace = session.get(WorkspaceRecord, workspace_id)
        assert workspace is not None
        assert workspace.draft_revision == 1
        assert workspace.draft is not None
        assert workspace.draft["confirmed"] is False
