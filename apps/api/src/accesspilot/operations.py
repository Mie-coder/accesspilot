"""审批收件箱与申请全链路只读查询。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict
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


class _PublicRequest(BaseModel):
    """最新申请产品面只公开固定业务字段。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str
    requester_id: str
    requester_name: str
    entitlement_code: str
    duration_days: int
    justification: str
    request_status: str
    confirmed_at: datetime
    created_at: datetime


class _PublicEntitlement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    name: str
    system_code: str
    risk_level: str
    approval_policy: str
    owner_id: str | None


class _PublicRiskCitation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_code: str
    reason: str
    title: str | None
    content: str | None


class _PublicRiskReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    risk_level: str | None
    outcome: str | None
    summary: str | None
    findings: list[str]
    citations: list[_PublicRiskCitation]


class _PublicApprovalStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str
    step_order: int
    approver_id: str
    approver_role: str
    step_status: str
    comment: str | None
    decided_at: datetime | None


class _PublicApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_case_id: str
    approval_status: str
    created_at: datetime
    steps: list[_PublicApprovalStep]


class _PublicProvisioning(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provisioning_status: str
    provisioning_attempt_id: str | None
    attempt_count: int
    last_error: str | None
    updated_at: datetime | None
    access_granted: bool
    grant_id: str | None
    starts_at: datetime | None
    expires_at: datetime | None


class _PublicAuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    audit_event_id: str
    event_type: str
    actor_type: str
    actor_id: str | None
    details: dict[str, str]
    created_at: datetime


class _PublicRequestDetail(BaseModel):
    """最新申请 API 的严格公开 DTO，拒绝未知字段进入产品响应。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    view_mode: str
    request: _PublicRequest
    entitlement: _PublicEntitlement
    risk_review: _PublicRiskReview | None
    approval: _PublicApproval | None
    provisioning: _PublicProvisioning
    audit_events: list[_PublicAuditEvent]


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


_PUBLIC_AUDIT_DETAIL_KEYS = ("status", "next_step", "approver_role")
_PUBLIC_SENSITIVE_MARKERS = (
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "client_secret",
    "idempotency",
    "password",
    "private_key",
    "system_prompt",
    "system prompt",
    "system promote",
    "system instructions",
    "developer prompt",
    "api key",
    "api token",
    "密钥",
    "系统提示词",
    "internal",
    "localhost",
    "127.0.0.1",
    "http://",
    "https://",
    "sk-",
)


def _safe_public_detail_text(value: object) -> str | None:
    """仅保留可展示的短文本，阻止敏感值借允许字段回流。"""

    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.casefold()
    if any(marker in normalized for marker in _PUBLIC_SENSITIVE_MARKERS):
        return None
    return value


def _public_audit_details(value: object) -> dict[str, str]:
    """审计时间线只允许页面确需的三个稳定字段。"""

    if not isinstance(value, dict):
        return {}
    details: dict[str, str] = {}
    for key in _PUBLIC_AUDIT_DETAIL_KEYS:
        safe_value = _safe_public_detail_text(value.get(key))
        if safe_value is not None:
            details[key] = safe_value
    return details


def _public_provisioning_error(status: object) -> str | None:
    """不回显下游异常；只按状态返回固定的业务提示。"""

    if status == "failed":
        return "权限开通失败，请稍后重试或联系人工流程。"
    if status == "unknown":
        return "开通结果未知，请查询原 IAM 操作。"
    return None


def _safe_risk_text(value: object, *, fallback: str) -> str:
    """净化风险模型自由文本；政策正文不经过此函数。"""

    return _safe_public_detail_text(value) or fallback


def _project_public_request_detail(detail: dict[str, object]) -> dict[str, object]:
    """将内部事实投影为严格公开 DTO，丢弃未知键和原始错误。"""

    raw_risk = detail.get("risk_review")
    public_risk: _PublicRiskReview | None = None
    if isinstance(raw_risk, dict):
        raw_findings = raw_risk.get("findings")
        findings = [
            safe_value
            for value in (raw_findings if isinstance(raw_findings, list) else [])
            if (safe_value := _safe_public_detail_text(value)) is not None
        ]
        raw_citations = raw_risk.get("citations")
        citations: list[_PublicRiskCitation] = []
        for citation in raw_citations if isinstance(raw_citations, list) else []:
            if not isinstance(citation, dict):
                continue
            if not isinstance(citation.get("policy_code"), str):
                continue
            if not isinstance(citation.get("reason"), str):
                continue
            citations.append(
                _PublicRiskCitation(
                    policy_code=citation["policy_code"],
                    reason=_safe_risk_text(
                        citation["reason"],
                        fallback="风险审查引用已由政策事实源校验。",
                    ),
                    title=(
                        citation["title"]
                        if isinstance(citation.get("title"), str)
                        else None
                    ),
                    content=(
                        citation["content"]
                        if isinstance(citation.get("content"), str)
                        else None
                    ),
                )
            )
        public_risk = _PublicRiskReview(
            risk_level=(
                raw_risk["risk_level"]
                if isinstance(raw_risk.get("risk_level"), str)
                else None
            ),
            outcome=(
                raw_risk["outcome"]
                if isinstance(raw_risk.get("outcome"), str)
                else None
            ),
            summary=_safe_risk_text(
                raw_risk.get("summary"),
                fallback="风险审查结果已生成，详情按政策事实展示。",
            ),
            findings=findings,
            citations=citations,
        )

    raw_approval = detail.get("approval")
    public_approval: _PublicApproval | None = None
    if isinstance(raw_approval, dict):
        public_steps: list[_PublicApprovalStep] = []
        raw_steps = raw_approval.get("steps")
        for step in raw_steps if isinstance(raw_steps, list) else []:
            if not isinstance(step, dict):
                continue
            public_steps.append(_PublicApprovalStep.model_validate(step))
        public_approval = _PublicApproval.model_validate(
            {
                "approval_case_id": raw_approval.get("approval_case_id"),
                "approval_status": raw_approval.get("approval_status"),
                "created_at": raw_approval.get("created_at"),
                "steps": public_steps,
            }
        )

    raw_provisioning = detail.get("provisioning")
    if not isinstance(raw_provisioning, dict):
        raw_provisioning = {}
    provisioning_status = raw_provisioning.get("provisioning_status")
    public_provisioning = _PublicProvisioning.model_validate(
        {
            "provisioning_status": provisioning_status,
            "provisioning_attempt_id": raw_provisioning.get(
                "provisioning_attempt_id"
            ),
            "attempt_count": raw_provisioning.get("attempt_count", 0),
            "last_error": _public_provisioning_error(provisioning_status),
            "updated_at": raw_provisioning.get("updated_at"),
            "access_granted": raw_provisioning.get("access_granted", False),
            "grant_id": raw_provisioning.get("grant_id"),
            "starts_at": raw_provisioning.get("starts_at"),
            "expires_at": raw_provisioning.get("expires_at"),
        }
    )

    public_events: list[_PublicAuditEvent] = []
    raw_events = detail.get("audit_events")
    for event in raw_events if isinstance(raw_events, list) else []:
        if not isinstance(event, dict):
            continue
        public_events.append(
            _PublicAuditEvent.model_validate(
                {
                    "audit_event_id": event.get("audit_event_id"),
                    "event_type": event.get("event_type"),
                    "actor_type": event.get("actor_type"),
                    "actor_id": event.get("actor_id"),
                    "details": _public_audit_details(event.get("details")),
                    "created_at": event.get("created_at"),
                }
            )
        )

    public_detail = _PublicRequestDetail.model_validate(
        {
            "view_mode": detail.get("view_mode"),
            "request": detail.get("request"),
            "entitlement": detail.get("entitlement"),
            "risk_review": public_risk,
            "approval": public_approval,
            "provisioning": public_provisioning,
            "audit_events": public_events,
        }
    )
    return public_detail.model_dump(mode="json")


def get_latest_request_detail(
    session: Session,
    *,
    workspace_token: str,
) -> dict[str, object] | None:
    """读取当前 Workspace/员工最新正式申请的完整只读事实。

    查询先按 Workspace 和后端绑定的 actor_id 限定，再复用
    :func:`get_request_detail`，避免“最新”接口意外读取其他身份或空间。
    """

    workspace = _load_workspace(session, workspace_token)
    if session.get(EmployeeRecord, workspace.actor_id) is None:
        raise OperationsNotFoundError("当前员工不存在")
    request = session.scalar(
        select(AccessRequestRecord)
        .where(
            AccessRequestRecord.workspace_id == workspace.id,
            AccessRequestRecord.requester_id == workspace.actor_id,
        )
        .order_by(AccessRequestRecord.created_at.desc(), AccessRequestRecord.id.desc())
        .limit(1)
    )
    if request is None:
        return None
    return _project_public_request_detail(
        get_request_detail(
            session,
            workspace_token=workspace_token,
            request_id=request.id,
        )
    )
