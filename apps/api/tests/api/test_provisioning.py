from datetime import UTC, datetime
from hashlib import sha256
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
from accesspilot.db.workspace_store import SqlAlchemyWorkspaceStore
from accesspilot.main import create_app
from accesspilot.provisioning import SimulatedIamProvisioner
from accesspilot.risk.review import DeterministicRiskReviewModel


def build_approved_request(
    database_session_factory: sessionmaker[Session],
    *,
    fault_mode: str | None = None,
) -> tuple[TestClient, UUID]:
    token = f"provisioning-api-{uuid4()}"
    with database_session_factory() as session:
        seed_catalog(session)
        workspace = WorkspaceRecord(
            token_hash=sha256(token.encode()).hexdigest(),
            fault_mode=fault_mode,
            demo_actor_id="EMP-001" if fault_mode is not None else None,
            demo_session_active=fault_mode is not None,
        )
        session.add(workspace)
        session.flush()
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
    client.cookies.set("accesspilot_workspace", token)
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


def test_api_timeout_requires_status_recovery_before_grant(
    database_session_factory: sessionmaker[Session],
) -> None:
    client, request_id = build_approved_request(
        database_session_factory,
        fault_mode="iam_timeout",
    )
    key = f"api-timeout-{uuid4()}"

    unknown = client.post(
        f"/api/requests/{request_id}/provision",
        json={"idempotency_key": key},
    )

    assert unknown.status_code == 200
    assert unknown.json()["provisioning_status"] == "unknown"
    assert unknown.json()["access_granted"] is False
    assert grant_count(database_session_factory, request_id) == 0

    recovered = client.post(f"/api/requests/{request_id}/provision/recover")

    assert recovered.status_code == 200
    assert recovered.json()["provisioning_status"] == "succeeded"
    assert recovered.json()["access_granted"] is True
    assert grant_count(database_session_factory, request_id) == 1


def test_api_failure_retries_same_key_after_fault_is_cleared(
    database_session_factory: sessionmaker[Session],
) -> None:
    client, request_id = build_approved_request(
        database_session_factory,
        fault_mode="iam_failure",
    )
    key = f"api-failure-{uuid4()}"

    failed = client.post(
        f"/api/requests/{request_id}/provision",
        json={"idempotency_key": key},
    )
    assert failed.json()["provisioning_status"] == "failed"
    assert failed.json()["access_granted"] is False

    cleared = client.post(
        "/api/demo/fault-mode",
        json={"fault_mode": None},
    )
    retried = client.post(
        f"/api/requests/{request_id}/provision",
        json={"idempotency_key": key},
    )

    assert cleared.json() == {"fault_mode": None}
    assert retried.json()["provisioning_status"] == "succeeded"
    assert retried.json()["attempt_count"] == 2
    assert grant_count(database_session_factory, request_id) == 1
