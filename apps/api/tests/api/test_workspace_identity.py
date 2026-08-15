"""T19 closes the legacy anonymous Workspace/Demo identity surface."""

from fastapi.testclient import TestClient

from accesspilot.config import Settings
from accesspilot.main import create_app
from accesspilot.workspaces import InMemoryWorkspaceStore


def test_legacy_identity_and_demo_routes_are_permanently_not_found() -> None:
    client = TestClient(
        create_app(
            settings=Settings(demo_mode_enabled=True),
            store=InMemoryWorkspaceStore(),
        )
    )

    for method, path in (
        ("get", "/api/workspaces/identity"),
        ("post", "/api/workspaces"),
        ("post", "/api/workspaces/ensure"),
        ("post", "/api/demo/session"),
        ("get", "/api/demo/session"),
        ("post", "/api/demo/session/exit"),
    ):
        response = getattr(client, method)(path)
        assert response.status_code == 404, (method, path, response.text)
