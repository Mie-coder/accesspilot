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
from accesspilot.rag.policies import index_policy_embeddings
from accesspilot.risk.review import (
    DeterministicRiskReviewModel,
    RiskReview,
    RiskReviewContext,
)


class CountingRiskReviewModel:
    def __init__(self) -> None:
        self.calls = 0

    def review(self, context: RiskReviewContext) -> RiskReview:
        self.calls += 1
        return DeterministicRiskReviewModel().review(context)


@pytest.fixture
def approval_client(
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
        )
    )
    yield client
    # 测试库恢复为“待向量化”，避免影响只验证种子行为的旧测试。
    with database_session_factory() as session:
        for chunk in session.scalars(select(PolicyChunkRecord)).all():
            chunk.embedding = None
        session.commit()


def submit_request(client: TestClient) -> str:
    assert client.post("/api/workspaces").status_code == 201
    assert client.post(
        "/api/demo/session",
        json={"employee_id": "EMP-001"},
    ).status_code == 200
    assert (
        client.post(
            "/api/drafts/preview",
            json={
                "employee_id": "EMP-001",
                "entitlement_id": "insighthub.customer_export",
                "duration_days": 14,
                "justification": "核验虚构项目运营数据",
                "confirmed": True,
            },
        ).status_code
        == 200
    )
    response = client.post("/api/requests")
    assert response.status_code == 201
    return response.json()["request_id"]


def start_case(client: TestClient, request_id: str) -> dict[str, object]:
    response = client.post(f"/api/requests/{request_id}/approval-case")
    assert response.status_code == 201
    return response.json()


def switch_identity(client: TestClient, employee_id: str) -> None:
    response = client.post(
        "/api/demo/session",
        json={"employee_id": employee_id},
    )
    assert response.status_code == 200


def test_api_completes_manager_then_data_owner_approval(
    approval_client: TestClient,
) -> None:
    request_id = submit_request(approval_client)
    case = start_case(approval_client, request_id)
    case_id = case["approval_case_id"]
    assert case["approval_status"] == "pending_manager"

    switch_identity(approval_client, "EMP-002")
    manager = approval_client.post(
        f"/api/approval-cases/{case_id}/decisions",
        json={
            "decision": "approve",
            "comment": "经理确认业务需要。",
        },
    )
    assert manager.status_code == 200
    assert manager.json()["approval_status"] == "pending_data_owner"

    switch_identity(approval_client, "EMP-003")
    owner = approval_client.post(
        f"/api/approval-cases/{case_id}/decisions",
        json={
            "decision": "approve",
            "comment": "数据所有者确认最小权限。",
        },
    )
    assert owner.status_code == 200
    assert owner.json()["approval_status"] == "approved"
    assert [step["step_status"] for step in owner.json()["steps"]] == [
        "approved",
        "approved",
    ]


def test_api_rejects_owner_before_manager_without_advancing(
    approval_client: TestClient,
) -> None:
    request_id = submit_request(approval_client)
    case = start_case(approval_client, request_id)
    case_id = case["approval_case_id"]

    switch_identity(approval_client, "EMP-003")
    owner = approval_client.post(
        f"/api/approval-cases/{case_id}/decisions",
        json={"decision": "approve"},
    )
    assert owner.status_code == 409
    assert owner.json() == {"detail": "前序审批尚未完成"}

    switch_identity(approval_client, "EMP-002")
    manager = approval_client.post(
        f"/api/approval-cases/{case_id}/decisions",
        json={"decision": "approve"},
    )
    assert manager.status_code == 200
    assert manager.json()["approval_status"] == "pending_data_owner"


def test_api_rejects_stale_draft_after_workspace_identity_switch(
    approval_client: TestClient,
) -> None:
    assert approval_client.post("/api/workspaces").status_code == 201
    assert approval_client.post(
        "/api/demo/session",
        json={"employee_id": "EMP-001"},
    ).status_code == 200
    preview = approval_client.post(
        "/api/drafts/preview",
        json={
            "employee_id": "EMP-001",
            "entitlement_id": "insighthub.customer_export",
            "duration_days": 14,
            "justification": "核验虚构项目运营数据",
            "confirmed": True,
        },
    )
    assert preview.status_code == 200
    switch_identity(approval_client, "EMP-002")

    submitted = approval_client.post("/api/requests")

    assert submitted.status_code == 409
    assert submitted.json()["detail"] == (
        "当前草稿属于另一演示身份，请切回原身份后提交"
    )


def test_decision_body_cannot_supply_an_actor_id(
    approval_client: TestClient,
) -> None:
    request_id = submit_request(approval_client)
    case_id = start_case(approval_client, request_id)["approval_case_id"]

    response = approval_client.post(
        f"/api/approval-cases/{case_id}/decisions",
        json={"actor_id": "EMP-002", "decision": "approve"},
    )

    assert response.status_code == 422


def test_api_rejects_cross_workspace_before_calling_risk_model(
    database_session_factory: sessionmaker[Session],
) -> None:
    embedding_model = DeterministicEmbeddingModel()
    review_model = CountingRiskReviewModel()
    with database_session_factory() as session:
        seed_catalog(session)
        index_policy_embeddings(session, embedding_model)
    app = create_app(
        store=SqlAlchemyWorkspaceStore(database_session_factory),
        settings=Settings(demo_mode_enabled=True),
        session_factory=database_session_factory,
        embedding_model=embedding_model,
        risk_review_model=review_model,
    )
    owner_browser = TestClient(app)
    other_browser = TestClient(app)
    request_id = submit_request(owner_browser)
    assert other_browser.post("/api/workspaces").status_code == 201

    response = other_browser.post(f"/api/requests/{request_id}/approval-case")

    assert response.status_code == 404
    assert review_model.calls == 0

    with database_session_factory() as session:
        for chunk in session.scalars(select(PolicyChunkRecord)).all():
            chunk.embedding = None
        session.commit()
