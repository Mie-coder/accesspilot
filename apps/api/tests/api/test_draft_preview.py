from fastapi.testclient import TestClient

from accesspilot.config import Settings
from accesspilot.main import create_app
from accesspilot.workspaces import InMemoryWorkspaceStore


def test_preview_requires_a_workspace_cookie() -> None:
    client = TestClient(create_app(store=InMemoryWorkspaceStore()))

    response = client.post(
        "/api/drafts/preview",
        json={"employee_id": "EMP-001"},
    )

    assert response.status_code == 401


def test_preview_returns_missing_fields_for_current_workspace() -> None:
    client = TestClient(create_app(store=InMemoryWorkspaceStore()))
    client.post("/api/workspaces")

    response = client.post(
        "/api/drafts/preview",
        json={
            "employee_id": "EMP-001",
            "entitlement_id": "ENT-CUSTOMER-EXPORT",
        },
    )

    assert response.status_code == 200
    assert response.json()["missing_fields"] == [
        "duration_days",
        "justification",
    ]
    assert response.json()["is_complete"] is False
    assert response.json()["can_enter_approval"] is False
    assert response.json()["draft"] == {
        "employee_id": "EMP-001",
        "entitlement_id": "ENT-CUSTOMER-EXPORT",
        "duration_days": None,
        "justification": None,
        "confirmed": False,
    }


def test_complete_unconfirmed_draft_is_not_ready_for_approval() -> None:
    client = TestClient(create_app(store=InMemoryWorkspaceStore()))
    client.post("/api/workspaces")

    response = client.post(
        "/api/drafts/preview",
        json={
            "employee_id": "EMP-001",
            "entitlement_id": "ENT-CUSTOMER-EXPORT",
            "duration_days": 14,
            "justification": "  核验项目运营数据  ",
        },
    )

    assert response.status_code == 200
    assert response.json()["draft"]["justification"] == "核验项目运营数据"
    assert response.json()["is_complete"] is True
    assert response.json()["can_enter_approval"] is False


def test_confirmed_complete_draft_is_ready_for_approval() -> None:
    client = TestClient(create_app(store=InMemoryWorkspaceStore()))
    client.post("/api/workspaces")

    response = client.post(
        "/api/drafts/preview",
        json={
            "employee_id": "EMP-001",
            "entitlement_id": "ENT-CUSTOMER-EXPORT",
            "duration_days": 14,
            "justification": "核验项目运营数据",
            "confirmed": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["is_complete"] is True
    assert response.json()["can_enter_approval"] is True


def test_preview_rejects_session_and_derived_fields() -> None:
    client = TestClient(create_app(store=InMemoryWorkspaceStore()))
    client.post("/api/workspaces")

    response = client.post(
        "/api/drafts/preview",
        json={
            "employee_id": "EMP-001",
            "workspace": "client-controlled-workspace",
            "manager": {"employee_id": "EMP-999"},
        },
    )

    assert response.status_code == 422
    assert {
        error["type"]
        for error in response.json()["detail"]
    } == {"extra_forbidden"}


def test_preview_rejects_invalid_request_body() -> None:
    client = TestClient(create_app(store=InMemoryWorkspaceStore()))
    client.post("/api/workspaces")

    response = client.post("/api/drafts/preview", json={"duration_days": "tomorrow"})

    assert response.status_code == 422


def test_preview_cannot_override_backend_workspace_identity() -> None:
    client = TestClient(create_app(store=InMemoryWorkspaceStore()))
    client.post("/api/workspaces")

    response = client.post(
        "/api/drafts/preview",
        json={
            "employee_id": "EMP-003",
            "entitlement_id": "insighthub.customer_export",
        },
    )

    assert response.status_code == 200
    assert response.json()["draft"]["employee_id"] == "EMP-001"


def test_preview_does_not_overwrite_an_old_draft_after_identity_switch() -> None:
    store = InMemoryWorkspaceStore()
    client = TestClient(
        create_app(
            store=store,
            settings=Settings(demo_mode_enabled=True),
        )
    )
    assert client.post("/api/workspaces").status_code == 201
    assert client.post(
        "/api/demo/session",
        json={"employee_id": "EMP-001"},
    ).status_code == 200
    token = client.cookies.get("accesspilot_workspace")
    assert token is not None
    original = client.post(
        "/api/drafts/preview",
        json={
            "employee_id": "EMP-001",
            "entitlement_id": "insighthub.customer_export",
            "duration_days": 14,
            "justification": "核验项目运营数据",
            "confirmed": True,
        },
    )
    assert original.status_code == 200
    assert client.post(
        "/api/demo/session",
        json={"employee_id": "EMP-002"},
    ).status_code == 200

    response = client.post(
        "/api/drafts/preview",
        json={"employee_id": "EMP-002", "duration_days": 7},
    )

    assert response.status_code == 409
    workspace = store.get(token)
    assert workspace is not None
    assert workspace.draft is not None
    assert workspace.draft.employee_id == "EMP-001"
    assert workspace.draft.duration_days == 14
    assert workspace.draft.confirmed is True
