from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.embeddings import DeterministicEmbeddingModel
from accesspilot.config import Settings
from accesspilot.db.models import (
    AccessGrantRecord,
    AccessRequestRecord,
    ApprovalCaseRecord,
    WorkspaceRecord,
)
from accesspilot.db.seed import seed_catalog
from accesspilot.db.workspace_store import (
    SqlAlchemyWorkspaceStore,
    hash_workspace_token,
)
from accesspilot.main import create_app
from accesspilot.provisioning import SimulatedIamProvisioner
from accesspilot.risk.review import DeterministicRiskReviewModel
from support.auth import login_as


def build_approved_request(
    database_session_factory: sessionmaker[Session],
) -> tuple[TestClient, UUID]:
    with database_session_factory() as session:
        seed_catalog(session)
    client = TestClient(
        create_app(
            store=SqlAlchemyWorkspaceStore(database_session_factory),
            settings=Settings(demo_mode_enabled=True),
            session_factory=database_session_factory,
            embedding_model=DeterministicEmbeddingModel(),
            risk_review_model=DeterministicRiskReviewModel(),
            iam_provisioner=SimulatedIamProvisioner(),
        )
    )
    login_as(client)
    token = client.cookies.get("accesspilot_session")
    assert token is not None
    with database_session_factory() as session:
        workspace = session.scalar(
            select(WorkspaceRecord).where(
                WorkspaceRecord.token_hash == hash_workspace_token(token)
            )
        )
        assert workspace is not None
        request = AccessRequestRecord(
            workspace_id=workspace.id,
            requester_id="EMP-001",
            entitlement_code="insighthub.customer_export",
            duration_days=14,
            justification="核验虚构项目运营数据",
            request_status="submitted",
            confirmed_at=datetime.now(UTC),
        )
        session.add(request)
        session.flush()
        session.add(
            ApprovalCaseRecord(
                workspace_id=workspace.id,
                request_id=request.id,
                approval_status="approved",
            )
        )
        session.commit()
        request_id = request.id
    return client, request_id


def grant_count(
    database_session_factory: sessionmaker[Session],
    request_id: UUID,
) -> int:
    with database_session_factory() as session:
        return session.scalar(
            select(func.count())
            .select_from(AccessGrantRecord)
            .where(AccessGrantRecord.request_id == request_id)
        ) or 0


def test_api_success_replay_creates_one_grant(
    database_session_factory: sessionmaker[Session],
) -> None:
    client, request_id = build_approved_request(database_session_factory)
    key = f"api-success-{uuid4()}"

    first = client.post(
        f"/api/requests/{request_id}/provision",
        json={"idempotency_key": key},
    )
    second = client.post(
        f"/api/requests/{request_id}/provision",
        json={"idempotency_key": key},
    )

    assert first.status_code == 200
    assert first.json()["provisioning_status"] == "succeeded"
    assert first.json()["access_granted"] is True
    assert second.json()["provisioning_attempt_id"] == first.json()[
        "provisioning_attempt_id"
    ]
    assert grant_count(database_session_factory, request_id) == 1


def test_legacy_fault_control_is_closed_before_provisioning(
    database_session_factory: sessionmaker[Session],
) -> None:
    client, _ = build_approved_request(database_session_factory)
    cleared = client.post(
        "/api/demo/fault-mode",
        json={"fault_mode": None},
    )
    assert cleared.status_code == 404
