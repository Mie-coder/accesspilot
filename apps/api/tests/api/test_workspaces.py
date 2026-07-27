from fastapi.testclient import TestClient

from accesspilot.main import create_app
from accesspilot.workspaces import InMemoryWorkspaceStore


def test_create_workspace_sets_http_only_cookie() -> None:
    client = TestClient(create_app(store=InMemoryWorkspaceStore()))

    response = client.post("/api/workspaces")

    assert response.status_code == 201
    assert response.json() == {"status": "created"}
    assert "accesspilot_workspace=" in response.headers["set-cookie"]
    assert "httponly" in response.headers["set-cookie"].lower()


def test_reset_with_unknown_workspace_token_returns_not_found() -> None:
    client = TestClient(
        create_app(store=InMemoryWorkspaceStore()),
        raise_server_exceptions=False,
    )
    client.cookies.set("accesspilot_workspace", "does-not-exist")

    response = client.post("/api/workspaces/reset")

    assert response.status_code == 404


def test_reset_only_clears_the_current_browser_workspace() -> None:
    store = InMemoryWorkspaceStore()
    app = create_app(store=store)
    first_browser = TestClient(app)
    second_browser = TestClient(app)

    first_browser.post("/api/workspaces")
    second_browser.post("/api/workspaces")
    first_token = first_browser.cookies.get("accesspilot_workspace")
    second_token = second_browser.cookies.get("accesspilot_workspace")
    assert first_token is not None
    assert second_token is not None

    first_browser.post(
        "/api/drafts/preview",
        json={
            "employee_id": "EMP-001",
            "entitlement_id": "ENT-CUSTOMER-EXPORT",
        },
    )
    second_browser.post(
        "/api/drafts/preview",
        json={
            "employee_id": "EMP-002",
            "entitlement_id": "ENT-OPS-LOG-READ",
        },
    )

    response = first_browser.post("/api/workspaces/reset")

    first_workspace = store.get(first_token)
    second_workspace = store.get(second_token)
    assert response.status_code == 200
    assert response.json() == {"status": "reset"}
    assert first_browser.cookies.get("accesspilot_workspace") == first_token
    assert first_workspace is not None
    assert first_workspace.draft is None
    assert second_workspace is not None
    assert second_workspace.draft is not None
    assert second_workspace.draft.employee_id == "EMP-002"
    assert second_workspace.draft.entitlement_id == "ENT-OPS-LOG-READ"
