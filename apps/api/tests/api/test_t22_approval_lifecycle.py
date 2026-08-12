"""T22 cross-session ordered approval and refusal-safety contract."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.embeddings import DeterministicEmbeddingModel
from accesspilot.config import Settings
from accesspilot.db.models import (
    ApprovalCaseRecord,
    ApprovalStepRecord,
    AuditEventRecord,
    PolicyChunkRecord,
)
from accesspilot.db.seed import seed_catalog
from accesspilot.db.workspace_store import SqlAlchemyWorkspaceStore
from accesspilot.decision_packets import DecisionAdvisory, DecisionAdvisoryContext
from accesspilot.main import create_app
from accesspilot.rag.policies import index_policy_embeddings
from support.auth import login_as

ORIGIN = "http://127.0.0.1:5173"


class CountingAdvisoryModel:
    generation_mode = "deterministic"

    def __init__(self) -> None:
        self.calls = 0
        self._lock = Lock()

    def review(self, context: DecisionAdvisoryContext) -> DecisionAdvisory:
        del context
        with self._lock:
            self.calls += 1
        return DecisionAdvisory(
            assessment="risk",
            summary="仅供人工审批参考。",
            unknowns=[],
            recommendations=["核对最小权限与期限。"],
            citations=["POL-003"],
        )


@pytest.fixture
def t22_app(
    database_session_factory: sessionmaker[Session],
) -> Iterator[tuple[FastAPI, CountingAdvisoryModel]]:
    embedding_model = DeterministicEmbeddingModel()
    with database_session_factory() as session:
        seed_catalog(session)
        index_policy_embeddings(session, embedding_model)
    advisory_model = CountingAdvisoryModel()
    app = create_app(
        settings=Settings(web_origin=ORIGIN, deepseek_api_key=None),
        store=SqlAlchemyWorkspaceStore(database_session_factory),
        session_factory=database_session_factory,
        embedding_model=embedding_model,
        decision_advisory_model=advisory_model,
    )
    yield app, advisory_model
    with database_session_factory() as session:
        for chunk in session.scalars(select(PolicyChunkRecord)).all():
            chunk.embedding = None
        session.commit()


def role_client(app: FastAPI, employee_id: str) -> TestClient:
    client = TestClient(app)
    login_as(client, employee_id)
    return client


def submit_packet_and_case(app: FastAPI) -> tuple[str, str, str, str]:
    requester = role_client(app, "EMP-001")
    preview = requester.post(
        "/api/drafts/preview",
        json={
            "entitlement_id": "insighthub.customer_export",
            "duration_days": 14,
            "justification": "T22 跨账号串行审批验证",
            "confirmed": True,
        },
    )
    assert preview.status_code == 200, preview.text
    submitted = requester.post("/api/requests")
    assert submitted.status_code == 201, submitted.text
    request_id = submitted.json()["request_id"]
    packet = requester.post(f"/api/requests/{request_id}/decision-packet")
    assert packet.status_code == 201, packet.text
    started = requester.post(f"/api/requests/{request_id}/approval-case")
    assert started.status_code == 201, started.text
    detail = requester.get(f"/api/requests/{request_id}")
    assert detail.status_code == 200, detail.text
    steps = detail.json()["approval"]["steps"]
    return (
        request_id,
        started.json()["approval_case_id"],
        steps[0]["step_id"],
        steps[1]["step_id"],
    )


def approval_snapshot(
    session: Session,
    *,
    case_id: UUID,
) -> tuple[object, ...]:
    case = session.get(ApprovalCaseRecord, case_id)
    assert case is not None
    steps = session.scalars(
        select(ApprovalStepRecord)
        .where(ApprovalStepRecord.approval_case_id == case_id)
        .order_by(ApprovalStepRecord.step_order)
    ).all()
    events = session.scalars(
        select(AuditEventRecord)
        .where(
            AuditEventRecord.request_id == case.request_id,
            AuditEventRecord.event_type.in_(
                ("approval.step.approved", "approval.step.rejected")
            ),
        )
        .order_by(AuditEventRecord.created_at, AuditEventRecord.id)
    ).all()
    return (
        case.approval_status,
        tuple(
            (
                step.step_order,
                step.approver_id,
                step.approver_role,
                step.step_status,
                step.comment,
                step.decided_at,
            )
            for step in steps
        ),
        tuple(
            (event.event_type, event.actor_id, event.details)
            for event in events
        ),
    )


def test_manager_then_owner_approve_from_independent_sessions_and_refresh(
    t22_app: tuple[FastAPI, CountingAdvisoryModel],
) -> None:
    app, model = t22_app
    request_id, case_id, manager_step_id, owner_step_id = submit_packet_and_case(app)
    manager = role_client(app, "EMP-002")
    waiting_owner = role_client(app, "EMP-003")

    assert request_id in {
        item["request_id"] for item in manager.get("/api/approval-inbox").json()["items"]
    }
    assert request_id not in {
        item["request_id"]
        for item in waiting_owner.get("/api/approval-inbox").json()["items"]
    }

    manager_decision = manager.post(
        f"/api/approval-cases/{case_id}/decisions",
        json={
            "approval_step_id": manager_step_id,
            "decision": "approve",
            "comment": "经理确认业务需要。",
        },
    )
    assert manager_decision.status_code == 200, manager_decision.text

    manager_refresh = role_client(app, "EMP-002")
    refreshed = manager_refresh.get(f"/api/requests/{request_id}")
    assert refreshed.status_code == 200
    assert refreshed.json()["approval"]["steps"][0]["comment"] == "经理确认业务需要。"
    assert request_id not in {
        item["request_id"]
        for item in manager_refresh.get("/api/approval-inbox").json()["items"]
    }

    owner = role_client(app, "EMP-003")
    assert request_id in {
        item["request_id"] for item in owner.get("/api/approval-inbox").json()["items"]
    }
    owner_decision = owner.post(
        f"/api/approval-cases/{case_id}/decisions",
        json={
            "approval_step_id": owner_step_id,
            "decision": "approve",
            "comment": "数据负责人确认最小权限。",
        },
    )
    assert owner_decision.status_code == 200, owner_decision.text

    requester_refresh = role_client(app, "EMP-001")
    final_detail = requester_refresh.get(f"/api/requests/{request_id}")
    assert final_detail.status_code == 200
    assert final_detail.json()["approval"]["approval_status"] == "approved"
    assert [
        step["comment"] for step in final_detail.json()["approval"]["steps"]
    ] == ["经理确认业务需要。", "数据负责人确认最小权限。"]
    assert [
        event["actor_id"]
        for event in final_detail.json()["audit_events"]
        if event["event_type"] == "approval.step.approved"
    ] == ["EMP-002", "EMP-003"]
    assert model.calls == 1


def test_wrong_actor_role_self_and_out_of_order_refusals_write_nothing(
    t22_app: tuple[FastAPI, CountingAdvisoryModel],
    database_session_factory: sessionmaker[Session],
) -> None:
    app, model = t22_app
    _, case_id, manager_step_id, owner_step_id = submit_packet_and_case(app)
    with database_session_factory() as session:
        before = approval_snapshot(session, case_id=UUID(case_id))

    owner = role_client(app, "EMP-003")
    admin = role_client(app, "EMP-004")
    requester = role_client(app, "EMP-001")
    assert owner.post(
        f"/api/approval-cases/{case_id}/decisions",
        json={
            "approval_step_id": owner_step_id,
            "decision": "approve",
            "comment": None,
        },
    ).status_code == 409
    admin_existing = admin.post(
        f"/api/approval-cases/{case_id}/decisions",
        json={
            "approval_step_id": manager_step_id,
            "decision": "approve",
            "comment": None,
        },
    )
    admin_random = admin.post(
        f"/api/approval-cases/{uuid4()}/decisions",
        json={
            "approval_step_id": manager_step_id,
            "decision": "approve",
            "comment": None,
        },
    )
    assert admin_existing.status_code == admin_random.status_code == 404
    assert admin_existing.json() == admin_random.json() == {"detail": "审批流不存在"}
    assert requester.post(
        f"/api/approval-cases/{case_id}/decisions",
        json={
            "approval_step_id": manager_step_id,
            "decision": "approve",
            "comment": None,
        },
    ).status_code == 404

    with database_session_factory() as session:
        after = approval_snapshot(session, case_id=UUID(case_id))
    assert after == before
    assert model.calls == 1


def test_reject_requires_reason_and_late_owner_cannot_change_terminal_case(
    t22_app: tuple[FastAPI, CountingAdvisoryModel],
    database_session_factory: sessionmaker[Session],
) -> None:
    app, _ = t22_app
    _, case_id, manager_step_id, owner_step_id = submit_packet_and_case(app)
    manager = role_client(app, "EMP-002")

    with database_session_factory() as session:
        before = approval_snapshot(session, case_id=UUID(case_id))
    blank = manager.post(
        f"/api/approval-cases/{case_id}/decisions",
        json={
            "approval_step_id": manager_step_id,
            "decision": "reject",
            "comment": "   ",
        },
    )
    assert blank.status_code == 422
    with database_session_factory() as session:
        assert approval_snapshot(session, case_id=UUID(case_id)) == before

    rejected = manager.post(
        f"/api/approval-cases/{case_id}/decisions",
        json={
            "approval_step_id": manager_step_id,
            "decision": "reject",
            "comment": "业务证据不足。",
        },
    )
    assert rejected.status_code == 200
    admin = role_client(app, "EMP-004")
    admin_existing = admin.post(
        f"/api/approval-cases/{case_id}/decisions",
        json={
            "approval_step_id": manager_step_id,
            "decision": "approve",
            "comment": None,
        },
    )
    admin_random = admin.post(
        f"/api/approval-cases/{uuid4()}/decisions",
        json={
            "approval_step_id": manager_step_id,
            "decision": "approve",
            "comment": None,
        },
    )
    assert admin_existing.status_code == admin_random.status_code == 404
    assert admin_existing.json() == admin_random.json() == {"detail": "审批流不存在"}
    owner = role_client(app, "EMP-003")
    late = owner.post(
        f"/api/approval-cases/{case_id}/decisions",
        json={
            "approval_step_id": owner_step_id,
            "decision": "approve",
            "comment": "迟到操作",
        },
    )
    assert late.status_code == 409

    with database_session_factory() as session:
        status, steps, events = approval_snapshot(session, case_id=UUID(case_id))
    assert status == "rejected"
    assert [step[3] for step in steps] == ["rejected", "cancelled"]
    assert steps[0][4] == "业务证据不足。"
    assert [(event[0], event[1]) for event in events] == [
        ("approval.step.rejected", "EMP-002")
    ]


def test_concurrent_duplicate_manager_decisions_commit_once(
    t22_app: tuple[FastAPI, CountingAdvisoryModel],
    database_session_factory: sessionmaker[Session],
) -> None:
    app, _ = t22_app
    _, case_id, manager_step_id, _ = submit_packet_and_case(app)
    managers = [role_client(app, "EMP-002"), role_client(app, "EMP-002")]

    def decide(client: TestClient) -> int:
        return client.post(
            f"/api/approval-cases/{case_id}/decisions",
            json={
                "approval_step_id": manager_step_id,
                "decision": "approve",
                "comment": "并发经理审批",
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(decide, managers))

    assert sorted(statuses) == [200, 409]
    with database_session_factory() as session:
        status, steps, events = approval_snapshot(session, case_id=UUID(case_id))
        event_count = session.scalar(
            select(func.count())
            .select_from(AuditEventRecord)
            .where(
                AuditEventRecord.event_type == "approval.step.approved",
                AuditEventRecord.request_id
                == select(ApprovalCaseRecord.request_id)
                .where(ApprovalCaseRecord.id == UUID(case_id))
                .scalar_subquery(),
            )
        )
    assert status == "pending_data_owner"
    assert [step[3] for step in steps] == ["approved", "pending"]
    assert len(events) == event_count == 1


def test_step_role_must_match_server_principal_role(
    t22_app: tuple[FastAPI, CountingAdvisoryModel],
    database_session_factory: sessionmaker[Session],
) -> None:
    app, _ = t22_app
    _, case_id, manager_step_id, _ = submit_packet_and_case(app)
    with database_session_factory() as session:
        step = session.scalar(
            select(ApprovalStepRecord).where(
                ApprovalStepRecord.approval_case_id == UUID(case_id),
                ApprovalStepRecord.step_order == 1,
            )
        )
        assert step is not None
        step.approver_role = "data_owner"
        session.commit()
        before = approval_snapshot(session, case_id=UUID(case_id))

    manager = role_client(app, "EMP-002")
    denied = manager.post(
        f"/api/approval-cases/{case_id}/decisions",
        json={
            "approval_step_id": manager_step_id,
            "decision": "approve",
            "comment": None,
        },
    )
    assert denied.status_code == 404
    with database_session_factory() as session:
        assert approval_snapshot(session, case_id=UUID(case_id)) == before
