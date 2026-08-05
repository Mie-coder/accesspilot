from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from accesspilot.db.models import (
    AccessRequestRecord,
    AuditEventRecord,
    WorkspaceRecord,
)
from accesspilot.db.seed import seed_catalog
from accesspilot.domain.models import RequestDraft
from accesspilot.requests import (
    RequestActorMismatchError,
    RequestNotReadyError,
    submit_access_request,
)


def create_workspace(session: Session) -> tuple[str, WorkspaceRecord]:
    token = f"request-submission-{uuid4()}"
    workspace = WorkspaceRecord(token_hash=sha256(token.encode()).hexdigest())
    session.add(workspace)
    session.commit()
    return token, workspace


def complete_draft(*, confirmed: bool) -> RequestDraft:
    return RequestDraft(
        employee_id="EMP-001",
        entitlement_id="insighthub.customer_export",
        duration_days=14,
        justification="核验项目运营数据",
        confirmed=confirmed,
    )


def test_unconfirmed_draft_cannot_create_request(database_session: Session) -> None:
    seed_catalog(database_session)
    token, workspace = create_workspace(database_session)

    with pytest.raises(RequestNotReadyError):
        submit_access_request(
            database_session,
            workspace_token=token,
            draft=complete_draft(confirmed=False),
        )

    stored_request = database_session.scalar(
        select(AccessRequestRecord).where(
            AccessRequestRecord.workspace_id == workspace.id
        )
    )
    assert stored_request is None


def test_confirmed_draft_freezes_request_and_audit(database_session: Session) -> None:
    seed_catalog(database_session)
    token, workspace = create_workspace(database_session)

    request = submit_access_request(
        database_session,
        workspace_token=token,
        draft=complete_draft(confirmed=True),
    )

    audit = database_session.scalar(
        select(AuditEventRecord).where(AuditEventRecord.request_id == request.id)
    )
    assert request.workspace_id == workspace.id
    assert request.requester_id == "EMP-001"
    assert request.entitlement_code == "insighthub.customer_export"
    assert request.duration_days == 14
    assert request.justification == "核验项目运营数据"
    assert request.request_status == "submitted"
    assert audit is not None
    assert audit.event_type == "request.submitted"
    assert audit.details == {
        "employee_id": "EMP-001",
        "entitlement_id": "insighthub.customer_export",
        "duration_days": 14,
        "justification": "核验项目运营数据",
    }


def test_submit_rejects_a_draft_owned_by_another_demo_identity(
    database_session: Session,
) -> None:
    seed_catalog(database_session)
    token, workspace = create_workspace(database_session)
    workspace.actor_id = "EMP-002"
    database_session.commit()

    with pytest.raises(RequestActorMismatchError):
        submit_access_request(
            database_session,
            workspace_token=token,
            draft=complete_draft(confirmed=True),
            actor_id="EMP-002",
        )

    assert database_session.scalar(
        select(AccessRequestRecord).where(
            AccessRequestRecord.workspace_id == workspace.id
        )
    ) is None
