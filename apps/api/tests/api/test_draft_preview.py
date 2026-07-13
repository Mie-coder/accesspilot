from fastapi.testclient import TestClient

from accesspilot.main import create_app


def test_preview_requires_a_workspace_cookie() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/api/drafts/preview",
        json={"system_name": "InsightHub", "entitlement_name": "客户数据导出"},
    )

    assert response.status_code == 401


def test_preview_returns_missing_fields_for_current_workspace() -> None:
    client = TestClient(create_app())
    client.post("/api/workspaces")

    response = client.post(
        "/api/drafts/preview",
        json={"system_name": "InsightHub", "entitlement_name": "客户数据导出"},
    )

    assert response.status_code == 200
    assert response.json()["missing_fields"] == [
        "project_code",
        "data_scope",
        "business_reason",
        "start_date",
        "duration_days",
    ]
    assert response.json()["is_complete"] is False


def test_preview_rejects_invalid_request_body() -> None:
    client = TestClient(create_app())
    client.post("/api/workspaces")

    response = client.post("/api/drafts/preview", json={"duration_days": "tomorrow"})

    assert response.status_code == 422
