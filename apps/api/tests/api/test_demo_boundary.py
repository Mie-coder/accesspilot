"""T12 产品身份与 Demo 控制面边界回归。"""

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.config import Settings
from accesspilot.db.models import WorkspaceRecord
from accesspilot.db.seed import seed_catalog
from accesspilot.db.workspace_store import SqlAlchemyWorkspaceStore, hash_workspace_token
from accesspilot.events import append_workspace_event, consume_model_call, list_workspace_events
from accesspilot.main import create_app


def build_client(
    database_session_factory: sessionmaker[Session],
    *,
    demo_mode_enabled: bool = True,
    product_actor_id: str = "EMP-001",
) -> TestClient:
    with database_session_factory() as session:
        seed_catalog(session)
    return TestClient(
        create_app(
            settings=Settings(
                database_url="postgresql+psycopg://unused",
                demo_mode_enabled=demo_mode_enabled,
                product_actor_id=product_actor_id,
            ),
            store=SqlAlchemyWorkspaceStore(database_session_factory),
            session_factory=database_session_factory,
        )
    )


def create_workspace(client: TestClient) -> str:
    response = client.post("/api/workspaces")
    assert response.status_code == 201
    token = client.cookies.get("accesspilot_workspace")
    assert token is not None
    return token


def test_product_identity_ignores_legacy_workspace_actor_and_hides_controls(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    token = create_workspace(client)
    with database_session_factory() as session:
        workspace = session.scalar(
            select(WorkspaceRecord).where(
                WorkspaceRecord.token_hash == hash_workspace_token(token)
            )
        )
        assert workspace is not None
        workspace.actor_id = "EMP-002"
        session.commit()

    identity = client.get("/api/workspaces/identity")
    legacy_switch = client.post(
        "/api/workspaces/identity",
        json={"employee_id": "EMP-002"},
    )
    legacy_fault = client.post(
        "/api/workspaces/fault-mode",
        json={"fault_mode": "iam_timeout"},
    )
    legacy_reset = client.post("/api/workspaces/reset")
    product_body = client.get("/api/drafts/current").json()

    assert identity.status_code == 200
    assert identity.json()["employee_id"] == "EMP-001"
    assert legacy_switch.status_code in {404, 405}
    assert legacy_fault.status_code == 404
    assert legacy_reset.status_code == 404
    assert "demo_session_active" not in identity.json()
    assert "quota" not in product_body


def test_demo_session_requires_explicit_entry_and_exit_restores_product_identity(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    create_workspace(client)

    before = client.get("/api/workspaces/identity")
    entered = client.post(
        "/api/demo/session",
        json={"employee_id": "EMP-002"},
    )
    during = client.get("/api/workspaces/identity")
    fault = client.post(
        "/api/demo/fault-mode",
        json={"fault_mode": "iam_timeout"},
    )
    exited = client.post("/api/demo/session/exit")
    after = client.get("/api/workspaces/identity")

    assert before.json()["employee_id"] == "EMP-001"
    assert entered.status_code == 200
    assert entered.json()["demo_session_active"] is True
    assert during.json()["employee_id"] == "EMP-002"
    assert fault.status_code == 200
    assert exited.status_code == 200
    assert exited.json()["demo_session_active"] is False
    assert "demo_session_active" not in after.json()
    assert after.json()["employee_id"] == "EMP-001"


def test_demo_controls_are_not_available_when_feature_is_disabled(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory, demo_mode_enabled=False)
    create_workspace(client)

    for method, path, payload in (
        ("get", "/api/demo/session", None),
        ("post", "/api/demo/session", {"employee_id": "EMP-002"}),
        ("post", "/api/demo/session/exit", None),
        ("post", "/api/demo/reset", None),
        ("post", "/api/demo/fault-mode", {"fault_mode": "iam_timeout"}),
        ("get", "/api/demo/model-quota", None),
    ):
        request = getattr(client, method)
        response = request(path, json=payload) if payload is not None else request(path)
        assert response.status_code == 404

    assert client.get("/api/workspaces/identity").json()["employee_id"] == "EMP-001"


def test_product_events_never_expose_model_quota_fields(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    token = create_workspace(client)
    with database_session_factory() as session:
        append_workspace_event(
            session,
            workspace_token=token,
            event_type="business.status",
            payload={"status": "answered"},
        )

    response = client.get("/api/events")
    assert response.status_code == 200
    assert all(key not in response.text for key in ("quota", "used", "limit", "remaining"))


def test_demo_model_quota_is_only_available_inside_active_demo_session(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    create_workspace(client)

    inactive = client.get("/api/demo/model-quota")
    client.post("/api/demo/session", json={"employee_id": "EMP-002"})
    active = client.get("/api/demo/model-quota")

    assert inactive.status_code == 404
    assert active.status_code == 200
    assert active.json() == {"used": 0, "limit": 20, "remaining": 20, "retry_consumed": 0}



def test_other_demo_identity_draft_is_not_exposed_or_rebound(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    product_token = create_workspace(client)
    entered = client.post("/api/demo/session", json={"employee_id": "EMP-002"})
    assert entered.status_code == 200
    demo_token = client.cookies.get("accesspilot_workspace")

    preview = client.post(
        "/api/drafts/preview",
        json={
            "employee_id": "EMP-002",
            "entitlement_id": "insighthub.customer_export",
            "duration_days": 14,
            "justification": "季度客户分析",
            "confirmed": False,
        },
    )
    assert preview.status_code == 200

    switched = client.post("/api/demo/session", json={"employee_id": "EMP-003"})
    assert switched.status_code == 200
    assert client.cookies.get("accesspilot_workspace") == demo_token != product_token
    assert client.get("/api/drafts/current").json() == {"draft": None}
    assert client.post("/api/requests").status_code == 409
    assert client.post("/api/demo/session/exit").status_code == 200
    assert client.cookies.get("accesspilot_workspace") == product_token


def test_product_draft_is_hidden_during_demo_and_restored_after_exit(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    create_workspace(client)

    preview = client.post(
        "/api/drafts/preview",
        json={
            "employee_id": "EMP-001",
            "entitlement_id": "insighthub.customer_export",
            "duration_days": 14,
            "justification": "季度客户分析",
            "confirmed": False,
        },
    )
    assert preview.status_code == 200
    original = client.get("/api/drafts/current").json()["draft"]
    assert original["employee_id"] == "EMP-001"

    assert client.post("/api/demo/session", json={"employee_id": "EMP-002"}).status_code == 200
    assert client.get("/api/drafts/current").json() == {"draft": None}

    assert client.post("/api/demo/session/exit").status_code == 200
    assert client.get("/api/drafts/current").json() == {"draft": original}

def test_demo_reset_rotates_workspace_and_clears_events_and_quota(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    product_token = create_workspace(client)
    assert client.post("/api/demo/session", json={"employee_id": "EMP-002"}).status_code == 200
    demo_token = client.cookies.get("accesspilot_workspace")
    assert demo_token is not None and demo_token != product_token
    preview = client.post(
        "/api/drafts/preview",
        json={
            "employee_id": "EMP-002",
            "entitlement_id": "insighthub.customer_export",
            "duration_days": 14,
            "justification": "季度客户分析",
            "confirmed": False,
        },
    )
    assert preview.status_code == 200
    with database_session_factory() as session:
        append_workspace_event(
            session,
            workspace_token=demo_token,
            event_type="business.status",
            payload={"status": "answered"},
        )
        consume_model_call(session, workspace_token=demo_token)

    assert client.get("/api/demo/model-quota").json()["used"] == 1
    reset = client.post("/api/demo/reset")
    new_token = client.cookies.get("accesspilot_workspace")

    assert reset.status_code == 200
    assert new_token is not None
    assert new_token != demo_token
    assert client.get("/api/drafts/current").json() == {"draft": None}
    assert client.get("/api/events").text == ""
    assert client.get("/api/demo/model-quota").json() == {
        "used": 0,
        "limit": 20,
        "remaining": 20,
        "retry_consumed": 0,
    }
    assert client.get("/api/demo/session").json()["employee_id"] == "EMP-002"

    with database_session_factory() as session:
        assert len(list_workspace_events(session, workspace_token=demo_token)) == 1
        old_record = session.scalar(
            select(WorkspaceRecord).where(
                WorkspaceRecord.token_hash == hash_workspace_token(demo_token)
            )
        )
        assert old_record is not None
        assert old_record.model_calls_used == 1
        assert old_record.demo_actor_id is None
        assert old_record.demo_session_active is False
        assert old_record.fault_mode is None


def test_product_and_demo_facts_are_isolated_across_cookie_switches(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    product_token = create_workspace(client)
    product_preview = client.post(
        "/api/drafts/preview",
        json={
            "employee_id": "EMP-001",
            "entitlement_id": "insighthub.customer_export",
            "duration_days": 14,
            "justification": "产品事实隔离回归",
            "confirmed": True,
        },
    )
    assert product_preview.status_code == 200
    product_request = client.post("/api/requests")
    assert product_request.status_code == 201
    product_request_id = product_request.json()["request_id"]
    with database_session_factory() as session:
        append_workspace_event(
            session,
            workspace_token=product_token,
            event_type="message.assistant",
            payload={"content": "product-only-fact"},
        )

    entered = client.post("/api/demo/session", json={"employee_id": "EMP-002"})
    assert entered.status_code == 200
    demo_token = client.cookies.get("accesspilot_workspace")
    assert demo_token is not None and demo_token != product_token
    assert "product-only-fact" not in client.get("/api/events").text
    assert client.get(f"/api/requests/{product_request_id}").status_code == 404
    with database_session_factory() as session:
        append_workspace_event(
            session,
            workspace_token=demo_token,
            event_type="message.assistant",
            payload={"content": "demo-only-fact"},
        )
    assert "demo-only-fact" in client.get("/api/events").text

    demo_preview = client.post(
        "/api/drafts/preview",
        json={
            "employee_id": "EMP-002",
            "entitlement_id": "insighthub.customer_export",
            "duration_days": 14,
            "justification": "Demo 事实隔离回归",
            "confirmed": True,
        },
    )
    assert demo_preview.status_code == 200
    demo_request = client.post("/api/requests")
    assert demo_request.status_code == 201
    demo_request_id = demo_request.json()["request_id"]

    exited = client.post("/api/demo/session/exit")
    assert exited.status_code == 200
    assert client.cookies.get("accesspilot_workspace") == product_token
    product_events = client.get("/api/events").text
    assert "product-only-fact" in product_events
    assert "demo-only-fact" not in product_events
    assert client.get(f"/api/requests/{demo_request_id}").status_code == 404
