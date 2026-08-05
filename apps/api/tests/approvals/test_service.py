from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from accesspilot.approvals import (
    ApprovalActorMismatchError,
    ApprovalAlreadyStartedError,
    ApprovalOutOfOrderError,
    ApprovalRoutingError,
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
from accesspilot.risk.review import PolicyCitation, RiskReview


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
    return token, request.id


def risk_review() -> RiskReview:
    return RiskReview(
        risk_level="high",
        outcome="requires_human_review",
        summary="高风险权限需要两级人工审批。",
        findings=["申请期限为 14 天"],
        citations=[
            PolicyCitation(
                policy_code="POL-003",
                reason="该政策要求直属经理和数据所有者依次审批。",
            )
        ],
    )


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
        review=risk_review(),
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
        "risk_review.completed",
        "approval.started",
    ]

    with pytest.raises(ApprovalAlreadyStartedError):
        start_approval_case(
            database_session,
            workspace_token=token,
            request_id=request_id,
            review=risk_review(),
        )


def test_high_risk_review_cannot_mark_case_clear_and_start_approval(
    database_session: Session,
) -> None:
    token, request_id = create_submitted_request(database_session)
    unsafe_review = risk_review().model_copy(update={"outcome": "clear"})

    with pytest.raises(ApprovalRoutingError):
        start_approval_case(
            database_session,
            workspace_token=token,
            request_id=request_id,
            review=unsafe_review,
        )

    assert database_session.scalar(
        select(ApprovalCaseRecord).where(
            ApprovalCaseRecord.request_id == request_id
        )
    ) is None


def test_correct_approvers_complete_two_steps_in_order(
    database_session: Session,
) -> None:
    token, request_id = create_submitted_request(database_session)
    case = start_approval_case(
        database_session,
        workspace_token=token,
        request_id=request_id,
        review=risk_review(),
    )

    decide_approval(
        database_session,
        workspace_token=token,
        case_id=case.id,
        actor_id="EMP-002",
        decision="approve",
        comment="经理确认业务需要。",
    )
    database_session.refresh(case)
    steps = load_steps(database_session, case.id)
    assert case.approval_status == "pending_data_owner"
    assert [step.step_status for step in steps] == ["approved", "pending"]

    decide_approval(
        database_session,
        workspace_token=token,
        case_id=case.id,
        actor_id="EMP-003",
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
        review=risk_review(),
    )
    audit_count = request_audit_count(database_session, request_id)

    with pytest.raises(ApprovalOutOfOrderError):
        decide_approval(
            database_session,
            workspace_token=token,
            case_id=case.id,
            actor_id="EMP-003",
            decision="approve",
        )
    with pytest.raises(ApprovalActorMismatchError):
        decide_approval(
            database_session,
            workspace_token=token,
            case_id=case.id,
            actor_id="EMP-004",
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
        review=risk_review(),
    )
    decide_approval(
        database_session,
        workspace_token=token,
        case_id=case.id,
        actor_id="EMP-002",
        decision="approve",
    )
    audit_count = request_audit_count(database_session, request_id)

    with pytest.raises(ApprovalStepAlreadyDecidedError):
        decide_approval(
            database_session,
            workspace_token=token,
            case_id=case.id,
            actor_id="EMP-002",
            decision="approve",
        )

    assert request_audit_count(database_session, request_id) == audit_count


def test_same_employee_can_decide_two_distinct_roles_in_order(
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
        review=risk_review(),
    )

    decide_approval(
        database_session,
        workspace_token=token,
        case_id=case.id,
        actor_id="EMP-004",
        decision="approve",
    )
    decide_approval(
        database_session,
        workspace_token=token,
        case_id=case.id,
        actor_id="EMP-004",
        decision="approve",
    )

    database_session.refresh(case)
    assert case.approval_status == "approved"
    assert [step.step_status for step in load_steps(database_session, case.id)] == [
        "approved",
        "approved",
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
        review=risk_review(),
    )

    decide_approval(
        database_session,
        workspace_token=token,
        case_id=case.id,
        actor_id="EMP-002",
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
            workspace_token=token,
            case_id=case.id,
            actor_id="EMP-003",
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
