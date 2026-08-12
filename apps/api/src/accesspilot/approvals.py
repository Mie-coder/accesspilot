"""两级人工审批的创建、顺序守卫与审计用例。"""

from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from accesspilot.db.models import (
    AccessRequestRecord,
    ApprovalCaseRecord,
    ApprovalStepRecord,
    AuditEventRecord,
    DecisionPacketRecord,
    EmployeeRecord,
    EntitlementRecord,
    WorkspaceRecord,
    utc_now,
)
from accesspilot.db.workspace_store import hash_workspace_token
from accesspilot.domain.catalog import ApprovalPolicy
from accesspilot.domain.workflow import ApprovalStatus


class ApprovalError(RuntimeError):
    """人工审批业务错误基类。"""


class ApprovalNotFoundError(ApprovalError):
    """目标申请、审批流或 Workspace 不存在。"""


class ApprovalWorkspaceMismatchError(ApprovalError):
    """当前 Workspace 无权访问目标审批流。"""


class ApprovalAlreadyStartedError(ApprovalError):
    """一份申请只能创建一个审批流。"""


class ApprovalRoutingError(ApprovalError):
    """目录缺少可执行的审批路由。"""


class DecisionPacketRequiredError(ApprovalError):
    """启动审批前必须已存在冻结的 Decision Packet。"""


class ApprovalActorMismatchError(ApprovalError):
    """当前员工不是这份审批流的指定审批人。"""


class ApprovalOutOfOrderError(ApprovalError):
    """审批人存在于流程中，但尚未轮到该步骤。"""


class ApprovalStepAlreadyDecidedError(ApprovalError):
    """同一审批步骤不能重复决定。"""


class ApprovalTerminalError(ApprovalError):
    """审批流已通过或驳回，不能继续决定。"""


def _load_workspace(session: Session, token: str) -> WorkspaceRecord:
    workspace = session.scalar(
        select(WorkspaceRecord).where(
            WorkspaceRecord.token_hash == hash_workspace_token(token)
        )
    )
    if workspace is None:
        raise ApprovalNotFoundError("Workspace 不存在")
    return workspace


def _require_workspace_scope(
    workspace: WorkspaceRecord,
    workspace_id: UUID,
) -> None:
    if workspace.id != workspace_id:
        raise ApprovalWorkspaceMismatchError("审批流不属于当前 Workspace")


def build_approval_route(
    requester: EmployeeRecord,
    entitlement: EntitlementRecord,
) -> list[tuple[str, str]]:
    if requester.manager_id is None or requester.manager_id == requester.employee_id:
        raise ApprovalRoutingError("申请人没有可用的直属经理审批人")

    route = [(requester.manager_id, "manager")]
    if entitlement.approval_policy == ApprovalPolicy.MANAGER.value:
        return route
    if entitlement.approval_policy != ApprovalPolicy.MANAGER_AND_DATA_OWNER.value:
        raise ApprovalRoutingError("该权限必须转人工安全流程")
    if entitlement.owner_id is None or entitlement.owner_id == requester.employee_id:
        raise ApprovalRoutingError("权限没有可用的数据所有者审批人")
    route.append((entitlement.owner_id, "data_owner"))
    return route


def require_approval_startable(
    session: Session,
    *,
    request_id: UUID,
    actor_id: str | None = None,
    workspace_token: str | None = None,
) -> None:
    """在任何写入前验证 requester ACL、状态、Packet 与重复启动。"""

    request = session.get(AccessRequestRecord, request_id)
    if request is None:
        raise ApprovalNotFoundError("申请不存在")
    if actor_id is not None:
        if request.requester_id != actor_id:
            raise ApprovalNotFoundError("申请不存在")
    elif workspace_token is not None:
        workspace = _load_workspace(session, workspace_token)
        _require_workspace_scope(workspace, request.workspace_id)
    else:
        raise ApprovalNotFoundError("申请不存在")
    if request.request_status != "submitted":
        raise ApprovalRoutingError("只有已提交申请才能启动审批")
    if session.scalar(
        select(ApprovalCaseRecord).where(
            ApprovalCaseRecord.request_id == request_id
        )
    ) is not None:
        raise ApprovalAlreadyStartedError("审批流已经创建")
    if session.scalar(
        select(DecisionPacketRecord.id).where(
            DecisionPacketRecord.request_id == request_id
        )
    ) is None:
        raise DecisionPacketRequiredError("决策材料尚未生成")


def start_approval_case(
    session: Session,
    *,
    request_id: UUID,
    actor_id: str | None = None,
    workspace_token: str | None = None,
) -> ApprovalCaseRecord:
    """从冻结 Packet 读取路线，并用当前目录二次校验。"""

    request = session.get(AccessRequestRecord, request_id, with_for_update=True)
    if request is None:
        raise ApprovalNotFoundError("申请不存在")
    if actor_id is not None:
        if request.requester_id != actor_id:
            raise ApprovalNotFoundError("申请不存在")
    elif workspace_token is not None:
        workspace = _load_workspace(session, workspace_token)
        _require_workspace_scope(workspace, request.workspace_id)
    else:
        raise ApprovalNotFoundError("申请不存在")
    if request.request_status != "submitted":
        raise ApprovalRoutingError("只有已提交申请才能启动审批")
    if session.scalar(
        select(ApprovalCaseRecord).where(
            ApprovalCaseRecord.request_id == request_id
        )
    ) is not None:
        raise ApprovalAlreadyStartedError("审批流已经创建")

    requester = session.get(EmployeeRecord, request.requester_id)
    entitlement = session.get(EntitlementRecord, request.entitlement_code)
    if requester is None or entitlement is None:
        raise ApprovalRoutingError("申请引用的目录信息不存在")
    packet = session.scalar(
        select(DecisionPacketRecord).where(
            DecisionPacketRecord.request_id == request.id
        )
    )
    if packet is None:
        raise DecisionPacketRequiredError("决策材料尚未生成")
    raw_route = packet.frozen_content.get("fixed_route")
    if not isinstance(raw_route, list):
        raise ApprovalRoutingError("决策材料中的审批路线无效")
    route: list[tuple[str, str]] = []
    for index, raw_step in enumerate(raw_route, start=1):
        if not isinstance(raw_step, dict) or raw_step.get("step_order") != index:
            raise ApprovalRoutingError("决策材料中的审批顺序无效")
        approver_id = raw_step.get("approver_id")
        approver_role = raw_step.get("approver_role")
        if not isinstance(approver_id, str) or not isinstance(approver_role, str):
            raise ApprovalRoutingError("决策材料中的审批路线无效")
        route.append((approver_id, approver_role))
    if route != build_approval_route(requester, entitlement):
        raise ApprovalRoutingError("决策材料与当前目录路线不一致")
    case = ApprovalCaseRecord(
        workspace_id=request.workspace_id,
        request_id=request.id,
        approval_status=ApprovalStatus.PENDING_MANAGER.value,
    )

    try:
        session.add(case)
        session.flush()
        for index, (approver_id, role) in enumerate(route, start=1):
            session.add(
                ApprovalStepRecord(
                    workspace_id=request.workspace_id,
                    approval_case_id=case.id,
                    step_order=index,
                    approver_id=approver_id,
                    approver_role=role,
                    step_status="pending" if index == 1 else "waiting",
                )
            )
        session.add(
            AuditEventRecord(
                workspace_id=request.workspace_id,
                request_id=request.id,
                actor_type="employee",
                actor_id=request.requester_id,
                event_type="approval.started",
                details={
                    "approval_case_id": str(case.id),
                    "route": [
                        {
                            "step_order": index,
                            "approver_id": approver_id,
                            "approver_role": role,
                        }
                        for index, (approver_id, role) in enumerate(route, start=1)
                    ],
                },
            )
        )
        session.commit()
    except Exception:
        session.rollback()
        raise

    session.refresh(case)
    return case


def decide_approval(
    session: Session,
    *,
    workspace_token: str,
    case_id: UUID,
    actor_id: str,
    decision: Literal["approve", "reject"],
    comment: str | None = None,
) -> ApprovalCaseRecord:
    """由当前指定审批人决定一步，并按顺序推进或终止流程。"""

    if decision not in {"approve", "reject"}:
        raise ApprovalError("审批决定只能是 approve 或 reject")
    workspace = _load_workspace(session, workspace_token)
    case = session.get(ApprovalCaseRecord, case_id, with_for_update=True)
    if case is None:
        raise ApprovalNotFoundError("审批流不存在")
    _require_workspace_scope(workspace, case.workspace_id)
    if case.approval_status in {
        ApprovalStatus.APPROVED.value,
        ApprovalStatus.REJECTED.value,
    }:
        raise ApprovalTerminalError("审批流已经结束")

    steps = list(
        session.scalars(
            select(ApprovalStepRecord)
            .where(ApprovalStepRecord.approval_case_id == case.id)
            .order_by(ApprovalStepRecord.step_order)
            .with_for_update()
        ).all()
    )
    current_step = next(
        (step for step in steps if step.step_status == "pending"),
        None,
    )
    if current_step is None:
        raise ApprovalTerminalError("审批流没有待处理步骤")
    if current_step.approver_id != actor_id:
        actor_steps = [step for step in steps if step.approver_id == actor_id]
        if not actor_steps:
            raise ApprovalActorMismatchError("当前员工不是指定审批人")
        if any(step.step_status == "waiting" for step in actor_steps):
            raise ApprovalOutOfOrderError("前序审批尚未完成")
        raise ApprovalStepAlreadyDecidedError("该审批步骤已经决定")

    request = session.get(AccessRequestRecord, case.request_id)
    if request is None:
        raise ApprovalNotFoundError("审批流引用的申请不存在")
    if actor_id == request.requester_id:
        raise ApprovalActorMismatchError("申请人不能审批自己的申请")

    normalized_comment = comment.strip() if comment is not None else None
    normalized_comment = normalized_comment or None
    decided_at = utc_now()
    current_step.comment = normalized_comment
    current_step.decided_at = decided_at

    if decision == "reject":
        current_step.step_status = "rejected"
        case.approval_status = ApprovalStatus.REJECTED.value
        for step in steps:
            if step.step_order > current_step.step_order and step.step_status == "waiting":
                step.step_status = "cancelled"
        event_type = "approval.step.rejected"
    else:
        current_step.step_status = "approved"
        next_step = next(
            (
                step
                for step in steps
                if step.step_order > current_step.step_order
                and step.step_status == "waiting"
            ),
            None,
        )
        if next_step is None:
            case.approval_status = ApprovalStatus.APPROVED.value
        else:
            next_step.step_status = "pending"
            case.approval_status = ApprovalStatus.PENDING_DATA_OWNER.value
        event_type = "approval.step.approved"

    try:
        session.add(
            AuditEventRecord(
                workspace_id=case.workspace_id,
                request_id=case.request_id,
                actor_type="employee",
                actor_id=actor_id,
                event_type=event_type,
                details={
                    "approval_case_id": str(case.id),
                    "approval_step_id": str(current_step.id),
                    "step_order": current_step.step_order,
                    "approver_role": current_step.approver_role,
                    "comment": normalized_comment,
                },
            )
        )
        session.commit()
    except Exception:
        session.rollback()
        raise

    session.refresh(case)
    return case
