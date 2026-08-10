from fastapi.testclient import TestClient

from accesspilot.config import Settings
from accesspilot.main import create_app
from accesspilot.workspaces import InMemoryWorkspaceStore, WorkspaceService


def test_create_workspace_sets_http_only_cookie() -> None:
    client = TestClient(
        create_app(
            settings=Settings(demo_mode_enabled=True),
            store=InMemoryWorkspaceStore(),
        )
    )

    response = client.post("/api/workspaces")

    assert response.status_code == 201
    assert response.json() == {"status": "created"}
    assert "accesspilot_workspace=" in response.headers["set-cookie"]
    assert "httponly" in response.headers["set-cookie"].lower()


def test_ensure_workspace_creates_once_and_then_reuses_cookie() -> None:
    client = TestClient(
        create_app(
            settings=Settings(demo_mode_enabled=True),
            store=InMemoryWorkspaceStore(),
        )
    )

    created = client.post("/api/workspaces/ensure")
    token = client.cookies.get("accesspilot_workspace")
    existing = client.post("/api/workspaces/ensure")

    assert created.json() == {"status": "created"}
    assert token is not None
    assert existing.json() == {"status": "existing"}
    assert client.cookies.get("accesspilot_workspace") == token


def test_reset_with_unknown_workspace_token_returns_not_found() -> None:
    client = TestClient(
        create_app(settings=Settings(demo_mode_enabled=True), store=InMemoryWorkspaceStore()),
        raise_server_exceptions=False,
    )
    client.cookies.set("accesspilot_workspace", "does-not-exist")

    response = client.post("/api/demo/reset")

    assert response.status_code == 404


def test_reset_only_clears_the_current_browser_workspace() -> None:
    store = InMemoryWorkspaceStore()
    app = create_app(settings=Settings(demo_mode_enabled=True), store=store)
    first_browser = TestClient(app)
    second_browser = TestClient(app)

    first_browser.post("/api/workspaces")
    second_browser.post("/api/workspaces")
    first_token = first_browser.cookies.get("accesspilot_workspace")
    second_token = second_browser.cookies.get("accesspilot_workspace")
    assert first_token is not None
    assert second_token is not None
    WorkspaceService(store).set_actor(second_token, "EMP-002")

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

    assert (
        first_browser.post("/api/demo/session", json={"employee_id": "EMP-001"}).status_code == 200
    )
    response = first_browser.post("/api/demo/reset")

    first_workspace = store.get(first_token)
    replacement_token = first_browser.cookies.get("accesspilot_workspace")
    replacement_workspace = store.get(replacement_token) if replacement_token else None
    second_workspace = store.get(second_token)
    assert response.status_code == 200
    assert response.json() == {"status": "reset"}
    assert replacement_token is not None
    assert replacement_token != first_token
    assert first_workspace is not None
    assert first_workspace.draft is not None
    assert first_workspace.draft.employee_id == "EMP-001"
    assert replacement_workspace is not None
    assert replacement_workspace.draft is None
    assert replacement_workspace.actor_id == "EMP-001"
    assert replacement_workspace.demo_session_active is True
    assert replacement_workspace.demo_actor_id == "EMP-001"
    assert second_workspace is not None
    assert second_workspace.draft is not None
    assert second_workspace.draft.employee_id == "EMP-002"
    assert second_workspace.draft.entitlement_id == "ENT-OPS-LOG-READ"


def test_feature_off_restores_product_workspace_from_demo_cookie() -> None:
    store = InMemoryWorkspaceStore()
    enabled_service = WorkspaceService(
        store,
        product_actor_id="EMP-001",
        demo_mode_enabled=True,
    )
    product = enabled_service.create()
    demo = enabled_service.create()
    enabled_service.enter_demo(demo.token, "EMP-002")

    app = create_app(
        settings=Settings(demo_mode_enabled=False),
        store=store,
    )
    client = TestClient(app)
    client.cookies.clear()
    client.cookies.set(
        "accesspilot_workspace", demo.token, domain="testserver.local", path="/"
    )
    client.cookies.set(
        "accesspilot_product_workspace", product.token, domain="testserver.local", path="/"
    )

    response = client.get("/api/drafts/current")

    assert response.status_code == 200
    assert response.json() == {"draft": None}
    assert client.cookies.get("accesspilot_workspace") == product.token
    assert client.cookies.get("accesspilot_product_workspace") is None
    restored = store.get(product.token)
    stale_demo = store.get(demo.token)
    assert restored is not None and restored.actor_id == "EMP-001"
    assert stale_demo is not None
    assert stale_demo.demo_session_active is False
    assert stale_demo.demo_actor_id is None
    assert stale_demo.fault_mode is None
