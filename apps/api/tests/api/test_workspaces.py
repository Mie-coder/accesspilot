from fastapi.testclient import TestClient

from accesspilot.main import create_app


def test_create_workspace_sets_http_only_cookie() -> None:
    client = TestClient(create_app())

    response = client.post("/api/workspaces")

    assert response.status_code == 201
    assert response.json() == {"status": "created"}
    assert "accesspilot_workspace=" in response.headers["set-cookie"]
    assert "httponly" in response.headers["set-cookie"].lower()
