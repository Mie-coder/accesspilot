"""T20 shared Case resource ACL contract tests."""

from datetime import UTC, datetime, timedelta
from typing import Never
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.config import Settings
from accesspilot.db.models import (
    AccessGrantRecord,
    AccessRequestRecord,
    ApprovalCaseRecord,
    ApprovalStepRecord,
    AuditEventRecord,
    AuthSessionRecord,
    ProvisioningAttemptRecord,
    WorkspaceEventRecord,
    WorkspaceRecord,
)
from accesspilot.db.seed import seed_catalog
from accesspilot.db.workspace_store import SqlAlchemyWorkspaceStore, hash_workspace_token
from accesspilot.main import create_app
from support.auth import login_as

ORIGIN = "http://127.0.0.1:5173"


class NoCallStructuredReplyModel:
    def __init__(self) -> None:
        self.calls = 0

    def parse_reply(
        self,
        user_reply: str,
        correction: str | None = None,
    ) -> Never:
        del user_reply, correction
        self.calls += 1
        raise AssertionError("ACL read/refusal must not call the conversation model")


class NoCallRiskReviewModel:
    def __init__(self) -> None:
        self.calls = 0

    def review(self, context: object) -> Never:
        del context
        self.calls += 1
        raise AssertionError("ACL read/refusal must not call the risk model")


class NoCallIamProvisioner:
    def __init__(self) -> None:
        self.provision_calls = 0
        self.query_calls = 0

    def provision(
        self,
        *,
        request_id: UUID,
        idempotency_key: str,
        fault_mode: str | None,
    ) -> Never:
        del request_id, idempotency_key, fault_mode
        self.provision_calls += 1
        raise AssertionError("ACL read/refusal must not call IAM provision")

    def query_status(self, *, idempotency_key: str) -> Never:
        del idempotency_key
        self.query_calls += 1
        raise AssertionError("ACL read/refusal must not query IAM")


def build_client(
    database_session_factory: sessionmaker[Session],
    **app_overrides: object,
) -> TestClient:
    with database_session_factory() as session:
        seed_catalog(session)
    return TestClient(
        create_app(
            settings=Settings(web_origin=ORIGIN),
            store=SqlAlchemyWorkspaceStore(database_session_factory),
            session_factory=database_session_factory,
            **app_overrides,
        )
    )


def submit_case(
    client: TestClient,
    database_session_factory: sessionmaker[Session],
) -> tuple[str, UUID]:
    login_as(client, "EMP-001", session_factory=database_session_factory)
    preview = client.post(
        "/api/drafts/preview",
        json={
            "entitlement_id": "insighthub.customer_export",
            "duration_days": 14,
            "justification": "T20 资源级 Case ACL 验证",
            "confirmed": True,
        },
    )
    assert preview.status_code == 200, preview.text
    submitted = client.post("/api/requests")
    assert submitted.status_code == 201, submitted.text
    request_id = submitted.json()["request_id"]

    cookie = client.cookies.get("accesspilot_session")
    assert cookie is not None
    with database_session_factory() as session:
        auth_session = session.scalar(
            select(AuthSessionRecord).where(
                AuthSessionRecord.token_hash == hash_workspace_token(cookie)
            )
        )
        assert auth_session is not None
        case = ApprovalCaseRecord(
            workspace_id=auth_session.workspace_id,
            request_id=UUID(request_id),
            approval_status="pending_manager",
        )
        session.add(case)
        session.flush()
        session.add_all(
            [
                ApprovalStepRecord(
                    workspace_id=auth_session.workspace_id,
                    approval_case_id=case.id,
                    step_order=1,
                    approver_id="EMP-002",
                    approver_role="manager",
                    step_status="pending",
                ),
                ApprovalStepRecord(
                    workspace_id=auth_session.workspace_id,
                    approval_case_id=case.id,
                    step_order=2,
                    approver_id="EMP-003",
                    approver_role="data_owner",
                    step_status="waiting",
                ),
            ]
        )
        session.commit()
    return request_id, auth_session.workspace_id


def business_snapshot(
    session: Session,
    *,
    request_id: UUID,
) -> dict[str, object]:
    """Snapshot every T20 business table touched by a Case read contract."""

    request = session.get(AccessRequestRecord, request_id)
    assert request is not None
    case = session.scalar(
        select(ApprovalCaseRecord).where(ApprovalCaseRecord.request_id == request_id)
    )
    assert case is not None
    steps = session.scalars(
        select(ApprovalStepRecord)
        .where(ApprovalStepRecord.approval_case_id == case.id)
        .order_by(ApprovalStepRecord.step_order)
    ).all()
    attempts = session.scalars(
        select(ProvisioningAttemptRecord).where(
            ProvisioningAttemptRecord.request_id == request_id
        )
    ).all()
    grants = session.scalars(
        select(AccessGrantRecord).where(AccessGrantRecord.request_id == request_id)
    ).all()
    audits = session.scalars(
        select(AuditEventRecord)
        .where(AuditEventRecord.request_id == request_id)
        .order_by(AuditEventRecord.id)
    ).all()
    return {
        "global_counts": {
            model.__tablename__: session.scalar(
                select(func.count()).select_from(model)
            )
            for model in (
                AccessRequestRecord,
                ApprovalCaseRecord,
                ApprovalStepRecord,
                ProvisioningAttemptRecord,
                AccessGrantRecord,
                AuditEventRecord,
            )
        },
        "request": (
            str(request.id),
            request.request_status,
            request.requester_id,
            request.workspace_id,
        ),
        "case": (str(case.id), case.approval_status, case.workspace_id),
        "steps": tuple(
            (
                str(step.id),
                step.step_order,
                step.approver_id,
                step.approver_role,
                step.step_status,
                step.decided_at,
            )
            for step in steps
        ),
        "attempts": tuple(
            (str(attempt.id), attempt.provisioning_status, attempt.attempt_count)
            for attempt in attempts
        ),
        "grants": tuple(str(grant.id) for grant in grants),
        "audits": tuple((str(audit.id), audit.event_type) for audit in audits),
    }


def test_case_acl_is_resource_scoped_across_sessions_and_private_workspaces(
    database_session_factory: sessionmaker[Session],
) -> None:
    requester = build_client(database_session_factory)
    request_id, workspace_id = submit_case(requester, database_session_factory)

    requester_relogin = build_client(database_session_factory)
    login_as(requester_relogin, "EMP-001")
    requester_detail = requester_relogin.get(f"/api/requests/{request_id}")
    assert requester_detail.status_code == 200, requester_detail.text

    # A fresh Session gets a different private Workspace and cannot see the
    # submitted draft, while the resource-level Case remains readable.
    private_snapshot = requester_relogin.get("/api/drafts/current")
    assert private_snapshot.status_code == 200
    assert private_snapshot.json()["draft"] is None
    assert requester.get("/api/drafts/current").json()["draft"] is not None

    manager = build_client(database_session_factory)
    login_as(manager, "EMP-002")
    manager_detail = manager.get(f"/api/requests/{request_id}")
    assert manager_detail.status_code == 200, manager_detail.text

    waiting_owner = build_client(database_session_factory)
    login_as(waiting_owner, "EMP-003")
    assert waiting_owner.get(f"/api/requests/{request_id}").status_code == 404

    guessed = requester_relogin.get(f"/api/requests/{uuid4()}")
    assert guessed.status_code == 404

    with database_session_factory() as session:
        request = session.get(AccessRequestRecord, UUID(request_id))
        assert request is not None
        assert request.workspace_id == workspace_id
        assert session.get(WorkspaceRecord, workspace_id) is not None


def test_case_lists_inbox_and_latest_are_sql_filtered_by_resource_relation(
    database_session_factory: sessionmaker[Session],
) -> None:
    requester = build_client(database_session_factory)
    request_id, _ = submit_case(requester, database_session_factory)

    requester_relogin = build_client(database_session_factory)
    login_as(requester_relogin, "EMP-001")
    mine = requester_relogin.get("/api/requests/mine")
    accessible = requester_relogin.get("/api/requests/accessible")
    latest = requester_relogin.get("/api/requests/latest")
    assert mine.status_code == accessible.status_code == latest.status_code == 200
    assert request_id in {item["request_id"] for item in mine.json()["items"]}
    assert request_id in {
        item["request_id"] for item in accessible.json()["items"]
    }
    assert latest.json()["request"]["request"]["requester_id"] == "EMP-001"

    manager = build_client(database_session_factory)
    login_as(manager, "EMP-002")
    manager_accessible = manager.get("/api/requests/accessible")
    manager_latest = manager.get("/api/requests/latest")
    manager_inbox = manager.get("/api/approval-inbox")
    assert manager_accessible.status_code == manager_inbox.status_code == 200
    assert request_id in {
        item["request_id"] for item in manager_accessible.json()["items"]
    }
    manager_latest_request = manager_latest.json()["request"]
    if manager_latest_request is not None:
        assert manager_latest_request["request"]["requester_id"] == "EMP-002"
        assert manager_latest_request["request"]["request_id"] != request_id
    assert request_id in {
        item["request_id"] for item in manager_inbox.json()["items"]
    }

    requester_inbox = requester_relogin.get("/api/approval-inbox")
    assert requester_inbox.status_code == 403


def test_approver_relation_requires_step_role_to_match_principal_roles(
    database_session_factory: sessionmaker[Session],
) -> None:
    requester = build_client(database_session_factory)
    request_id, _ = submit_case(requester, database_session_factory)
    with database_session_factory() as session:
        manager_step = session.scalar(
            select(ApprovalStepRecord).where(
                ApprovalStepRecord.approver_id == "EMP-002",
                ApprovalStepRecord.approval_case_id
                == select(ApprovalCaseRecord.id)
                .where(ApprovalCaseRecord.request_id == UUID(request_id))
                .scalar_subquery(),
            )
        )
        assert manager_step is not None
        # The assignee id alone must not create a Case relation when the
        # workflow role disagrees with the server-derived Principal roles.
        manager_step.approver_role = "data_owner"
        session.commit()

    manager = build_client(database_session_factory)
    login_as(manager, "EMP-002")
    assert manager.get(f"/api/requests/{request_id}").status_code == 404
    assert request_id not in {
        item["request_id"]
        for item in manager.get("/api/requests/accessible").json()["items"]
    }
    assert request_id not in {
        item["request_id"]
        for item in manager.get("/api/approval-inbox").json()["items"]
    }


def test_non_approver_principal_role_cannot_become_a_step_relation(
    database_session_factory: sessionmaker[Session],
) -> None:
    requester = build_client(database_session_factory)
    request_id, _ = submit_case(requester, database_session_factory)
    with database_session_factory() as session:
        manager_step = session.scalar(
            select(ApprovalStepRecord).where(
                ApprovalStepRecord.approval_case_id
                == select(ApprovalCaseRecord.id)
                .where(ApprovalCaseRecord.request_id == UUID(request_id))
                .scalar_subquery(),
                ApprovalStepRecord.step_order == 1,
            )
        )
        assert manager_step is not None
        manager_step.approver_id = "EMP-004"
        manager_step.approver_role = "permissions_admin"
        session.commit()

    admin = build_client(database_session_factory)
    login_as(admin, "EMP-004")
    assert admin.get(f"/api/requests/{request_id}").status_code == 404
    assert request_id not in {
        item["request_id"]
        for item in admin.get("/api/requests/accessible").json()["items"]
    }


@pytest.mark.parametrize("request_status", ["approved", "cancelled"])
def test_requester_lists_and_latest_keep_formal_case_after_status_transition(
    database_session_factory: sessionmaker[Session],
    request_status: str,
) -> None:
    requester = build_client(database_session_factory)
    request_id, _ = submit_case(requester, database_session_factory)
    with database_session_factory() as session:
        request = session.get(AccessRequestRecord, UUID(request_id))
        assert request is not None
        original_status = request.request_status
        original_created_at = request.created_at
        latest_created_at = session.scalar(select(func.max(AccessRequestRecord.created_at)))
        assert latest_created_at is not None
        request.request_status = request_status
        # Temporarily make this fixture the latest without leaving a future
        # timestamp behind in the shared integration database.
        request.created_at = latest_created_at + timedelta(microseconds=1)
        session.commit()
    try:
        requester_relogin = build_client(database_session_factory)
        login_as(requester_relogin, "EMP-001")
        mine = requester_relogin.get("/api/requests/mine")
        latest = requester_relogin.get("/api/requests/latest")
        assert mine.status_code == latest.status_code == 200
        assert request_id in {item["request_id"] for item in mine.json()["items"]}
        assert latest.json()["request"]["request"]["request_id"] == request_id
        assert latest.json()["request"]["request"]["request_status"] == request_status
    finally:
        with database_session_factory() as session:
            request = session.get(AccessRequestRecord, UUID(request_id))
            assert request is not None
            request.request_status = original_status
            request.created_at = original_created_at
            session.commit()


def test_decided_approver_and_approved_admin_case_reads_are_publicly_projected(
    database_session_factory: sessionmaker[Session],
) -> None:
    requester = build_client(database_session_factory)
    request_id, workspace_id = submit_case(requester, database_session_factory)
    with database_session_factory() as session:
        case = session.scalar(
            select(ApprovalCaseRecord).where(
                ApprovalCaseRecord.request_id == UUID(request_id)
            )
        )
        assert case is not None
        steps = list(
            session.scalars(
                select(ApprovalStepRecord)
                .where(ApprovalStepRecord.approval_case_id == case.id)
                .order_by(ApprovalStepRecord.step_order)
            ).all()
        )
        steps[0].step_status = "approved"
        steps[0].decided_at = datetime.now(UTC)
        steps[1].step_status = "cancelled"
        session.add(
            AuditEventRecord(
                workspace_id=workspace_id,
                request_id=UUID(request_id),
                actor_type="system",
                actor_id=None,
                event_type="t20.secret.fixture",
                details={"status": "safe", "api_key": "must-not-leak"},
            )
        )
        case.approval_status = "approved"
        session.commit()

    manager = build_client(database_session_factory)
    login_as(manager, "EMP-002")
    manager_detail = manager.get(f"/api/requests/{request_id}")
    assert manager_detail.status_code == 200

    owner = build_client(database_session_factory)
    login_as(owner, "EMP-003")
    # A cancelled, never-decided step is not a resource relation.
    assert owner.get(f"/api/requests/{request_id}").status_code == 404

    admin = build_client(database_session_factory)
    login_as(admin, "EMP-004")
    admin_detail = admin.get(f"/api/requests/{request_id}")
    admin_accessible = admin.get("/api/requests/accessible")
    assert admin_detail.status_code == 200
    assert request_id in {
        item["request_id"] for item in admin_accessible.json()["items"]
    }
    assert "must-not-leak" not in admin_detail.text
    assert "api_key" not in admin_detail.text

    with database_session_factory() as session:
        case = session.scalar(
            select(ApprovalCaseRecord).where(
                ApprovalCaseRecord.request_id == UUID(request_id)
            )
        )
        assert case is not None
        case.approval_status = "rejected"
        session.commit()
    assert admin.get(f"/api/requests/{request_id}").status_code == 404


def test_case_survives_logout_and_old_workspace_cookie_cannot_authorize_it(
    database_session_factory: sessionmaker[Session],
) -> None:
    requester = build_client(database_session_factory)
    request_id, workspace_id = submit_case(requester, database_session_factory)

    no_session = build_client(database_session_factory)
    no_session.cookies.set("accesspilot_workspace", "legacy-only-token")
    assert no_session.get(f"/api/requests/{request_id}").status_code == 401

    logout = requester.post("/api/auth/logout")
    assert logout.status_code == 200
    assert requester.get(f"/api/requests/{request_id}").status_code == 401

    requester_relogin = build_client(database_session_factory)
    login_as(requester_relogin, "EMP-001")
    assert requester_relogin.get(f"/api/requests/{request_id}").status_code == 200
    with database_session_factory() as session:
        assert session.get(AccessRequestRecord, UUID(request_id)) is not None
        assert session.get(WorkspaceRecord, workspace_id) is not None


def test_decided_approver_refresh_and_admin_terminal_facts_remain_readable(
    database_session_factory: sessionmaker[Session],
) -> None:
    requester = build_client(database_session_factory)
    request_id, workspace_id = submit_case(requester, database_session_factory)
    with database_session_factory() as session:
        case = session.scalar(
            select(ApprovalCaseRecord).where(
                ApprovalCaseRecord.request_id == UUID(request_id)
            )
        )
        assert case is not None
        manager_step = session.scalar(
            select(ApprovalStepRecord).where(
                ApprovalStepRecord.approval_case_id == case.id,
                ApprovalStepRecord.step_order == 1,
            )
        )
        assert manager_step is not None
        manager_step.step_status = "approved"
        manager_step.decided_at = datetime.now(UTC)
        case.approval_status = "approved"
        session.commit()

    manager = build_client(database_session_factory)
    login_as(manager, "EMP-002")
    assert manager.get(f"/api/requests/{request_id}").status_code == 200
    refreshed = manager.get("/api/auth/session")
    assert refreshed.status_code == 200
    manager.headers.update({"X-CSRF-Token": refreshed.json()["csrf_token"]})
    assert manager.get(f"/api/requests/{request_id}").status_code == 200
    assert request_id in {
        item["request_id"]
        for item in manager.get("/api/requests/accessible").json()["items"]
    }
    assert request_id not in {
        item["request_id"]
        for item in manager.get("/api/approval-inbox").json()["items"]
    }

    admin = build_client(database_session_factory)
    login_as(admin, "EMP-004")
    not_started = admin.get(f"/api/requests/{request_id}")
    assert not_started.status_code == 200
    assert not_started.json()["provisioning"]["provisioning_status"] == "not_started"

    idempotency_key = f"t20-terminal-{request_id}"
    with database_session_factory() as session:
        attempt = ProvisioningAttemptRecord(
            workspace_id=workspace_id,
            request_id=UUID(request_id),
            idempotency_key=idempotency_key,
            provisioning_status="failed",
            attempt_count=1,
            last_error="fixture failure must be projected safely",
        )
        session.add(attempt)
        session.commit()
    failed = admin.get(f"/api/requests/{request_id}")
    assert failed.status_code == 200
    assert failed.json()["provisioning"]["provisioning_status"] == "failed"

    with database_session_factory() as session:
        attempt = session.scalar(
            select(ProvisioningAttemptRecord).where(
                ProvisioningAttemptRecord.request_id == UUID(request_id)
            )
        )
        assert attempt is not None
        attempt.provisioning_status = "unknown"
        attempt.attempt_count = 2
        session.commit()
    unknown = admin.get(f"/api/requests/{request_id}")
    assert unknown.status_code == 200
    assert unknown.json()["provisioning"]["provisioning_status"] == "unknown"

    starts_at = datetime.now(UTC)
    with database_session_factory() as session:
        attempt = session.scalar(
            select(ProvisioningAttemptRecord).where(
                ProvisioningAttemptRecord.request_id == UUID(request_id)
            )
        )
        assert attempt is not None
        attempt.provisioning_status = "succeeded"
        attempt.attempt_count = 3
        session.add(
            AccessGrantRecord(
                workspace_id=workspace_id,
                request_id=UUID(request_id),
                idempotency_key=idempotency_key,
                starts_at=starts_at,
                expires_at=starts_at + timedelta(days=14),
            )
        )
        session.commit()
    succeeded = admin.get(f"/api/requests/{request_id}")
    assert succeeded.status_code == 200
    assert succeeded.json()["provisioning"]["provisioning_status"] == "succeeded"
    assert succeeded.json()["provisioning"]["access_granted"] is True
    assert succeeded.json()["provisioning"]["grant_id"] is not None


def test_unapproved_or_unrelated_existing_id_matches_random_not_found(
    database_session_factory: sessionmaker[Session],
) -> None:
    requester = build_client(database_session_factory)
    request_id, _ = submit_case(requester, database_session_factory)

    waiting_owner = build_client(database_session_factory)
    login_as(waiting_owner, "EMP-003")
    owner_existing = waiting_owner.get(f"/api/requests/{request_id}")
    owner_random = waiting_owner.get(f"/api/requests/{uuid4()}")
    assert owner_existing.status_code == owner_random.status_code == 404
    assert owner_existing.json() == owner_random.json() == {"detail": "申请不存在"}
    assert request_id not in {
        item["request_id"]
        for item in waiting_owner.get("/api/requests/accessible").json()["items"]
    }

    admin = build_client(database_session_factory)
    login_as(admin, "EMP-004")
    admin_existing = admin.get(f"/api/requests/{request_id}")
    admin_random = admin.get(f"/api/requests/{uuid4()}")
    assert admin_existing.status_code == admin_random.status_code == 404
    assert admin_existing.json() == admin_random.json() == {"detail": "申请不存在"}
    assert request_id not in {
        item["request_id"]
        for item in admin.get("/api/requests/accessible").json()["items"]
    }


def test_draft_chat_events_and_cursor_stay_in_the_source_workspace(
    database_session_factory: sessionmaker[Session],
) -> None:
    requester = build_client(database_session_factory)
    request_id, source_workspace_id = submit_case(
        requester,
        database_session_factory,
    )
    with database_session_factory() as session:
        source_session = session.scalar(
            select(AuthSessionRecord).where(
                AuthSessionRecord.workspace_id == source_workspace_id
            )
        )
        source_workspace = session.get(WorkspaceRecord, source_workspace_id)
        assert source_session is not None and source_workspace is not None
        source_workspace.cursor_actor_id = "EMP-001"
        source_workspace.cursor_auth_session_id = str(source_session.id)
        source_workspace.cursor_expected_field = "duration_days"
        source_workspace.cursor_last_question_kind = "ask_duration_days"
        source_workspace.cursor_issued_at = datetime.now(UTC)
        session.add(
            WorkspaceEventRecord(
                workspace_id=source_workspace_id,
                event_type="message.user",
                payload={"content": "T20-PRIVATE-CHAT-EVENT"},
            )
        )
        session.commit()

    new_session = build_client(database_session_factory)
    new_login = login_as(
        new_session,
        "EMP-001",
        session_factory=database_session_factory,
    )
    assert new_session.get(f"/api/requests/{request_id}").status_code == 200
    assert new_session.get("/api/drafts/current").json() == {
        "draft": None,
        "draft_revision": 0,
    }
    event_replay = new_session.get("/api/events?follow=false")
    assert event_replay.status_code == 200
    assert "T20-PRIVATE-CHAT-EVENT" not in event_replay.text

    with database_session_factory() as session:
        new_auth_session = session.get(AuthSessionRecord, UUID(new_login.session_id or ""))
        assert new_auth_session is not None
        new_workspace = session.get(WorkspaceRecord, new_auth_session.workspace_id)
        source_workspace = session.get(WorkspaceRecord, source_workspace_id)
        assert new_workspace is not None and source_workspace is not None
        assert new_workspace.id != source_workspace.id
        assert new_workspace.draft is None
        assert new_workspace.cursor_expected_field is None
        assert source_workspace.cursor_expected_field == "duration_days"
        assert source_workspace.cursor_auth_session_id == str(source_session.id)


@pytest.mark.parametrize("invalidated_by", ["revoked", "expired"])
def test_revoked_or_expired_session_cannot_read_but_new_session_can(
    database_session_factory: sessionmaker[Session],
    invalidated_by: str,
) -> None:
    requester = build_client(database_session_factory)
    request_id, source_workspace_id = submit_case(
        requester,
        database_session_factory,
    )
    cookie = requester.cookies.get("accesspilot_session")
    assert cookie is not None
    with database_session_factory() as session:
        auth_session = session.scalar(
            select(AuthSessionRecord).where(
                AuthSessionRecord.token_hash == hash_workspace_token(cookie)
            )
        )
        assert auth_session is not None
        if invalidated_by == "revoked":
            auth_session.revoked_at = datetime.now(UTC)
        else:
            auth_session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

    assert requester.get(f"/api/requests/{request_id}").status_code == 401
    requester_relogin = build_client(database_session_factory)
    login_as(requester_relogin, "EMP-001")
    assert requester_relogin.get(f"/api/requests/{request_id}").status_code == 200
    with database_session_factory() as session:
        assert session.get(WorkspaceRecord, source_workspace_id) is not None
        assert session.get(AccessRequestRecord, UUID(request_id)) is not None


def test_acl_gets_and_refusals_never_call_models_or_iam_or_write_business_facts(
    database_session_factory: sessionmaker[Session],
) -> None:
    structured_model = NoCallStructuredReplyModel()
    risk_model = NoCallRiskReviewModel()
    iam = NoCallIamProvisioner()
    requester = build_client(
        database_session_factory,
        structured_reply_model=structured_model,
        risk_review_model=risk_model,
        iam_provisioner=iam,
    )
    request_id, _ = submit_case(requester, database_session_factory)
    manager = TestClient(requester.app)
    owner = TestClient(requester.app)
    admin = TestClient(requester.app)
    login_as(manager, "EMP-002")
    login_as(owner, "EMP-003")
    login_as(admin, "EMP-004")

    with database_session_factory() as session:
        before = business_snapshot(session, request_id=UUID(request_id))

    assert requester.get(f"/api/requests/{request_id}").status_code == 200
    assert requester.get("/api/requests/mine").status_code == 200
    assert requester.get("/api/requests/latest").status_code == 200
    assert manager.get(f"/api/requests/{request_id}").status_code == 200
    assert manager.get("/api/approval-inbox").status_code == 200
    assert owner.get(f"/api/requests/{request_id}").status_code == 404
    assert owner.get(f"/api/requests/{uuid4()}").status_code == 404
    assert admin.get(f"/api/requests/{request_id}").status_code == 404
    assert admin.get(f"/api/requests/{uuid4()}").status_code == 404

    with database_session_factory() as session:
        after = business_snapshot(session, request_id=UUID(request_id))
    assert after == before
    assert structured_model.calls == 0
    assert risk_model.calls == 0
    assert iam.provision_calls == 0
    assert iam.query_calls == 0


def test_case_summary_reports_grant_facts_without_inferring_from_approval(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = build_client(database_session_factory)
    request_id, workspace_id = submit_case(client, database_session_factory)
    with database_session_factory() as session:
        case = session.scalar(select(ApprovalCaseRecord).where(
            ApprovalCaseRecord.request_id == UUID(request_id)))
        assert case is not None
        case.approval_status = "approved"
        session.commit()
    item = client.get("/api/requests/mine").json()["items"][0]
    assert item["approval_status"] == "approved"
    assert item["grant_id"] is None and item["starts_at"] is None
    start = datetime.now(UTC) + timedelta(days=1)
    with database_session_factory() as session:
        grant = AccessGrantRecord(workspace_id=workspace_id, request_id=UUID(request_id),
                                  idempotency_key=str(uuid4()), starts_at=start,
                                  expires_at=start + timedelta(days=7))
        session.add(grant)
        session.commit()
        grant_id = str(grant.id)
    items = [item for item in client.get("/api/requests/mine").json()["items"]
             if item["request_id"] == request_id]
    assert len(items) == 1
    assert items[0]["grant_id"] == grant_id
    assert datetime.fromisoformat(items[0]["starts_at"]) == start
    assert datetime.fromisoformat(items[0]["expires_at"]) == start + timedelta(days=7)
