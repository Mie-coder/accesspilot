from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.db.seed import seed_catalog
from accesspilot.db.workspace_store import SqlAlchemyWorkspaceStore
from accesspilot.main import create_app


def build_client(
    database_session_factory: sessionmaker[Session],
) -> TestClient:
    with database_session_factory() as session:
        seed_catalog(session)
    return TestClient(
        create_app(
            store=SqlAlchemyWorkspaceStore(database_session_factory),
            session_factory=database_session_factory,
        )
    )


def save_complete_draft(client: TestClient, *, confirmed: bool) -> None:
    response = client.post(
        "/api/drafts/preview",
        json={
            "employee_id": "EMP-001",
            "entitlement_id": "insighthub.customer_export",
            "duration_days": 14,
            "justification": "核验项目运营数据",
            "confirmed": confirmed,
        },
    )
    assert response.status_code == 200


def test_submit_rejects_complete_but_unconfirmed_draft(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    client.post("/api/workspaces")
    save_complete_draft(client, confirmed=False)

    response = client.post("/api/requests")

    assert response.status_code == 409
    assert response.json() == {"detail": "申请草稿尚未明确确认"}


def test_submit_returns_frozen_request(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    client.post("/api/workspaces")
    save_complete_draft(client, confirmed=True)

    response = client.post("/api/requests")

    assert response.status_code == 201
    payload = response.json()
    UUID(payload["request_id"])
    assert payload["request_status"] == "submitted"
