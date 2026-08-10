import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.config import Settings
from accesspilot.db.seed import seed_catalog
from accesspilot.db.workspace_store import SqlAlchemyWorkspaceStore
from accesspilot.main import create_app
from accesspilot.workspaces import InMemoryWorkspaceStore


def build_client(
    database_session_factory: sessionmaker[Session],
) -> TestClient:
    with database_session_factory() as session:
        seed_catalog(session)
    return TestClient(
        create_app(
            store=SqlAlchemyWorkspaceStore(database_session_factory),
            settings=Settings(demo_mode_enabled=True),
            session_factory=database_session_factory,
        )
    )


def test_preview_requires_a_workspace_cookie() -> None:
    client = TestClient(create_app(store=InMemoryWorkspaceStore()))

    response = client.post(
        "/api/drafts/preview",
        json={"employee_id": "EMP-001"},
    )

    assert response.status_code == 401


def test_preview_returns_missing_fields_for_current_workspace(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    assert client.post("/api/workspaces").status_code == 201

    response = client.post(
        "/api/drafts/preview",
        json={
            "employee_id": "EMP-001",
            "entitlement_id": "insighthub.customer_export",
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
        "entitlement_id": "insighthub.customer_export",
        "duration_days": None,
        "justification": None,
        "confirmed": False,
    }


def test_complete_unconfirmed_draft_is_not_ready_for_approval(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    assert client.post("/api/workspaces").status_code == 201

    response = client.post(
        "/api/drafts/preview",
        json={
            "employee_id": "EMP-001",
            "entitlement_id": "insighthub.customer_export",
            "duration_days": 14,
            "justification": "  核验项目运营数据  ",
        },
    )

    assert response.status_code == 200
    assert response.json()["draft"]["justification"] == "核验项目运营数据"
    assert response.json()["is_complete"] is True
    assert response.json()["can_enter_approval"] is False


def test_confirmed_complete_draft_is_ready_for_approval(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    assert client.post("/api/workspaces").status_code == 201

    response = client.post(
        "/api/drafts/preview",
        json={
            "employee_id": "EMP-001",
            "entitlement_id": "insighthub.customer_export",
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
    assert {error["type"] for error in response.json()["detail"]} == {"extra_forbidden"}


def test_preview_rejects_invalid_request_body() -> None:
    client = TestClient(create_app(store=InMemoryWorkspaceStore()))
    client.post("/api/workspaces")

    response = client.post("/api/drafts/preview", json={"duration_days": "tomorrow"})

    assert response.status_code == 422


def test_preview_cannot_override_backend_workspace_identity(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    assert client.post("/api/workspaces").status_code == 201

    response = client.post(
        "/api/drafts/preview",
        json={
            "employee_id": "EMP-003",
            "entitlement_id": "insighthub.customer_export",
        },
    )

    assert response.status_code == 200
    assert response.json()["draft"]["employee_id"] == "EMP-001"


def test_preview_does_not_overwrite_an_old_draft_after_identity_switch(
    database_session_factory: sessionmaker[Session],
) -> None:
    with database_session_factory() as session:
        seed_catalog(session)
    store = SqlAlchemyWorkspaceStore(database_session_factory)
    client = TestClient(
        create_app(
            store=store,
            settings=Settings(demo_mode_enabled=True),
            session_factory=database_session_factory,
        )
    )
    assert client.post("/api/workspaces").status_code == 201
    assert (
        client.post(
            "/api/demo/session",
            json={"employee_id": "EMP-001"},
        ).status_code
        == 200
    )
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
    assert (
        client.post(
            "/api/demo/session",
            json={"employee_id": "EMP-002"},
        ).status_code
        == 200
    )

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


def test_preview_rejects_forged_entitlement_without_creating_a_draft(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    assert client.post("/api/workspaces").status_code == 201

    response = client.post(
        "/api/drafts/preview",
        json={
            "employee_id": "EMP-001",
            "entitlement_id": "invented.admin",
            "duration_days": 14,
            "justification": "执行虚构排查",
            "confirmed": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["draft"] is None
    assert payload["can_enter_approval"] is False
    resolution = payload["entitlement_resolution"]
    assert resolution["status"] == "no_match"
    assert resolution["target_field"] == "entitlement_id"
    assert resolution["candidates"] == []
    assert resolution["eligible_access"]
    assert client.get("/api/drafts/current").json() == {"draft": None}


def test_preview_ambiguous_entitlement_keeps_existing_draft_unchanged(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    assert client.post("/api/workspaces").status_code == 201
    original = client.post(
        "/api/drafts/preview",
        json={
            "employee_id": "EMP-001",
            "entitlement_id": "codeforge.repo_read",
            "duration_days": 7,
            "justification": "原有虚构理由",
            "confirmed": False,
        },
    )
    assert original.status_code == 200
    snapshot = original.json()["draft"]

    response = client.post(
        "/api/drafts/preview",
        json={
            "employee_id": "EMP-001",
            "entitlement_id": "数据洞察中心",
            "duration_days": 30,
            "justification": "新虚构理由",
            "confirmed": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["draft"] == snapshot
    assert payload["can_enter_approval"] is False
    resolution = payload["entitlement_resolution"]
    assert resolution["status"] == "ambiguous"
    assert resolution["target_field"] == "entitlement_id"
    assert [candidate["code"] for candidate in resolution["candidates"]] == [
        "insighthub.customer_export",
        "insighthub.dashboard_view",
    ]
    assert client.get("/api/drafts/current").json() == {"draft": snapshot}


def test_preview_alias_is_canonicalized_to_stable_entitlement_code(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    assert client.post("/api/workspaces").status_code == 201

    response = client.post(
        "/api/drafts/preview",
        json={
            "employee_id": "EMP-001",
            "entitlement_id": "仪表盘查看",
            "duration_days": 14,
            "justification": "核验虚构数据",
            "confirmed": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["draft"]["entitlement_id"] == "insighthub.dashboard_view"
    assert payload["draft"]["confirmed"] is True
    assert payload["entitlement_resolution"]["status"] == "matched"
    assert payload["entitlement_resolution"]["target_field"] == "entitlement_id"


def test_preview_switching_entitlement_invalidates_old_confirmation(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    assert client.post("/api/workspaces").status_code == 201
    original = client.post(
        "/api/drafts/preview",
        json={
            "employee_id": "EMP-001",
            "entitlement_id": "insighthub.customer_export",
            "duration_days": 14,
            "justification": "继续核验虚构数据",
            "confirmed": True,
        },
    )
    assert original.status_code == 200
    assert original.json()["draft"]["confirmed"] is True

    response = client.post(
        "/api/drafts/preview",
        json={
            "employee_id": "EMP-001",
            "entitlement_id": "仪表盘查看",
            "duration_days": 14,
            "justification": "继续核验虚构数据",
            "confirmed": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["draft"]["entitlement_id"] == "insighthub.dashboard_view"
    assert payload["draft"]["confirmed"] is False
    assert client.get("/api/drafts/current").json()["draft"] == payload["draft"]


def test_preview_exposes_duration_limit_issue_and_blocks_approval(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    assert client.post("/api/workspaces").status_code == 201

    response = client.post(
        "/api/drafts/preview",
        json={
            "employee_id": "EMP-001",
            "entitlement_id": "insighthub.customer_export",
            "duration_days": 31,
            "justification": "核验虚构数据",
            "confirmed": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["draft"]["entitlement_id"] == "insighthub.customer_export"
    assert payload["draft"]["duration_days"] == 31
    assert payload["draft"]["confirmed"] is False
    assert payload["can_enter_approval"] is False
    assert any(issue["code"] == "duration_exceeds_maximum" for issue in payload["issues"])


@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [("duration_days", 7), ("justification", "改为核对月度数据")],
)
def test_preview_editing_confirmed_business_field_requires_new_confirmation(
    database_session_factory: sessionmaker[Session],
    changed_field: str,
    changed_value: int | str,
) -> None:
    client = build_client(database_session_factory)
    assert client.post("/api/workspaces").status_code == 201
    original = client.post(
        "/api/drafts/preview",
        json={
            "employee_id": "EMP-001",
            "entitlement_id": "insighthub.customer_export",
            "duration_days": 14,
            "justification": "核验虚构数据",
            "confirmed": True,
        },
    )
    assert original.status_code == 200
    assert original.json()["draft"]["confirmed"] is True

    update = {
        "employee_id": "EMP-001",
        "entitlement_id": "insighthub.customer_export",
        "duration_days": 14,
        "justification": "核验虚构数据",
        "confirmed": True,
    }
    update[changed_field] = changed_value
    response = client.post("/api/drafts/preview", json=update)

    assert response.status_code == 200
    payload = response.json()
    assert payload["draft"][changed_field] == changed_value
    assert payload["draft"]["confirmed"] is False
    assert payload["can_enter_approval"] is False
    assert client.get("/api/drafts/current").json()["draft"] == payload["draft"]
