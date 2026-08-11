from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.embeddings import DeterministicEmbeddingModel
from accesspilot.config import Settings
from accesspilot.db.models import PolicyChunkRecord
from accesspilot.db.seed import seed_catalog
from accesspilot.db.workspace_store import SqlAlchemyWorkspaceStore
from accesspilot.main import create_app
from accesspilot.provisioning import SimulatedIamProvisioner
from accesspilot.rag.policies import index_policy_embeddings
from accesspilot.risk.review import DeterministicRiskReviewModel
from support.auth import login_as


@pytest.fixture
def operations_client(
    database_session_factory: sessionmaker[Session],
) -> Iterator[TestClient]:
    embedding_model = DeterministicEmbeddingModel()
    with database_session_factory() as session:
        seed_catalog(session)
        index_policy_embeddings(session, embedding_model)
    client = TestClient(
        create_app(
            store=SqlAlchemyWorkspaceStore(database_session_factory),
            settings=Settings(demo_mode_enabled=True),
            session_factory=database_session_factory,
            embedding_model=embedding_model,
            risk_review_model=DeterministicRiskReviewModel(),
            iam_provisioner=SimulatedIamProvisioner(),
        )
    )
    login_as(client)
    yield client
    with database_session_factory() as session:
        for chunk in session.scalars(select(PolicyChunkRecord)).all():
            chunk.embedding = None
        session.commit()


def submit_and_start(client: TestClient) -> tuple[str, str]:
    assert client.post(
        "/api/drafts/preview",
        json={
            "entitlement_id": "insighthub.customer_export",
            "duration_days": 14,
            "justification": "核验虚构项目运营数据",
            "confirmed": True,
        },
    ).status_code == 200
    request_id = client.post("/api/requests").json()["request_id"]
    started = client.post(f"/api/requests/{request_id}/approval-case")
    assert started.status_code == 201
    return request_id, started.json()["approval_case_id"]


def role_client(client: TestClient, employee_id: str) -> TestClient:
    role = TestClient(client.app)
    login_as(role, employee_id)
    return role


def test_inbox_is_private_to_each_auth_session(
    operations_client: TestClient,
) -> None:
    _, case_id = submit_and_start(operations_client)

    manager_client = role_client(operations_client, "EMP-002")
    owner_client = role_client(operations_client, "EMP-003")
    manager = manager_client.get("/api/approval-inbox")
    owner_waiting = owner_client.get("/api/approval-inbox")

    assert manager.status_code == 200
    assert manager.json()["items"] == []
    assert owner_waiting.json()["items"] == []

    decided = manager_client.post(
        f"/api/approval-cases/{case_id}/decisions",
        json={"decision": "approve"},
    )
    assert decided.status_code == 404
    assert operations_client.get("/api/approval-inbox").json()["items"] == []


def test_detail_returns_policy_steps_and_append_only_audit(
    operations_client: TestClient,
) -> None:
    request_id, _ = submit_and_start(operations_client)

    response = operations_client.get(f"/api/requests/{request_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["view_mode"] == "read_only_replay"
    assert body["request"]["request_id"] == request_id
    assert body["entitlement"]["risk_level"] == "high"
    assert body["risk_review"]["citations"]
    assert body["risk_review"]["citations"][0]["title"]
    assert [step["step_status"] for step in body["approval"]["steps"]] == [
        "pending",
        "waiting",
    ]
    assert body["provisioning"] == {
        "provisioning_status": "not_started",
        "provisioning_attempt_id": None,
        "attempt_count": 0,
        "last_error": None,
        "updated_at": None,
        "access_granted": False,
        "grant_id": None,
        "starts_at": None,
        "expires_at": None,
    }
    assert [event["event_type"] for event in body["audit_events"]] == [
        "request.submitted",
        "risk_review.completed",
        "approval.started",
    ]


def test_legacy_demo_fault_control_is_closed_before_provisioning(
    operations_client: TestClient,
) -> None:
    request_id, case_id = submit_and_start(operations_client)
    for actor_id in ("EMP-002", "EMP-003"):
        role = role_client(operations_client, actor_id)
        assert role.post(
            f"/api/approval-cases/{case_id}/decisions",
            json={"decision": "approve"},
        ).status_code == 404
    assert operations_client.post(
        "/api/demo/fault-mode",
        json={"fault_mode": "iam_timeout"},
    ).status_code == 404

    provisioned = operations_client.post(
        f"/api/requests/{request_id}/provision",
        json={"idempotency_key": f"accesspilot-{request_id}"},
    )
    detail = operations_client.get(f"/api/requests/{request_id}")

    assert provisioned.status_code == 409
    assert detail.status_code == 200
    assert detail.json()["provisioning"]["access_granted"] is False

    recovered = operations_client.post(f"/api/requests/{request_id}/provision/recover")
    assert recovered.status_code == 409


def test_detail_does_not_cross_workspace_boundary(
    operations_client: TestClient,
) -> None:
    request_id, _ = submit_and_start(operations_client)
    other_browser = TestClient(operations_client.app)
    login_as(other_browser, "EMP-002")

    assert other_browser.get(f"/api/requests/{request_id}").status_code == 404
    assert other_browser.get("/api/approval-inbox").json()["items"] == []
