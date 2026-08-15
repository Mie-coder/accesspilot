"""T23 permissions-admin provisioning lifecycle contract."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Lock
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.embeddings import DeterministicEmbeddingModel
from accesspilot.config import Settings
from accesspilot.db.models import (
    AccessGrantRecord,
    AccessRequestRecord,
    ApprovalCaseRecord,
    ApprovalStepRecord,
    ProvisioningAttemptRecord,
)
from accesspilot.db.seed import seed_catalog
from accesspilot.db.workspace_store import SqlAlchemyWorkspaceStore
from accesspilot.main import create_app
from accesspilot.provisioning import IamOutcome
from accesspilot.rag.policies import index_policy_embeddings
from support.auth import login_as

ORIGIN = "http://127.0.0.1:5173"


class RecordingIam:
    def __init__(
        self,
        provision_outcomes: list[IamOutcome] | None = None,
        query_outcomes: list[IamOutcome] | None = None,
    ) -> None:
        self.provision_outcomes = provision_outcomes or [IamOutcome(status="succeeded")]
        self.query_outcomes = query_outcomes or []
        self.provision_calls: list[tuple[UUID, str]] = []
        self.query_calls: list[str] = []
        self._lock = Lock()

    def provision(
        self,
        *,
        request_id: UUID,
        idempotency_key: str,
        fault_mode: str | None,
    ) -> IamOutcome:
        del fault_mode
        with self._lock:
            self.provision_calls.append((request_id, idempotency_key))
            return self.provision_outcomes.pop(0)

    def query_status(self, *, idempotency_key: str) -> IamOutcome:
        with self._lock:
            self.query_calls.append(idempotency_key)
            return self.query_outcomes.pop(0)


def build_app(
    database_session_factory: sessionmaker[Session],
    iam: RecordingIam,
) -> FastAPI:
    embedding_model = DeterministicEmbeddingModel()
    with database_session_factory() as session:
        seed_catalog(session)
        index_policy_embeddings(session, embedding_model)
    return create_app(
        settings=Settings(web_origin=ORIGIN, deepseek_api_key=None),
        store=SqlAlchemyWorkspaceStore(database_session_factory),
        session_factory=database_session_factory,
        embedding_model=embedding_model,
        iam_provisioner=iam,
    )


def role_client(app: FastAPI, employee_id: str) -> TestClient:
    client = TestClient(app)
    login_as(client, employee_id)
    return client


def create_case(
    database_session_factory: sessionmaker[Session],
    *,
    approval_status: str = "approved",
) -> str:
    requester = role_client(build_app(database_session_factory, RecordingIam()), "EMP-001")
    preview = requester.post(
        "/api/drafts/preview",
        json={
            "entitlement_id": "insighthub.customer_export",
            "duration_days": 14,
            "justification": "T23 权限管理员开通验证",
            "confirmed": True,
        },
    )
    assert preview.status_code == 200, preview.text
    submitted = requester.post("/api/requests")
    assert submitted.status_code == 201, submitted.text
    request_id = submitted.json()["request_id"]
    with database_session_factory() as session:
        request = session.get(AccessRequestRecord, UUID(request_id))
        assert request is not None
        case = ApprovalCaseRecord(
            workspace_id=request.workspace_id,
            request_id=request.id,
            approval_status=approval_status,
        )
        session.add(case)
        session.flush()
        terminal = approval_status == "approved"
        session.add_all(
            [
                ApprovalStepRecord(
                    workspace_id=request.workspace_id,
                    approval_case_id=case.id,
                    step_order=1,
                    approver_id="EMP-002",
                    approver_role="manager",
                    step_status="approved" if terminal else "pending",
                    decided_at=datetime.now(UTC) if terminal else None,
                ),
                ApprovalStepRecord(
                    workspace_id=request.workspace_id,
                    approval_case_id=case.id,
                    step_order=2,
                    approver_id="EMP-003",
                    approver_role="data_owner",
                    step_status="approved" if terminal else "waiting",
                    decided_at=datetime.now(UTC) if terminal else None,
                ),
            ]
        )
        session.commit()
    return request_id


def counts(
    session: Session,
    request_id: UUID,
) -> tuple[int, int]:
    return (
        session.scalar(
            select(func.count())
            .select_from(ProvisioningAttemptRecord)
            .where(ProvisioningAttemptRecord.request_id == request_id)
        )
        or 0,
        session.scalar(
            select(func.count())
            .select_from(AccessGrantRecord)
            .where(AccessGrantRecord.request_id == request_id)
        )
        or 0,
    )


def test_admin_tasks_and_server_owned_key_create_one_grant_across_sessions(
    database_session_factory: sessionmaker[Session],
) -> None:
    iam = RecordingIam()
    app = build_app(database_session_factory, iam)
    request_id = create_case(database_session_factory)
    admin = role_client(app, "EMP-004")

    tasks = admin.get("/api/provisioning-tasks")
    assert tasks.status_code == 200, tasks.text
    task = next(item for item in tasks.json()["items"] if item["request_id"] == request_id)
    assert task["provisioning_status"] == "not_started"
    assert task["can_provision"] is True
    assert task["can_recover"] is False

    created = admin.post(f"/api/requests/{request_id}/provision")
    replay = admin.post(f"/api/requests/{request_id}/provision")
    assert created.status_code == replay.status_code == 200
    assert replay.json()["provisioning_attempt_id"] == created.json()[
        "provisioning_attempt_id"
    ]
    assert len(iam.provision_calls) == 1
    expected_key = f"accesspilot:{request_id}"
    assert iam.provision_calls[0][1] == expected_key
    with database_session_factory() as session:
        assert counts(session, UUID(request_id)) == (1, 1)
        attempt = session.scalar(
            select(ProvisioningAttemptRecord).where(
                ProvisioningAttemptRecord.request_id == UUID(request_id)
            )
        )
        assert attempt is not None and attempt.idempotency_key == expected_key

    requester_relogin = role_client(app, "EMP-001")
    detail = requester_relogin.get(f"/api/requests/{request_id}")
    assert detail.status_code == 200
    assert detail.json()["provisioning"]["access_granted"] is True
    assert detail.json()["provisioning"]["grant_id"] is not None


def test_unknown_recover_reuses_original_operation_and_grant(
    database_session_factory: sessionmaker[Session],
) -> None:
    iam = RecordingIam(
        [IamOutcome(status="unknown", message="IAM timeout")],
        [IamOutcome(status="succeeded")],
    )
    app = build_app(database_session_factory, iam)
    request_id = create_case(database_session_factory)
    admin = role_client(app, "EMP-004")

    unknown = admin.post(f"/api/requests/{request_id}/provision")
    replay = admin.post(f"/api/requests/{request_id}/provision")
    assert unknown.status_code == replay.status_code == 200
    assert replay.json()["provisioning_status"] == "unknown"
    assert len(iam.provision_calls) == 1
    task = next(
        item
        for item in admin.get("/api/provisioning-tasks").json()["items"]
        if item["request_id"] == request_id
    )
    assert task["can_provision"] is False and task["can_recover"] is True

    recovered = admin.post(f"/api/requests/{request_id}/provision/recover")
    recovered_again = admin.post(f"/api/requests/{request_id}/provision/recover")
    assert recovered.status_code == recovered_again.status_code == 200
    assert recovered_again.json()["access_granted"] is True
    assert iam.query_calls == [f"accesspilot:{request_id}"]
    with database_session_factory() as session:
        assert counts(session, UUID(request_id)) == (1, 1)


def test_provision_acl_state_and_body_refusals_do_not_call_iam_or_write(
    database_session_factory: sessionmaker[Session],
) -> None:
    iam = RecordingIam()
    app = build_app(database_session_factory, iam)
    approved_id = create_case(database_session_factory)
    pending_id = create_case(database_session_factory, approval_status="pending_manager")
    admin = role_client(app, "EMP-004")
    requester = role_client(app, "EMP-001")
    manager = role_client(app, "EMP-002")

    with database_session_factory() as session:
        approved_before = counts(session, UUID(approved_id))
        pending_before = counts(session, UUID(pending_id))
    injected = admin.post(
        f"/api/requests/{approved_id}/provision",
        json={"idempotency_key": "client-key", "employee_id": "EMP-004"},
    )
    assert injected.status_code == 422
    assert admin.post(f"/api/requests/{pending_id}/provision").status_code == 404
    assert requester.post(f"/api/requests/{approved_id}/provision").status_code == 403
    assert manager.post(f"/api/requests/{approved_id}/provision").status_code == 403
    random_id = uuid4()
    waiting_owner = role_client(app, "EMP-003")
    unrelated_existing = waiting_owner.post(f"/api/requests/{pending_id}/provision")
    unrelated_random = waiting_owner.post(f"/api/requests/{random_id}/provision")
    assert unrelated_existing.status_code == unrelated_random.status_code == 404
    assert unrelated_existing.json() == unrelated_random.json()
    with database_session_factory() as session:
        assert counts(session, UUID(approved_id)) == approved_before
        assert counts(session, UUID(pending_id)) == pending_before
    assert iam.provision_calls == [] and iam.query_calls == []


def test_concurrent_admin_provision_calls_one_iam_and_one_grant(
    database_session_factory: sessionmaker[Session],
) -> None:
    iam = RecordingIam()
    app = build_app(database_session_factory, iam)
    request_id = create_case(database_session_factory)
    admins = [role_client(app, "EMP-004"), role_client(app, "EMP-004")]

    def provision(client: TestClient) -> int:
        return client.post(f"/api/requests/{request_id}/provision").status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(provision, admins))

    assert statuses == [200, 200]
    assert len(iam.provision_calls) == 1
    with database_session_factory() as session:
        assert counts(session, UUID(request_id)) == (1, 1)


def test_recover_uses_same_admin_acl_and_requires_attempt(
    database_session_factory: sessionmaker[Session],
) -> None:
    iam = RecordingIam()
    app = build_app(database_session_factory, iam)
    request_id = create_case(database_session_factory)
    admin = role_client(app, "EMP-004")
    manager = role_client(app, "EMP-002")

    assert admin.post(f"/api/requests/{request_id}/provision/recover").status_code == 409
    assert manager.post(f"/api/requests/{request_id}/provision/recover").status_code == 403
    assert iam.query_calls == []
