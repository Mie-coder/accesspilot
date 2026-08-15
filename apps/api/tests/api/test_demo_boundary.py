"""T19 keeps the product free of the former anonymous Demo identity boundary."""

from fastapi.testclient import TestClient

from accesspilot.config import Settings
from accesspilot.main import create_app
from accesspilot.workspaces import InMemoryWorkspaceStore


def test_former_demo_controls_are_closed_even_when_demo_mode_is_enabled() -> None:
    client = TestClient(
        create_app(
            settings=Settings(demo_mode_enabled=True),
            store=InMemoryWorkspaceStore(),
        )
    )

    for method, path in (
        ("post", "/api/demo/session"),
        ("get", "/api/demo/session"),
        ("post", "/api/demo/session/exit"),
        ("post", "/api/demo/reset"),
        ("post", "/api/demo/fault-mode"),
        ("get", "/api/demo/model-quota"),
    ):
        response = getattr(client, method)(path)
        assert response.status_code == 404, (method, path, response.text)
