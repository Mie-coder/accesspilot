"""Legacy Workspace entrypoints are intentionally closed in the T19 product."""

from fastapi.testclient import TestClient

from accesspilot.config import Settings
from accesspilot.main import create_app
from accesspilot.workspaces import InMemoryWorkspaceStore


def test_anonymous_workspace_bootstrap_and_demo_reset_are_not_found() -> None:
    client = TestClient(
        create_app(
            settings=Settings(demo_mode_enabled=True),
            store=InMemoryWorkspaceStore(),
        )
    )

    for method, path in (
        ("post", "/api/workspaces"),
        ("post", "/api/workspaces/ensure"),
        ("post", "/api/demo/reset"),
        ("post", "/api/demo/fault-mode"),
    ):
        response = getattr(client, method)(path)
        assert response.status_code == 404, (method, path, response.text)
