from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.db.seed import seed_catalog
from accesspilot.main import create_app
from accesspilot.workspaces import InMemoryWorkspaceStore


def build_client(
    database_session_factory: sessionmaker[Session],
) -> TestClient:
    with database_session_factory() as session:
        seed_catalog(session)
    return TestClient(
        create_app(
            store=InMemoryWorkspaceStore(),
            session_factory=database_session_factory,
        )
    )


def test_workspace_identity_defaults_to_applicant_and_can_switch(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    client.post("/api/workspaces")

    applicant = client.get("/api/workspaces/identity")
    switched = client.post(
        "/api/workspaces/identity",
        json={"employee_id": "EMP-002"},
    )
    current = client.get("/api/workspaces/identity")

    assert applicant.status_code == 200
    assert applicant.json()["employee_id"] == "EMP-001"
    assert applicant.json()["department"] == "product_operations"
    assert switched.status_code == 200
    assert switched.json()["employee_id"] == "EMP-002"
    assert "manager" in switched.json()["roles"]
    assert current.json() == switched.json()


def test_workspace_identity_rejects_unknown_or_unexposed_demo_actor(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    client.post("/api/workspaces")

    unknown = client.post(
        "/api/workspaces/identity",
        json={"employee_id": "EMP-999"},
    )
    hidden = client.post(
        "/api/workspaces/identity",
        json={"employee_id": "EMP-004"},
    )

    assert unknown.status_code == 422
    assert hidden.status_code == 422
    assert client.get("/api/workspaces/identity").json()["employee_id"] == "EMP-001"
