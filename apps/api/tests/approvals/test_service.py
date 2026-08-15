from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from accesspilot.agent.embeddings import DeterministicEmbeddingModel
from accesspilot.approvals import (
    ApprovalAlreadyStartedError,
    ApprovalNotFoundError,
    ApprovalOutOfOrderError,
    ApprovalStepAlreadyDecidedError,
    ApprovalTerminalError,
    ApprovalWorkspaceMismatchError,
    decide_approval,
    require_approval_startable,
    start_approval_case,
)
from accesspilot.db.models import (
    AccessRequestRecord,
    ApprovalCaseRecord,
    ApprovalStepRecord,
    AuditEventRecord,
    WorkspaceRecord,
)
from accesspilot.db.seed import seed_catalog
from accesspilot.decision_packets import generate_decision_packet
from accesspilot.rag.policies import index_policy_embeddings


def create_submitted_request(
    session: Session,
    *,
    requester_id: str = "EMP-001",
    entitlement_code: str = "insighthub.customer_export",
    duration_days: int = 14,
) -> tuple[str, UUID]:
    seed_catalog(session)
    token = f"approval-{uuid4()}"
    workspace = WorkspaceRecord(token_hash=sha256(token.encode()).hexdigest())
    session.add(workspace)
    session.flush()
    request = AccessRequestRecord(
        workspace_id=workspace.id,
        requester_id=requester_id,
        entitlement_code=entitlement_code,
        duration_days=duration_days,
        justification="核验虚构项目运营数据",
        request_status="submitted",
        confirmed_at=datetime.now(UTC),
    )
    session.add(request)
    session.commit()
    embedding_model = DeterministicEmbeddingModel()
    index_policy_embeddings(session, embedding_model)
    generate_decision_packet(
        session,
        request_id=request.id,
        actor_id=requester_id,
        embedding_model=embedding_model,
        advisory_model=None,
    )
    return token, request.id


def load_steps(session: Session, case_id: UUID) -> list[ApprovalStepRecord]:
    return list(
        session.scalars(
            select(ApprovalStepRecord)
            .where(ApprovalStepRecord.approval_case_id == case_id)
            .order_by(ApprovalStepRecord.step_order)
        ).all()
    )


def request_audit_count(session: Session, request_id: UUID) -> int:
    return session.scalar(
        select(func.count())
        .select_from(AuditEventRecord)
        .where(AuditEventRecord.request_id == request_id)
    ) or 0


def test_start_case_freezes_manager_then_data_owner_route(
    database_session: Session,
) -> None:
    token, request_id = create_submitted_request(database_session)

    case = start_approval_case(
        database_session,
        workspace_token=token,
        request_id=request_id,
    )

    steps = load_steps(database_session, case.id)
    events = database_session.scalars(
        select(AuditEventRecord)
        .where(AuditEventRecord.request_id == request_id)
        .order_by(AuditEventRecord.created_at)
    ).all()
    assert case.approval_status == "pending_manager"
    assert [step.approver_id for step in steps] == ["EMP-002", "EMP-003"]
    assert [step.approver_role for step in steps] == ["manager", "data_owner"]
    assert [step.step_status for step in steps] == ["pending", "waiting"]
    assert [event.event_type for event in events] == [
        "decision_packet.created",
        "approval.started",
    ]

    with pytest.raises(ApprovalAlreadyStartedError):
        start_approval_case(
            database_session,
            workspace_token=token,
            request_id=request_id,
        )


def test_unavailable_advisory_does_not_block_fixed_route(
    database_session: Session,
) -> None:
    token, request_id = create_submitted_request(database_session)

    case = start_approval_case(
        database_session,
        workspace_token=token,
        request_id=request_id,
    )

    assert case == database_session.scalar(
        select(ApprovalCaseRecord).where(
            ApprovalCaseRecord.request_id == request_id
        )
    )


def test_correct_approvers_complete_two_steps_in_order(
    database_session: Session,
) -> None:
    token, request_id = create_submitted_request(database_session)
    case = start_approval_case(
        database_session,
        workspace_token=token,
        request_id=request_id,
    )
    steps = load_steps(database_session, case.id)

    decide_approval(
        database_session,
        case_id=case.id,
        expected_step_id=steps[0].id,
        actor_id="EMP-002",
        roles={"employee", "manager"},
        decision="approve",
        comment="经理确认业务需要。",
    )
    database_session.refresh(case)
    steps = load_steps(database_session, case.id)
    assert case.approval_status == "pending_data_owner"
    assert [step.step_status for step in steps] == ["approved", "pending"]

    decide_approval(
        database_session,
        case_id=case.id,
        expected_step_id=steps[1].id,
        actor_id="EMP-003",
        roles={"employee", "data_owner"},
        decision="approve",
        comment="数据所有者确认最小权限与期限。",
    )
    database_session.refresh(case)
    steps = load_steps(database_session, case.id)
    decision_events = database_session.scalars(
        select(AuditEventRecord)
        .where(
            AuditEventRecord.request_id == request_id,
            AuditEventRecord.event_type == "approval.step.approved",
        )
        .order_by(AuditEventRecord.created_at)
    ).all()
    assert case.approval_status == "approved"
    assert [step.step_status for step in steps] == ["approved", "approved"]
    assert [event.actor_id for event in decision_events] == ["EMP-002", "EMP-003"]


def test_wrong_actor_and_out_of_order_owner_do_not_change_facts(
    database_session: Session,
) -> None:
    token, request_id = create_submitted_request(database_session)
    case = start_approval_case(
        database_session,
        workspace_token=token,
        request_id=request_id,
    )
    steps = load_steps(database_session, case.id)
    audit_count = request_audit_count(database_session, request_id)

    with pytest.raises(ApprovalOutOfOrderError):
        decide_approval(
            database_session,
            case_id=case.id,
            expected_step_id=steps[1].id,
            actor_id="EMP-003",
            roles={"employee", "data_owner"},
            decision="approve",
        )
    with pytest.raises(ApprovalNotFoundError):
        decide_approval(
            database_session,
            case_id=case.id,
            expected_step_id=steps[0].id,
            actor_id="EMP-004",
            roles={"employee", "permissions_admin"},
            decision="approve",
        )

    database_session.refresh(case)
    assert case.approval_status == "pending_manager"
    assert [step.step_status for step in load_steps(database_session, case.id)] == [
        "pending",
        "waiting",
    ]
    assert request_audit_count(database_session, request_id) == audit_count


def test_duplicate_decision_is_rejected_without_new_audit(
    database_session: Session,
) -> None:
    token, request_id = create_submitted_request(database_session)
    case = start_approval_case(
        database_session,
        workspace_token=token,
        request_id=request_id,
    )
    steps = load_steps(database_session, case.id)
    decide_approval(
        database_session,
        case_id=case.id,
        expected_step_id=steps[0].id,
        actor_id="EMP-002",
        roles={"employee", "manager"},
        decision="approve",
    )
    audit_count = request_audit_count(database_session, request_id)

    with pytest.raises(ApprovalStepAlreadyDecidedError):
        decide_approval(
            database_session,
            case_id=case.id,
            expected_step_id=steps[0].id,
            actor_id="EMP-002",
            roles={"employee", "manager"},
            decision="approve",
        )

    assert request_audit_count(database_session, request_id) == audit_count


def test_same_employee_cannot_decide_steps_without_server_role_match(
    database_session: Session,
) -> None:
    token, request_id = create_submitted_request(
        database_session,
        requester_id="EMP-005",
        entitlement_code="opsdesk.production_operator",
        duration_days=7,
    )
    case = start_approval_case(
        database_session,
        workspace_token=token,
        request_id=request_id,
    )
    steps = load_steps(database_session, case.id)

    with pytest.raises(ApprovalNotFoundError):
        decide_approval(
            database_session,
            case_id=case.id,
            expected_step_id=steps[0].id,
            actor_id="EMP-004",
            roles={"employee", "permissions_admin"},
            decision="approve",
        )

    database_session.refresh(case)
    assert case.approval_status == "pending_manager"
    assert [step.step_status for step in load_steps(database_session, case.id)] == [
        "pending",
        "waiting",
    ]


def test_workspace_scope_is_checked_before_approval_can_start(
    database_session: Session,
) -> None:
    _, request_id = create_submitted_request(database_session)
    other_token = f"other-workspace-{uuid4()}"
    database_session.add(
        WorkspaceRecord(token_hash=sha256(other_token.encode()).hexdigest())
    )
    database_session.commit()

    with pytest.raises(ApprovalWorkspaceMismatchError):
        require_approval_startable(
            database_session,
            workspace_token=other_token,
            request_id=request_id,
        )


def test_rejection_terminates_case_and_cancels_later_step(
    database_session: Session,
) -> None:
    token, request_id = create_submitted_request(database_session)
    case = start_approval_case(
        database_session,
        workspace_token=token,
        request_id=request_id,
    )
    steps = load_steps(database_session, case.id)

    decide_approval(
        database_session,
        case_id=case.id,
        expected_step_id=steps[0].id,
        actor_id="EMP-002",
        roles={"employee", "manager"},
        decision="reject",
        comment="当前理由不足。",
    )
    database_session.refresh(case)
    audit_count = request_audit_count(database_session, request_id)
    assert case.approval_status == "rejected"
    assert [step.step_status for step in load_steps(database_session, case.id)] == [
        "rejected",
        "cancelled",
    ]

    with pytest.raises(ApprovalTerminalError):
        decide_approval(
            database_session,
            case_id=case.id,
            expected_step_id=steps[1].id,
            actor_id="EMP-003",
            roles={"employee", "data_owner"},
            decision="approve",
        )

    rejection = database_session.scalar(
        select(AuditEventRecord).where(
            AuditEventRecord.request_id == request_id,
            AuditEventRecord.event_type == "approval.step.rejected",
        )
    )
    assert rejection is not None
    assert rejection.actor_id == "EMP-002"
    assert request_audit_count(database_session, request_id) == audit_count
