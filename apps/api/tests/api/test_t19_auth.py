"""T19 authentication/session boundary tests.

These tests intentionally describe the v1.2 contract before the implementation
exists.  They cover the observable login cutover rather than implementation
details of the legacy demo workspace.
"""

from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.auth import hash_secret
from accesspilot.config import Settings
from accesspilot.db.models import AuthSessionRecord, EmployeeRecord, WorkspaceRecord
from accesspilot.db.seed import seed_catalog
from accesspilot.db.workspace_store import SqlAlchemyWorkspaceStore, hash_workspace_token
from accesspilot.domain.models import RequestDraft
from accesspilot.main import create_app
from accesspilot.workspaces import CursorConflictError, WorkspaceService

ORIGIN = "http://testserver"


def build_client(
    database_session_factory: sessionmaker[Session],
) -> TestClient:
    with database_session_factory() as session:
        seed_catalog(session)
    return TestClient(
        create_app(
            settings=Settings(
                database_url="postgresql+psycopg://unused",
                web_origin=ORIGIN,
            ),
            session_factory=database_session_factory,
        )
    )


def login(client: TestClient, account_id: str = "EMP-001") -> dict[str, object]:
    response = client.post(
        "/api/auth/login",
        json={"account_id": account_id},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200
    return response.json()


def test_login_allowlist_creates_isolated_hashed_session_and_workspace(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    preview_schema = client.app.openapi()["components"]["schemas"]["DraftPreviewBody"]
    assert "employee_id" not in preview_schema["properties"]

    payload = login(client)

    assert payload["principal"]["employee_id"] == "EMP-001"
    assert isinstance(payload["csrf_token"], str)
    session_cookie = client.cookies.get("accesspilot_session")
    assert session_cookie is not None
    with database_session_factory() as session:
        auth_session = session.scalar(
            select(AuthSessionRecord).where(
                AuthSessionRecord.token_hash == hash_workspace_token(session_cookie)
            )
        )
        assert auth_session is not None
        assert auth_session.token_hash == hash_workspace_token(session_cookie)
        assert auth_session.token_hash != session_cookie
        assert auth_session.csrf_hash != payload["csrf_token"]
        workspace = session.get(WorkspaceRecord, auth_session.workspace_id)
        assert workspace is not None
        assert workspace.actor_id == "EMP-001"
        assert auth_session.employee_id == "EMP-001"
        assert auth_session.expires_at > datetime.now(UTC)


def test_employee_outside_login_allowlist_is_rejected_even_if_seeded(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    with database_session_factory() as session:
        if session.get(EmployeeRecord, "EMP-005") is None:
            session.add(
                EmployeeRecord(
                    employee_id="EMP-005",
                    name="未授权员工",
                    department="security",
                    manager_id=None,
                    roles=["requester"],
                )
            )
        session.commit()

    response = client.post(
        "/api/auth/login",
        json={"account_id": "EMP-005"},
        headers={"Origin": ORIGIN},
    )

    assert response.status_code == 422
    with database_session_factory() as session:
        assert session.scalar(
            select(AuthSessionRecord).where(AuthSessionRecord.employee_id == "EMP-005")
        ) is None


def test_session_refresh_rotates_csrf_but_not_principal(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    first = login(client)
    first_csrf = first["csrf_token"]

    refreshed = client.get("/api/auth/session")

    assert refreshed.status_code == 200
    assert refreshed.json()["principal"] == first["principal"]
    assert refreshed.json()["csrf_token"] != first_csrf
    assert client.cookies.get("accesspilot_session") is not None
    old_csrf_write = client.post(
        "/api/drafts/preview",
        json={},
        headers={"Origin": ORIGIN, "X-CSRF-Token": first_csrf},
    )
    new_csrf = refreshed.json()["csrf_token"]
    new_csrf_write = client.post(
        "/api/drafts/preview",
        json={},
        headers={"Origin": ORIGIN, "X-CSRF-Token": new_csrf},
    )
    assert old_csrf_write.status_code == 403
    assert new_csrf_write.status_code == 200
    with database_session_factory() as session:
        cookie = client.cookies.get("accesspilot_session")
        assert cookie is not None
        auth_session = session.scalar(
            select(AuthSessionRecord).where(
                AuthSessionRecord.token_hash == hash_workspace_token(cookie)
            )
        )
        assert auth_session is not None
        assert auth_session.csrf_hash == hash_secret(new_csrf)
        assert auth_session.csrf_hash != hash_secret(first_csrf)


def test_business_route_requires_session_and_legacy_workspace_cookie_is_not_authority(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    client.cookies.set("accesspilot_workspace", "legacy-token")

    response = client.get("/api/drafts/current")

    assert response.status_code == 401


def test_old_workspace_and_demo_entrypoints_are_closed(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    for method, path in (
        ("post", "/api/workspaces"),
        ("post", "/api/workspaces/ensure"),
        ("get", "/api/workspaces/identity"),
        ("post", "/api/demo/session"),
        ("get", "/api/demo/session"),
        ("post", "/api/demo/session/exit"),
        ("post", "/api/demo/reset"),
        ("post", "/api/demo/fault-mode"),
        ("get", "/api/demo/model-quota"),
    ):
        response = getattr(client, method)(path)
        assert response.status_code == 404, (method, path, response.text)


def test_wrong_origin_or_csrf_is_rejected_before_draft_side_effect(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    payload = login(client)
    with database_session_factory() as session:
        session_cookie = client.cookies.get("accesspilot_session")
        assert session_cookie is not None
        auth_session = session.scalar(
            select(AuthSessionRecord).where(
                AuthSessionRecord.token_hash == hash_workspace_token(session_cookie)
            )
        )
        assert auth_session is not None
        before = session.get(WorkspaceRecord, auth_session.workspace_id)
        assert before is not None
        before_revision = before.draft_revision

    wrong_origin = client.post(
        "/api/drafts/preview",
        json={"entitlement_id": "insighthub.customer_export"},
        headers={"Origin": "https://evil.example", "X-CSRF-Token": payload["csrf_token"]},
    )
    missing_csrf = client.post(
        "/api/drafts/preview",
        json={"entitlement_id": "insighthub.customer_export"},
        headers={"Origin": ORIGIN},
    )

    assert wrong_origin.status_code == 403
    assert missing_csrf.status_code == 403
    with database_session_factory() as session:
        after = session.get(WorkspaceRecord, before.id)
        assert after is not None
        assert after.draft_revision == before_revision
        assert after.draft is None


def test_login_rejects_identity_injection_fields_without_creating_session(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)

    with database_session_factory() as session:
        before_sessions = len(session.scalars(select(AuthSessionRecord)).all())
        before_workspaces = len(session.scalars(select(WorkspaceRecord)).all())

    response = client.post(
        "/api/auth/login",
        json={"account_id": "EMP-001", "role": "permissions_admin"},
        headers={"Origin": ORIGIN},
    )

    assert response.status_code == 422
    form_identity = client.post(
        "/api/auth/login",
        data={"account_id": "EMP-001", "employee_id": "EMP-002"},
        headers={"Origin": "https://evil.example"},
    )
    multipart_identity = client.post(
        "/api/auth/login",
        files={"account_id": (None, "EMP-001"), "employee_id": (None, "EMP-002")},
        headers={"Origin": "https://evil.example"},
    )
    assert form_identity.status_code == 422
    assert multipart_identity.status_code == 422
    for origin_headers in ({}, {"Origin": "http://localhost:5173"}, {"Origin": "https://evil.example"}):
        origin_response = client.post(
            "/api/auth/login",
            json={"account_id": "EMP-001"},
            headers=origin_headers,
        )
        assert origin_response.status_code == 403
    with database_session_factory() as session:
        assert len(session.scalars(select(AuthSessionRecord)).all()) == before_sessions
        assert len(session.scalars(select(WorkspaceRecord)).all()) == before_workspaces


def test_four_mock_accounts_use_independent_sessions_and_private_workspaces(
    database_session_factory: sessionmaker[Session],
) -> None:
    """Every fixed account gets a separate server-owned session/workspace pair."""

    clients = [build_client(database_session_factory) for _ in range(4)]
    payloads = [login(client, account_id) for client, account_id in zip(
        clients, ("EMP-001", "EMP-002", "EMP-003", "EMP-004"), strict=True
    )]
    session_cookies = [client.cookies.get("accesspilot_session") for client in clients]

    assert len(set(session_cookies)) == 4
    assert [payload["principal"]["employee_id"] for payload in payloads] == [
        "EMP-001",
        "EMP-002",
        "EMP-003",
        "EMP-004",
    ]
    assert [client.get("/api/drafts/current").status_code for client in clients] == [
        200,
        200,
        200,
        200,
    ]
    # A write path must carry the session context through WorkspaceService;
    # otherwise the legacy normalizer rewrites EMP-002 back to EMP-001.
    preview = clients[1].post(
        "/api/drafts/preview",
        json={},
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": payloads[1]["csrf_token"],
        },
    )
    assert preview.status_code == 200
    current_second = clients[1].get("/api/drafts/current")
    assert current_second.status_code == 200
    assert current_second.json()["draft"]["employee_id"] == "EMP-002"
    with database_session_factory() as session:
        rows = [
            session.scalar(
                select(AuthSessionRecord).where(
                    AuthSessionRecord.token_hash == hash_workspace_token(cookie)
                )
            )
            for cookie in session_cookies
        ]
        assert all(row is not None for row in rows)
        assert len({row.workspace_id for row in rows if row is not None}) == 4
        assert len({row.employee_id for row in rows if row is not None}) == 4

    logout_response = clients[0].post(
        "/api/auth/logout",
        headers={"Origin": ORIGIN, "X-CSRF-Token": payloads[0]["csrf_token"]},
    )
    assert logout_response.status_code == 200
    assert clients[0].get("/api/auth/session").status_code == 401
    assert [client.get("/api/auth/session").status_code for client in clients[1:]] == [
        200,
        200,
        200,
    ]


def test_login_cookie_flags_are_bounded_and_csrf_is_not_http_only(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)

    response = client.post(
        "/api/auth/login",
        json={"account_id": "EMP-001"},
        headers={"Origin": ORIGIN},
    )
    cookies = response.headers.get_list("set-cookie")
    session_cookie = next(value for value in cookies if value.startswith("accesspilot_session="))
    csrf_cookie = next(value for value in cookies if value.startswith("accesspilot_csrf="))

    assert "HttpOnly" in session_cookie
    assert "HttpOnly" not in csrf_cookie
    assert "SameSite=lax" in session_cookie
    assert "Secure" not in session_cookie
    assert "Max-Age=28800" in session_cookie
    assert "Path=/" in session_cookie


def test_logout_revokes_only_session_and_keeps_workspace(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    payload = login(client)
    cookie = client.cookies.get("accesspilot_session")
    assert cookie is not None
    with database_session_factory() as session:
        auth_session = session.scalar(
            select(AuthSessionRecord).where(
                AuthSessionRecord.token_hash == hash_workspace_token(cookie)
            )
        )
        assert auth_session is not None
        workspace_id = auth_session.workspace_id
        session_id = auth_session.id

    response = client.post(
        "/api/auth/logout",
        headers={"Origin": ORIGIN, "X-CSRF-Token": payload["csrf_token"]},
    )
    assert response.status_code == 200
    assert client.get("/api/auth/session").status_code == 401
    with database_session_factory() as session:
        row = session.get(AuthSessionRecord, session_id)
        assert row is not None and row.revoked_at is not None
        assert session.get(WorkspaceRecord, workspace_id) is not None


def test_expired_revoked_and_tampered_session_tokens_are_unauthorized(
    database_session_factory: sessionmaker[Session],
) -> None:
    expired_client = build_client(database_session_factory)
    login(expired_client)
    expired_cookie = expired_client.cookies.get("accesspilot_session")
    assert expired_cookie is not None
    with database_session_factory() as session:
        row = session.scalar(
            select(AuthSessionRecord).where(
                AuthSessionRecord.token_hash == hash_workspace_token(expired_cookie)
            )
        )
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    assert expired_client.get("/api/auth/session").status_code == 401

    revoked_client = build_client(database_session_factory)
    login(revoked_client)
    revoked_cookie = revoked_client.cookies.get("accesspilot_session")
    assert revoked_cookie is not None
    with database_session_factory() as session:
        row = session.scalar(
            select(AuthSessionRecord).where(
                AuthSessionRecord.token_hash == hash_workspace_token(revoked_cookie)
            )
        )
        assert row is not None
        row.revoked_at = datetime.now(UTC)
        session.commit()
    assert revoked_client.get("/api/auth/session").status_code == 401

    tampered_client = build_client(database_session_factory)
    login(tampered_client)
    tampered_client.cookies.set("accesspilot_session", token_urlsafe(48))
    assert tampered_client.get("/api/auth/session").status_code == 401


@pytest.mark.parametrize(
    ("method", "path", "kwargs", "headers"),
    [
        ("get", "/api/drafts/current?employee_id=EMP-002", {}, {}),
        (
            "post",
            "/api/drafts/preview?role=manager",
            {"json": {"entitlement_id": "insighthub.customer_export"}},
            {},
        ),
        (
            "post",
            "/api/auth/login",
            {"json": {"account_id": "EMP-001", "employee_id": "EMP-002"}},
            {"Origin": ORIGIN},
        ),
        (
            "post",
            "/api/auth/login",
            {"files": {"account_id": (None, "EMP-001"), "employee_id": (None, "EMP-002")}},
            {"Origin": ORIGIN},
        ),
    ],
)
def test_reserved_identity_injection_precedes_origin_and_session_checks(
    database_session_factory: sessionmaker[Session],
    method: str,
    path: str,
    kwargs: dict[str, object],
    headers: dict[str, str],
) -> None:
    client = build_client(database_session_factory)
    response = getattr(client, method)(path, headers=headers, **kwargs)

    assert response.status_code == 422


def test_reserved_identity_header_is_rejected_before_origin_and_session_checks(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)

    response = client.get(
        "/api/drafts/current",
        headers={"X-Role": "requester"},
    )

    assert response.status_code == 422


def test_authenticated_identity_scanner_rejects_form_nested_and_reserved_headers(
    database_session_factory: sessionmaker[Session],
) -> None:
    """No alternate body/header encoding can revoke or mutate a session."""

    client = build_client(database_session_factory)
    payload = login(client)
    cookie = client.cookies.get("accesspilot_session")
    assert cookie is not None
    with database_session_factory() as session:
        auth_session = session.scalar(
            select(AuthSessionRecord).where(
                AuthSessionRecord.token_hash == hash_workspace_token(cookie)
            )
        )
        assert auth_session is not None
        workspace = session.get(WorkspaceRecord, auth_session.workspace_id)
        assert workspace is not None
        workspace_id = workspace.id
        before_revision = workspace.draft_revision

    auth_headers = {
        "Origin": ORIGIN,
        "X-CSRF-Token": payload["csrf_token"],
    }
    form_logout = client.post(
        "/api/auth/logout",
        data={"employee_id": "EMP-002"},
        headers=auth_headers,
    )
    multipart_logout = client.post(
        "/api/auth/logout",
        files={"employee_id": (None, "EMP-002")},
        headers=auth_headers,
    )
    nested_json = client.post(
        "/api/drafts/preview",
        json={"metadata": {"employee_id": "EMP-002"}},
        headers=auth_headers,
    )
    merge_patch_nested_json = client.post(
        "/api/drafts/preview",
        content=b'{"metadata":{"employee_id":"EMP-002"}}',
        headers={
            **auth_headers,
            "Content-Type": "application/merge-patch+json",
        },
    )
    reserved_header = client.get(
        "/api/drafts/current",
        headers={"X-Auth-Session-Id": "forged-session"},
    )
    non_login_account = client.post(
        "/api/drafts/preview",
        json={"account_id": "EMP-002"},
        headers=auth_headers,
    )
    camel_json = client.post(
        "/api/auth/logout",
        json={"employeeId": "EMP-002"},
        headers=auth_headers,
    )
    camel_header = client.get(
        "/api/drafts/current",
        headers={"X-EmployeeId": "EMP-002"},
    )
    nonidentity_multipart_wrong_origin = client.post(
        "/api/drafts/preview",
        files={"content": (None, "x")},
        headers={
            "Origin": "https://evil.example",
            "X-CSRF-Token": payload["csrf_token"],
        },
    )
    nonidentity_form_correct = client.post(
        "/api/drafts/preview",
        data={"content": "x"},
        headers=auth_headers,
    )

    assert [
        form_logout.status_code,
        multipart_logout.status_code,
        nested_json.status_code,
        merge_patch_nested_json.status_code,
        reserved_header.status_code,
        non_login_account.status_code,
        camel_json.status_code,
        camel_header.status_code,
        nonidentity_multipart_wrong_origin.status_code,
        nonidentity_form_correct.status_code,
    ] == [422] * 8 + [403, 422]
    with database_session_factory() as session:
        auth_session = session.scalar(
            select(AuthSessionRecord).where(
                AuthSessionRecord.token_hash == hash_workspace_token(cookie)
            )
        )
        assert auth_session is not None
        assert auth_session.revoked_at is None
        workspace = session.get(WorkspaceRecord, workspace_id)
        assert workspace is not None
        assert workspace.draft_revision == before_revision


def test_unauthenticated_write_returns_401_before_origin_or_csrf_checks(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)

    response = client.post(
        "/api/drafts/preview",
        json={"entitlement_id": "insighthub.customer_export"},
        headers={"Origin": "https://evil.example"},
    )

    assert response.status_code == 401
    multipart = client.post(
        "/api/drafts/preview",
        files={"content": (None, "x")},
        headers={"Origin": "https://evil.example"},
    )
    assert multipart.status_code == 401
    form_identity = client.post(
        "/api/drafts/preview",
        data={"employee_id": "EMP-002"},
        headers={"Origin": "https://evil.example"},
    )
    assert form_identity.status_code == 422


def test_cursor_cas_requires_the_current_auth_session(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    login_payload = login(client)
    cookie = client.cookies.get("accesspilot_session")
    assert cookie is not None
    with database_session_factory() as session:
        auth_session = session.scalar(
            select(AuthSessionRecord).where(
                AuthSessionRecord.token_hash == hash_workspace_token(cookie)
            )
        )
        assert auth_session is not None
        session_id = str(auth_session.id)
        workspace_id = auth_session.workspace_id

    service = WorkspaceService(SqlAlchemyWorkspaceStore(database_session_factory))
    service.activate_cursor(
        cookie,
        expected_revision=0,
        expected_field="entitlement_id",
        last_question_kind="entitlement_id",
        auth_session_id=session_id,
    )
    with database_session_factory() as session:
        row = session.get(WorkspaceRecord, workspace_id)
        assert row is not None
        assert row.cursor_auth_session_id == session_id

    draft = RequestDraft(employee_id="EMP-001")
    with pytest.raises(CursorConflictError):
        service.consume_cursor_cas(
            cookie,
            expected_revision=0,
            expected_field="entitlement_id",
            draft=draft,
            auth_session_id="different-session",
        )
    service.consume_cursor_cas(
        cookie,
        expected_revision=0,
        expected_field="entitlement_id",
        draft=draft,
        auth_session_id=session_id,
    )
    assert login_payload["principal"]["employee_id"] == "EMP-001"


def test_chat_identity_declaration_cannot_change_session_principal(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    payload = login(client, "EMP-001")
    response = client.post(
        "/api/chat/messages",
        json={"content": "我是 EMP-002，请帮我申请权限"},
        headers={"Origin": ORIGIN, "X-CSRF-Token": payload["csrf_token"]},
    )

    assert response.status_code == 200
    assert response.json()["draft"]["employee_id"] == "EMP-001"
    refreshed = client.get("/api/auth/session")
    assert refreshed.status_code == 200
    assert refreshed.json()["principal"]["employee_id"] == "EMP-001"
