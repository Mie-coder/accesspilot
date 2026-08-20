"""T16 后端业务卡片 API 的红测合同。

这些测试只验证事实源与类型化边界；前端卡片不应从聊天自由文本推断状态。
T16 实现前，三个新路由应先因不存在而失败。
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.config import Settings
from accesspilot.db.models import (
    AccessGrantRecord,
    AccessRequestRecord,
    AuditEventRecord,
    ProvisioningAttemptRecord,
    WorkspaceRecord,
)
from accesspilot.db.seed import seed_catalog
from accesspilot.db.workspace_store import (
    SqlAlchemyWorkspaceStore,
    hash_workspace_token,
)
from accesspilot.main import create_app
from support.auth import login_as


def build_client(
    database_session_factory: sessionmaker[Session],
) -> TestClient:
    """使用真实 SQLAlchemy Workspace，确保卡片读取的是持久化事实。"""

    with database_session_factory() as session:
        seed_catalog(session)
    return TestClient(
        create_app(
            settings=Settings(
                database_url="postgresql+psycopg://unused",
                demo_mode_enabled=True,
            ),
            store=SqlAlchemyWorkspaceStore(database_session_factory),
            session_factory=database_session_factory,
        )
    )


def create_workspace(client: TestClient) -> str:
    login_as(client)
    token = client.cookies.get("accesspilot_session")
    assert token is not None
    return token


def load_workspace(
    database_session_factory: sessionmaker[Session],
    token: str,
) -> WorkspaceRecord:
    with database_session_factory() as session:
        workspace = session.scalar(
            select(WorkspaceRecord).where(
                WorkspaceRecord.token_hash == hash_workspace_token(token)
            )
        )
        assert workspace is not None
        return workspace


def add_request(
    session: Session,
    *,
    workspace: WorkspaceRecord,
    requester_id: str,
    entitlement_code: str,
    request_status: str = "submitted",
    created_at: datetime | None = None,
) -> AccessRequestRecord:
    timestamp = created_at or datetime.now(UTC)
    request = AccessRequestRecord(
        workspace_id=workspace.id,
        requester_id=requester_id,
        entitlement_code=entitlement_code,
        duration_days=14,
        justification="T16 业务卡片验收",
        request_status=request_status,
        confirmed_at=timestamp,
        created_at=timestamp,
    )
    session.add(request)
    session.flush()
    return request


def add_grant(
    session: Session,
    *,
    workspace: WorkspaceRecord,
    requester_id: str,
    entitlement_code: str,
    starts_at: datetime,
    expires_at: datetime,
) -> tuple[AccessRequestRecord, AccessGrantRecord]:
    request = add_request(
        session,
        workspace=workspace,
        requester_id=requester_id,
        entitlement_code=entitlement_code,
        request_status="approved",
        created_at=starts_at,
    )
    grant = AccessGrantRecord(
        workspace_id=workspace.id,
        request_id=request.id,
        idempotency_key=f"t16-{uuid4()}",
        starts_at=starts_at,
        expires_at=expires_at,
    )
    session.add(grant)
    session.flush()
    return request, grant


def item_for_code(payload: dict[str, object], code: str) -> dict[str, object]:
    items = payload["items"]
    assert isinstance(items, list)
    allowed_states = {
        "eligible",
        "owned",
        "pending",
        "expiring_soon",
        "expired",
    }
    for item in items:
        assert isinstance(item, dict)
        assert item.get("state") in allowed_states
        if item.get("code") == code:
            return item
    raise AssertionError(f"overview item not found: {code}")


def test_access_overview_requires_workspace_cookie(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)

    response = client.get("/api/access-overview")

    assert response.status_code == 401


def test_access_overview_isolated_to_current_workspace_and_actor(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    token = create_workspace(client)
    current_workspace = load_workspace(database_session_factory, token)

    other_client = build_client(database_session_factory)
    other_token = create_workspace(other_client)
    other_workspace = load_workspace(database_session_factory, other_token)

    with database_session_factory() as session:
        current = session.get(WorkspaceRecord, current_workspace.id)
        other = session.get(WorkspaceRecord, other_workspace.id)
        assert current is not None
        assert other is not None
        _, current_grant = add_grant(
            session,
            workspace=current,
            requester_id="EMP-001",
            entitlement_code="insighthub.dashboard_view",
            starts_at=datetime.now(UTC) - timedelta(days=1),
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        other_request, other_grant = add_grant(
            session,
            workspace=other,
            requester_id="EMP-002",
            entitlement_code="insighthub.customer_export",
            starts_at=datetime.now(UTC) - timedelta(days=1),
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        session.commit()

    response = client.get("/api/access-overview")

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, dict)
    assert isinstance(payload.get("items"), list)
    current_item = item_for_code(payload, "insighthub.dashboard_view")
    assert current_item["state"] == "owned"
    serialized = response.text
    assert str(current_grant.id) in serialized
    assert str(other_grant.id) not in serialized
    assert str(other_request.id) not in serialized
    assert "EMP-002" not in serialized


@pytest.mark.parametrize(
    ("fact", "entitlement_code", "expected_state"),
    [
        ("owned", "codeforge.repo_read", "owned"),
        ("expiring_soon", "insighthub.customer_export", "expiring_soon"),
        ("expired", "insighthub.dashboard_view", "expired"),
        ("pending", "insighthub.dashboard_view", "pending"),
    ],
)
def test_access_overview_classifies_authoritative_lifecycle_states(
    database_session_factory: sessionmaker[Session],
    fact: str,
    entitlement_code: str,
    expected_state: str,
) -> None:
    client = build_client(database_session_factory)
    token = create_workspace(client)
    workspace = load_workspace(database_session_factory, token)
    now = datetime.now(UTC)

    with database_session_factory() as session:
        persisted_workspace = session.get(WorkspaceRecord, workspace.id)
        assert persisted_workspace is not None
        if fact == "owned":
            add_grant(
                session,
                workspace=persisted_workspace,
                requester_id="EMP-001",
                entitlement_code=entitlement_code,
                starts_at=now - timedelta(days=1),
                expires_at=now + timedelta(days=30),
            )
        elif fact == "expiring_soon":
            add_grant(
                session,
                workspace=persisted_workspace,
                requester_id="EMP-001",
                entitlement_code=entitlement_code,
                starts_at=now - timedelta(days=1),
                expires_at=now + timedelta(days=6),
            )
        elif fact == "expired":
            add_grant(
                session,
                workspace=persisted_workspace,
                requester_id="EMP-001",
                entitlement_code=entitlement_code,
                starts_at=now - timedelta(days=14),
                expires_at=now - timedelta(days=1),
            )
        else:
            add_request(
                session,
                workspace=persisted_workspace,
                requester_id="EMP-001",
                entitlement_code=entitlement_code,
                request_status="submitted",
            )
        session.commit()

    response = client.get("/api/access-overview")

    assert response.status_code == 200
    item = item_for_code(response.json(), entitlement_code)
    assert item["state"] == expected_state
    if fact == "pending":
        assert item.get("grant_id") is None
        assert item.get("access_granted") is not True


def test_access_overview_uses_empty_state_when_actor_has_no_eligible_access(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    create_workspace(client)
    admin_client = TestClient(client.app)
    login_as(admin_client, "EMP-004")

    response = admin_client.get("/api/access-overview")

    assert response.status_code == 200
    assert response.json() == {"items": []}


def test_pending_renewal_is_not_hidden_by_expired_grant_history(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    token = create_workspace(client)
    workspace = load_workspace(database_session_factory, token)
    now = datetime.now(UTC)

    with database_session_factory() as session:
        persisted_workspace = session.get(WorkspaceRecord, workspace.id)
        assert persisted_workspace is not None
        add_grant(
            session,
            workspace=persisted_workspace,
            requester_id="EMP-001",
            entitlement_code="insighthub.dashboard_view",
            starts_at=now - timedelta(days=30),
            expires_at=now - timedelta(days=1),
        )
        pending = add_request(
            session,
            workspace=persisted_workspace,
            requester_id="EMP-001",
            entitlement_code="insighthub.dashboard_view",
            request_status="submitted",
            created_at=now,
        )
        session.commit()

    response = client.get("/api/access-overview")

    assert response.status_code == 200
    matching = [
        item
        for item in response.json()["items"]
        if item["code"] == "insighthub.dashboard_view"
    ]
    assert [item["state"] for item in matching] == ["pending", "expired"]
    pending_item = next(item for item in matching if item["state"] == "pending")
    assert pending_item["request_id"] == str(pending.id)
    assert all(item["state"] != "eligible" for item in matching)


def test_future_grant_is_pending_with_authoritative_grant_fact(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    token = create_workspace(client)
    workspace = load_workspace(database_session_factory, token)
    now = datetime.now(UTC)

    with database_session_factory() as session:
        persisted_workspace = session.get(WorkspaceRecord, workspace.id)
        assert persisted_workspace is not None
        request, grant = add_grant(
            session,
            workspace=persisted_workspace,
            requester_id="EMP-001",
            entitlement_code="insighthub.dashboard_view",
            starts_at=now + timedelta(days=2),
            expires_at=now + timedelta(days=30),
        )
        session.commit()

    response = client.get("/api/access-overview")

    assert response.status_code == 200
    matching = [
        item
        for item in response.json()["items"]
        if item["code"] == "insighthub.dashboard_view"
    ]
    assert [item["state"] for item in matching] == ["pending"]
    assert matching[0]["request_id"] == str(request.id)
    assert matching[0]["grant_id"] == str(grant.id)
    assert matching[0]["starts_at"] is not None
    assert matching[0]["next_step"] == "等待授权生效"


def test_older_non_terminal_request_remains_pending_after_newer_cancelled_request(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    token = create_workspace(client)
    workspace = load_workspace(database_session_factory, token)
    now = datetime.now(UTC)

    with database_session_factory() as session:
        persisted_workspace = session.get(WorkspaceRecord, workspace.id)
        assert persisted_workspace is not None
        earlier = add_request(
            session,
            workspace=persisted_workspace,
            requester_id="EMP-001",
            entitlement_code="insighthub.dashboard_view",
            request_status="submitted",
            created_at=now - timedelta(days=1),
        )
        add_request(
            session,
            workspace=persisted_workspace,
            requester_id="EMP-001",
            entitlement_code="insighthub.dashboard_view",
            request_status="cancelled",
            created_at=now,
        )
        session.commit()

    response = client.get("/api/access-overview")

    assert response.status_code == 200
    matching = [
        item
        for item in response.json()["items"]
        if item["code"] == "insighthub.dashboard_view"
    ]
    assert [item["state"] for item in matching] == ["pending"]
    assert matching[0]["request_id"] == str(earlier.id)


def test_request_detail_projects_only_safe_provisioning_and_audit_fields(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    token = create_workspace(client)
    workspace = load_workspace(database_session_factory, token)
    secret = "sk-live-t16-secret"
    internal_url = "http://10.0.0.8:8080/internal/iam"
    idempotency_value = f"t16-idempotency-secret-{uuid4()}"

    with database_session_factory() as session:
        persisted_workspace = session.get(WorkspaceRecord, workspace.id)
        assert persisted_workspace is not None
        request = add_request(
            session,
            workspace=persisted_workspace,
            requester_id="EMP-001",
            entitlement_code="insighthub.dashboard_view",
            request_status="submitted",
        )
        session.add(
            ProvisioningAttemptRecord(
                workspace_id=persisted_workspace.id,
                request_id=request.id,
                idempotency_key=idempotency_value,
                provisioning_status="failed",
                attempt_count=2,
                last_error=f"{secret}; retry at {internal_url}",
            )
        )
        session.add(
            AuditEventRecord(
                workspace_id=persisted_workspace.id,
                request_id=request.id,
                actor_type="system",
                actor_id=None,
                event_type="provisioning.failed",
                details={
                    "status": "failed",
                    "next_step": "联系人工流程",
                    "approver_role": "manager",
                    "idempotency_key": idempotency_value,
                    "internal_url": internal_url,
                    "password": secret,
                },
            )
        )
        session.commit()

    response = client.get(f"/api/requests/{request.id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["provisioning"]["last_error"] == (
        "权限开通失败，请稍后重试或联系人工流程。"
    )
    assert payload["audit_events"][0]["details"] == {
        "status": "failed",
        "next_step": "联系人工流程",
        "approver_role": "manager",
    }
    serialized = response.text
    assert secret not in serialized
    assert internal_url not in serialized
    assert idempotency_value not in serialized


def test_request_detail_projects_risk_free_text_without_leaking_model_content(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    token = create_workspace(client)
    workspace = load_workspace(database_session_factory, token)
    secret = f"sk-live-risk-{uuid4()}"
    internal_url = "http://10.0.0.9:8080/internal/risk"

    with database_session_factory() as session:
        persisted_workspace = session.get(WorkspaceRecord, workspace.id)
        assert persisted_workspace is not None
        request = add_request(
            session,
            workspace=persisted_workspace,
            requester_id="EMP-001",
            entitlement_code="insighthub.dashboard_view",
        )
        session.add(
            AuditEventRecord(
                workspace_id=persisted_workspace.id,
                request_id=request.id,
                actor_type="risk_agent",
                actor_id=None,
                event_type="risk_review.completed",
                details={
                    "risk_level": "low",
                    "outcome": "clear",
                    "summary": f"SYSTEM_PROMPT {secret} {internal_url}",
                    "findings": [
                        f"隐藏推理：{secret}",
                        f"正常字段但含内部地址 {internal_url}",
                    ],
                    "citations": [
                        {
                            "policy_code": "POL-001",
                            "reason": f"api key={secret}",
                        }
                    ],
                },
            )
        )
        session.commit()

    response = client.get(f"/api/requests/{request.id}")

    assert response.status_code == 200
    payload = response.json()
    risk = payload["risk_review"]
    assert risk["summary"] == "风险审查结果已生成，详情按政策事实展示。"
    assert risk["findings"] == []
    citation = risk["citations"][0]
    assert citation["policy_code"] == "POL-001"
    assert citation["title"] == "申请字段完整性"
    assert "权限申请必须包含申请人" in citation["content"]
    assert citation["reason"] == "风险审查引用已由政策事实源校验。"
    serialized = response.text
    assert secret not in serialized
    assert internal_url not in serialized
    assert "SYSTEM_PROMPT" not in serialized


def test_entitlement_resolution_requires_strict_query_body_and_workspace(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)

    no_cookie = client.post("/api/entitlements/resolve", json={"query": "InsightHub"})
    assert no_cookie.status_code == 401

    create_workspace(client)
    extra = client.post(
        "/api/entitlements/resolve",
        json={"query": "InsightHub", "employee_id": "EMP-999"},
    )
    non_string = client.post("/api/entitlements/resolve", json={"query": 7})
    blank = client.post("/api/entitlements/resolve", json={"query": "  "})

    assert extra.status_code == 422
    assert non_string.status_code == 422
    assert blank.status_code == 422


def test_entitlement_resolution_returns_typed_ambiguous_candidates_without_draft_mutation(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    create_workspace(client)
    preview = client.post(
        "/api/drafts/preview",
        json={
            "entitlement_id": "codeforge.repo_read",
            "duration_days": 14,
            "justification": "原有申请事实",
            "confirmed": True,
        },
    )
    assert preview.status_code == 200
    snapshot = preview.json()["draft"]
    assert snapshot["confirmed"] is True

    response = client.post(
        "/api/entitlements/resolve",
        json={"query": "InsightHub"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ambiguous"
    assert payload["target_field"] == "entitlement_id"
    assert payload["query"] == "InsightHub"
    assert [candidate["code"] for candidate in payload["candidates"]] == [
        "insighthub.customer_export",
        "insighthub.dashboard_view",
    ]
    assert all(
        {"code", "name", "system_code", "system_name", "risk_level"}
        <= candidate.keys()
        for candidate in payload["candidates"]
    )
    assert client.get("/api/drafts/current").json() == {
        "draft": snapshot,
        "draft_revision": 1,
    }


def test_selected_entitlement_is_revalidated_and_invalidates_old_confirmation(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    create_workspace(client)
    original = client.post(
        "/api/drafts/preview",
        json={
            "entitlement_id": "insighthub.customer_export",
            "duration_days": 14,
            "justification": "原有申请事实",
            "confirmed": True,
        },
    )
    assert original.status_code == 200
    assert original.json()["draft"]["confirmed"] is True

    resolution = client.post(
        "/api/entitlements/resolve",
        json={"query": "InsightHub"},
    )
    assert resolution.status_code == 200
    candidates = resolution.json()["candidates"]
    selected_code = next(
        candidate["code"]
        for candidate in candidates
        if candidate["code"] == "insighthub.dashboard_view"
    )

    revalidated = client.post(
        "/api/drafts/preview",
        json={
            "entitlement_id": selected_code,
            "duration_days": 14,
            "justification": "原有申请事实",
            "confirmed": True,
        },
    )

    assert revalidated.status_code == 200
    payload = revalidated.json()
    assert payload["draft"]["entitlement_id"] == selected_code
    assert payload["draft"]["confirmed"] is False
    assert payload["can_enter_approval"] is False


def test_latest_request_requires_session_and_ignores_other_requesters(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    no_cookie = client.get("/api/requests/latest")
    assert no_cookie.status_code == 401

    token = create_workspace(client)
    workspace = load_workspace(database_session_factory, token)
    baseline = client.get("/api/requests/latest")
    assert baseline.status_code == 200
    baseline_detail = baseline.json()["request"]
    with database_session_factory() as session:
        persisted_workspace = session.get(WorkspaceRecord, workspace.id)
        assert persisted_workspace is not None
        unrelated_request = add_request(
            session,
            workspace=persisted_workspace,
            requester_id="EMP-002",
            entitlement_code="insighthub.dashboard_view",
        )
        session.commit()

    response = client.get("/api/requests/latest")

    assert response.status_code == 200
    detail = response.json()["request"]
    # A persistent test database may already contain EMP-001's requests.  The
    # requester-only latest endpoint must remain stable when an unrelated
    # EMP-002 case is inserted into the same source workspace.
    assert detail == baseline_detail
    if detail is not None:
        assert detail["request"]["requester_id"] == "EMP-001"
        assert detail["request"]["request_id"] != str(unrelated_request.id)


def test_latest_request_is_scoped_to_current_actor_and_returns_latest_fact(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    token = create_workspace(client)
    workspace = load_workspace(database_session_factory, token)
    now = datetime.now(UTC)

    with database_session_factory() as session:
        persisted_workspace = session.get(WorkspaceRecord, workspace.id)
        assert persisted_workspace is not None
        latest_created_at = session.scalar(select(func.max(AccessRequestRecord.created_at)))
        assert latest_created_at is not None
        current_request = add_request(
            session,
            workspace=persisted_workspace,
            requester_id="EMP-001",
            entitlement_code="codeforge.repo_read",
            created_at=latest_created_at + timedelta(microseconds=1),
        )
        other_request = add_request(
            session,
            workspace=persisted_workspace,
            requester_id="EMP-002",
            entitlement_code="insighthub.customer_export",
            created_at=latest_created_at + timedelta(microseconds=2),
        )
        session.commit()
    try:
        response = client.get("/api/requests/latest")

        assert response.status_code == 200
        payload = response.json()
        detail = payload["request"]
        assert detail is not None
        request = detail["request"]
        assert request["request_id"] == str(current_request.id)
        assert request["requester_id"] == "EMP-001"
        assert request["request_id"] != str(other_request.id)
        assert request["entitlement_code"] == "codeforge.repo_read"
        assert detail["audit_events"] == []
    finally:
        # Keep the shared integration database repeatable: the temporary
        # ordering timestamps must not outrank later tests or future runs.
        with database_session_factory() as session:
            stored_current = session.get(AccessRequestRecord, current_request.id)
            stored_other = session.get(AccessRequestRecord, other_request.id)
            assert stored_current is not None and stored_other is not None
            stored_current.created_at = now
            stored_other.created_at = now
            session.commit()
