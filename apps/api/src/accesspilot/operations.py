"""审批收件箱与申请全链路只读查询。"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from accesspilot.db.models import (
    AccessGrantRecord,
    AccessRequestRecord,
    ApprovalCaseRecord,
    ApprovalStepRecord,
    AuditEventRecord,
    EmployeeRecord,
    EntitlementRecord,
    PolicyChunkRecord,
    ProvisioningAttemptRecord,
    WorkspaceRecord,
)
from accesspilot.db.workspace_store import hash_workspace_token


class OperationsNotFoundError(LookupError):
    """Workspace、演示身份或申请不存在。"""


def _load_workspace(session: Session, token: str) -> WorkspaceRecord:
    workspace = session.scalar(
        select(WorkspaceRecord).where(
            WorkspaceRecord.token_hash == hash_workspace_token(token)
        )
    )
    if workspace is None:
        raise OperationsNotFoundError("Workspace 不存在")
    return workspace


def list_approval_inbox(
    session: Session,
    *,
    workspace_token: str,
    actor_id: str,
) -> dict[str, object]:
    """只返回当前 Workspace 中真正轮到该演示身份处理的步骤。"""

    workspace = _load_workspace(session, workspace_token)
    actor = session.get(EmployeeRecord, actor_id)
    if actor is None:
        raise OperationsNotFoundError("演示身份不存在")

    rows = session.execute(
        select(
            ApprovalStepRecord,
            ApprovalCaseRecord,
            AccessRequestRecord,
            EmployeeRecord,
            EntitlementRecord,
        )
        .join(
            ApprovalCaseRecord,
            ApprovalCaseRecord.id == ApprovalStepRecord.approval_case_id,
        )
        .join(
            AccessRequestRecord,
            AccessRequestRecord.id == ApprovalCaseRecord.request_id,
        )
        .join(
            EmployeeRecord,
            EmployeeRecord.employee_id == AccessRequestRecord.requester_id,
        )
        .join(
            EntitlementRecord,
            EntitlementRecord.code == AccessRequestRecord.entitlement_code,
        )
        .where(
            ApprovalStepRecord.workspace_id == workspace.id,
            ApprovalStepRecord.approver_id == actor.employee_id,
            # waiting 表示尚未轮到，必须由查询层排除，不能交给 UI 假装不可点。
            ApprovalStepRecord.step_status == "pending",
        )
        .order_by(ApprovalStepRecord.created_at, ApprovalStepRecord.id)
    ).all()

    return {
        "actor": {
            "employee_id": actor.employee_id,
            "name": actor.name,
            "roles": actor.roles,
        },
        "items": [
            {
                "request_id": str(request.id),
                "approval_case_id": str(case.id),
                "approval_step_id": str(step.id),
                "step_order": step.step_order,
                "approver_role": step.approver_role,
                "step_status": step.step_status,
                "approval_status": case.approval_status,
                "requester_id": request.requester_id,
                "requester_name": requester.name,
                "entitlement_code": request.entitlement_code,
                "entitlement_name": entitlement.name,
                "risk_level": entitlement.risk_level,
                "duration_days": request.duration_days,
                "justification": request.justification,
                "submitted_at": request.created_at,
            }
            for step, case, request, requester, entitlement in rows
        ],
    }


def _risk_review_payload(
    session: Session,
    audit_events: list[AuditEventRecord],
) -> dict[str, object] | None:
    risk_event = next(
        (event for event in reversed(audit_events) if event.event_type == "risk_review.completed"),
        None,
    )
    if risk_event is None:
        return None

    stored = risk_event.details
    raw_citations = stored.get("citations")
    citations = raw_citations if isinstance(raw_citations, list) else []
    codes = {
        citation.get("policy_code")
        for citation in citations
        if isinstance(citation, dict) and isinstance(citation.get("policy_code"), str)
    }
    policies = {
        policy.policy_code: policy
        for policy in session.scalars(
            select(PolicyChunkRecord).where(PolicyChunkRecord.policy_code.in_(codes))
        ).all()
    }
    enriched_citations: list[dict[str, object]] = []
    for citation in citations:
        if not isinstance(citation, dict):
            continue
        code = citation.get("policy_code")
        reason = citation.get("reason")
        if not isinstance(code, str) or not isinstance(reason, str):
            continue
        policy = policies.get(code)
        enriched_citations.append(
            {
                "policy_code": code,
                "reason": reason,
                "title": policy.title if policy is not None else None,
                "content": policy.content if policy is not None else None,
            }
        )

    return {
        "risk_level": stored.get("risk_level"),
        "outcome": stored.get("outcome"),
        "summary": stored.get("summary"),
        "findings": stored.get("findings", []),
        "citations": enriched_citations,
    }


def get_request_detail(
    session: Session,
    *,
    workspace_token: str,
    request_id: UUID,
) -> dict[str, object]:
    """返回一份申请已落库的完整事实，不从审批状态猜测是否已授权。"""

    workspace = _load_workspace(session, workspace_token)
    request = session.get(AccessRequestRecord, request_id)
    if request is None or request.workspace_id != workspace.id:
        raise OperationsNotFoundError("申请不存在")
    requester = session.get(EmployeeRecord, request.requester_id)
    entitlement = session.get(EntitlementRecord, request.entitlement_code)
    if requester is None or entitlement is None:
        raise OperationsNotFoundError("申请目录信息不存在")

    case = session.scalar(
        select(ApprovalCaseRecord).where(
            ApprovalCaseRecord.workspace_id == workspace.id,
            ApprovalCaseRecord.request_id == request.id,
        )
    )
    steps = (
        list(
            session.scalars(
                select(ApprovalStepRecord)
                .where(
                    ApprovalStepRecord.workspace_id == workspace.id,
                    ApprovalStepRecord.approval_case_id == case.id,
                )
                .order_by(ApprovalStepRecord.step_order)
            ).all()
        )
        if case is not None
        else []
    )
    attempt = session.scalar(
        select(ProvisioningAttemptRecord).where(
            ProvisioningAttemptRecord.workspace_id == workspace.id,
            ProvisioningAttemptRecord.request_id == request.id,
        )
    )
    grant = session.scalar(
        select(AccessGrantRecord).where(
            AccessGrantRecord.workspace_id == workspace.id,
            AccessGrantRecord.request_id == request.id,
        )
    )
    audit_events = list(
        session.scalars(
            select(AuditEventRecord)
            .where(
                AuditEventRecord.workspace_id == workspace.id,
                AuditEventRecord.request_id == request.id,
            )
            .order_by(AuditEventRecord.created_at, AuditEventRecord.id)
        ).all()
    )

    return {
        # 详情 GET 是可重复读取的事实回放；所有写动作使用独立守卫端点。
        "view_mode": "read_only_replay",
        "fault_mode": workspace.fault_mode,
        "request": {
            "request_id": str(request.id),
            "requester_id": request.requester_id,
            "requester_name": requester.name,
            "entitlement_code": request.entitlement_code,
            "duration_days": request.duration_days,
            "justification": request.justification,
            "request_status": request.request_status,
            "confirmed_at": request.confirmed_at,
            "created_at": request.created_at,
        },
        "entitlement": {
            "code": entitlement.code,
            "name": entitlement.name,
            "system_code": entitlement.system_code,
            "risk_level": entitlement.risk_level,
            "approval_policy": entitlement.approval_policy,
            "owner_id": entitlement.owner_id,
        },
        "risk_review": _risk_review_payload(session, audit_events),
        "approval": (
            {
                "approval_case_id": str(case.id),
                "approval_status": case.approval_status,
                "created_at": case.created_at,
                "steps": [
                    {
                        "step_id": str(step.id),
                        "step_order": step.step_order,
                        "approver_id": step.approver_id,
                        "approver_role": step.approver_role,
                        "step_status": step.step_status,
                        "comment": step.comment,
                        "decided_at": step.decided_at,
                    }
                    for step in steps
                ],
            }
            if case is not None
            else None
        ),
        "provisioning": {
            "provisioning_status": (
                attempt.provisioning_status if attempt is not None else "not_started"
            ),
            "provisioning_attempt_id": str(attempt.id) if attempt is not None else None,
            "attempt_count": attempt.attempt_count if attempt is not None else 0,
            "last_error": attempt.last_error if attempt is not None else None,
            "updated_at": attempt.updated_at if attempt is not None else None,
            # 只有真实授权记录存在才是已开通，审批通过不等于授权成功。
            "access_granted": grant is not None,
            "grant_id": str(grant.id) if grant is not None else None,
            "starts_at": grant.starts_at if grant is not None else None,
            "expires_at": grant.expires_at if grant is not None else None,
        },
        "audit_events": [
            {
                "audit_event_id": str(event.id),
                "event_type": event.event_type,
                "actor_type": event.actor_type,
                "actor_id": event.actor_id,
                "details": event.details,
                "created_at": event.created_at,
            }
            for event in audit_events
        ],
    }
