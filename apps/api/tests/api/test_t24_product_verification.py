"""T24 backend product-verification integration contract."""

from dataclasses import dataclass
from threading import Lock
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

import accesspilot.main as main_module
from accesspilot.agent.embeddings import DeterministicEmbeddingModel
from accesspilot.auth import InvalidAuthSessionError
from accesspilot.config import Settings
from accesspilot.db.models import (
    AccessGrantRecord,
    ApprovalCaseRecord,
    ApprovalStepRecord,
    AuditEventRecord,
    DecisionPacketRecord,
    ProvisioningAttemptRecord,
)
from accesspilot.db.seed import seed_catalog
from accesspilot.db.workspace_store import SqlAlchemyWorkspaceStore
from accesspilot.decision_packets import DecisionAdvisory, DecisionAdvisoryContext
from accesspilot.events import append_workspace_event
from accesspilot.main import create_app
from accesspilot.provisioning import IamOutcome
from accesspilot.rag.policies import index_policy_embeddings
from support.auth import LoginResult, login_as

ORIGIN = "http://127.0.0.1:5173"


class CountingAdvisoryModel:
    """Deterministic advisory seam whose calls expose accidental GET work."""

    generation_mode = "deterministic"

    def __init__(self) -> None:
        self.calls = 0

    def review(self, context: DecisionAdvisoryContext) -> DecisionAdvisory:
        del context
        self.calls += 1
        return DecisionAdvisory(
            assessment="risk",
            summary="仅供人工审批参考。",
            unknowns=[],
            recommendations=["核对最小权限与期限。"],
            citations=["POL-003"],
        )


class RecordingIam:
    """Successful deterministic IAM seam with an observable side-effect count."""

    def __init__(self) -> None:
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
        return IamOutcome(status="succeeded")

    def query_status(self, *, idempotency_key: str) -> IamOutcome:
        with self._lock:
            self.query_calls.append(idempotency_key)
        return IamOutcome(status="succeeded")


@dataclass(frozen=True)
class RoleClients:
    requester: TestClient
    manager: TestClient
    owner: TestClient
    admin: TestClient
    requester_login: LoginResult


def build_app(
    database_session_factory: sessionmaker[Session],
) -> tuple[FastAPI, CountingAdvisoryModel, RecordingIam]:
    embedding_model = DeterministicEmbeddingModel()
    with database_session_factory() as session:
        seed_catalog(session)
        index_policy_embeddings(session, embedding_model)
    advisory = CountingAdvisoryModel()
    iam = RecordingIam()
    app = create_app(
        settings=Settings(web_origin=ORIGIN, deepseek_api_key=None),
        store=SqlAlchemyWorkspaceStore(database_session_factory),
        session_factory=database_session_factory,
        embedding_model=embedding_model,
        decision_advisory_model=advisory,
        iam_provisioner=iam,
    )
    return app, advisory, iam


def login_four_roles(app: FastAPI) -> RoleClients:
    requester = TestClient(app)
    manager = TestClient(app)
    owner = TestClient(app)
    admin = TestClient(app)
    requester_login = login_as(requester, "EMP-001")
    login_as(manager, "EMP-002")
    login_as(owner, "EMP-003")
    login_as(admin, "EMP-004")
    return RoleClients(requester, manager, owner, admin, requester_login)


def submit_and_start_case(clients: RoleClients) -> tuple[str, str, str, str]:
    preview = clients.requester.post(
        "/api/drafts/preview",
        json={
            "entitlement_id": "insighthub.customer_export",
            "duration_days": 14,
            "justification": "T24 四角色产品闭环验证",
            "confirmed": True,
        },
    )
    assert preview.status_code == 200, preview.text
    submitted = clients.requester.post("/api/requests")
    assert submitted.status_code == 201, submitted.text
    request_id = submitted.json()["request_id"]
    packet = clients.requester.post(f"/api/requests/{request_id}/decision-packet")
    assert packet.status_code == 201, packet.text
    started = clients.requester.post(f"/api/requests/{request_id}/approval-case")
    assert started.status_code == 201, started.text
    steps = started.json()["steps"]
    return (
        request_id,
        started.json()["approval_case_id"],
        steps[0]["step_id"],
        steps[1]["step_id"],
    )


def business_snapshot(
    session: Session,
    *,
    request_id: UUID,
    case_id: UUID,
) -> tuple[object, ...]:
    case = session.get(ApprovalCaseRecord, case_id)
    assert case is not None
    steps = session.scalars(
        select(ApprovalStepRecord)
        .where(ApprovalStepRecord.approval_case_id == case_id)
        .order_by(ApprovalStepRecord.step_order)
    ).all()

    def count(model: type[object]) -> int:
        return session.scalar(
            select(func.count()).select_from(model).where(  # type: ignore[attr-defined]
                model.request_id == request_id  # type: ignore[attr-defined]
            )
        ) or 0

    return (
        case.approval_status,
        tuple(
            (
                step.id,
                step.step_status,
                step.comment,
                step.decided_at,
            )
            for step in steps
        ),
        count(DecisionPacketRecord),
        count(ProvisioningAttemptRecord),
        count(AccessGrantRecord),
        count(AuditEventRecord),
    )


def test_four_independent_sessions_complete_one_case_and_requester_relogin_reads_grant(
    database_session_factory: sessionmaker[Session],
) -> None:
    app, advisory, iam = build_app(database_session_factory)
    clients = login_four_roles(app)
    request_id, case_id, manager_step_id, owner_step_id = submit_and_start_case(clients)

    manager_inbox = clients.manager.get("/api/approval-inbox")
    assert manager_inbox.status_code == 200
    assert request_id in {item["request_id"] for item in manager_inbox.json()["items"]}
    manager_approved = clients.manager.post(
        f"/api/approval-cases/{case_id}/decisions",
        json={
            "approval_step_id": manager_step_id,
            "decision": "approve",
            "comment": "经理确认业务需要。",
        },
    )
    assert manager_approved.status_code == 200, manager_approved.text

    owner_inbox = clients.owner.get("/api/approval-inbox")
    assert owner_inbox.status_code == 200
    assert request_id in {item["request_id"] for item in owner_inbox.json()["items"]}
    owner_approved = clients.owner.post(
        f"/api/approval-cases/{case_id}/decisions",
        json={
            "approval_step_id": owner_step_id,
            "decision": "approve",
            "comment": "数据负责人确认最小权限。",
        },
    )
    assert owner_approved.status_code == 200, owner_approved.text

    task = next(
        item
        for item in clients.admin.get("/api/provisioning-tasks").json()["items"]
        if item["request_id"] == request_id
    )
    assert task["can_provision"] is True
    provisioned = clients.admin.post(f"/api/requests/{request_id}/provision")
    assert provisioned.status_code == 200, provisioned.text
    assert provisioned.json()["access_granted"] is True
    grant_id = provisioned.json()["grant_id"]

    initial_token = clients.requester_login.session_token
    assert clients.requester.post("/api/auth/logout").status_code == 200
    relogin = login_as(clients.requester, "EMP-001")
    assert relogin.session_token != initial_token
    detail = clients.requester.get(f"/api/requests/{request_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["approval"]["approval_status"] == "approved"
    assert detail.json()["provisioning"]["grant_id"] == grant_id

    with database_session_factory() as session:
        before_gets = business_snapshot(
            session,
            request_id=UUID(request_id),
            case_id=UUID(case_id),
        )
    model_calls = advisory.calls
    iam_calls = (list(iam.provision_calls), list(iam.query_calls))
    assert clients.requester.get("/api/requests/mine").status_code == 200
    assert clients.manager.get(f"/api/requests/{request_id}").status_code == 200
    assert clients.owner.get(f"/api/requests/{request_id}").status_code == 200
    assert clients.admin.get(f"/api/requests/{request_id}").status_code == 200
    assert clients.admin.get("/api/provisioning-tasks").status_code == 200
    with database_session_factory() as session:
        after_gets = business_snapshot(
            session,
            request_id=UUID(request_id),
            case_id=UUID(case_id),
        )
    assert after_gets == before_gets
    assert advisory.calls == model_calls == 1
    assert (iam.provision_calls, iam.query_calls) == iam_calls
    assert len(iam.provision_calls) == 1


def test_attack_matrix_refuses_cross_role_actions_and_old_workspace_cookie_without_writes(
    database_session_factory: sessionmaker[Session],
) -> None:
    app, advisory, iam = build_app(database_session_factory)
    clients = login_four_roles(app)
    request_id, case_id, manager_step_id, owner_step_id = submit_and_start_case(clients)
    with database_session_factory() as session:
        before = business_snapshot(
            session,
            request_id=UUID(request_id),
            case_id=UUID(case_id),
        )

    owner_out_of_order = clients.owner.post(
        f"/api/approval-cases/{case_id}/decisions",
        json={
            "approval_step_id": owner_step_id,
            "decision": "approve",
            "comment": "越序尝试",
        },
    )
    requester_self_approval = clients.requester.post(
        f"/api/approval-cases/{case_id}/decisions",
        json={
            "approval_step_id": manager_step_id,
            "decision": "approve",
            "comment": "自批尝试",
        },
    )
    manager_provision = clients.manager.post(f"/api/requests/{request_id}/provision")
    unapproved_admin = clients.admin.post(f"/api/requests/{request_id}/provision")
    old_workspace_only = TestClient(app)
    old_workspace_only.cookies.set(
        "accesspilot_workspace",
        clients.requester_login.session_token,
    )
    old_cookie_read = old_workspace_only.get(f"/api/requests/{request_id}")

    assert owner_out_of_order.status_code == 409
    assert requester_self_approval.status_code == 404
    assert manager_provision.status_code == 403
    assert unapproved_admin.status_code == 404
    assert old_cookie_read.status_code == 401
    with database_session_factory() as session:
        after = business_snapshot(
            session,
            request_id=UUID(request_id),
            case_id=UUID(case_id),
        )
    assert after == before
    assert advisory.calls == 1
    assert iam.provision_calls == [] and iam.query_calls == []


def test_established_event_stream_revalidates_session_before_each_event(
    database_session_factory: sessionmaker[Session],
    monkeypatch: object,
) -> None:
    app, _, _ = build_app(database_session_factory)
    client = TestClient(app)
    login = login_as(client, "EMP-001")
    with database_session_factory() as session:
        first = append_workspace_event(
            session,
            workspace_token=login.session_token,
            event_type="message.user",
            payload={"content": "失效前可见"},
        )
        second = append_workspace_event(
            session,
            workspace_token=login.session_token,
            event_type="message.assistant",
            payload={"content": "失效后不得泄露"},
        )

    real_load = main_module.load_auth_context
    calls = 0

    def expire_after_stream_is_established(
        session_factory: sessionmaker[Session],
        *,
        token: str | None,
    ):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls >= 4:
            raise InvalidAuthSessionError("expired")
        return real_load(session_factory, token=token)

    monkeypatch.setattr(  # type: ignore[attr-defined]
        main_module,
        "load_auth_context",
        expire_after_stream_is_established,
    )
    response = client.get("/api/events?follow=false")

    assert response.status_code == 200
    assert f"id: {first.id}\n" in response.text
    assert f"id: {second.id}\n" not in response.text
    assert "失效后不得泄露" not in response.text
    assert calls >= 4
