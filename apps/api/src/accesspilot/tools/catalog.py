
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from accesspilot.db.models import (
    AccessGrantRecord,
    AccessRequestRecord,
    ApprovalCaseRecord,
    EmployeeRecord,
    EntitlementRecord,
    SystemRecord,
)
from accesspilot.domain.models import RequestDraft

'''给审批流程看的员工资料包'''
class EmployeeContext(BaseModel):
    employee_id:str
    name:str
    department:str
    roles:list[str]

"""经理摘要"""
class ManagerSummary(BaseModel):
    employee_id:str
    name:str

class SystemSummary(BaseModel):
    code:str
    name:str


class EntitlementSummary(BaseModel):
    code: str
    name: str
    risk_level: str


class ValidationIssue(BaseModel):
    """一条稳定错误代码及其面向用户的中文提示。"""

    code: str
    message: str


class RequestStatusSummary(BaseModel):
    """一份申请可由数据库证实的当前状态。"""

    request_status: str
    approval_status: str | None
    access_granted: bool


"""🙆‍♂️结果单"""
class ToolResult(BaseModel):
    """外层说明工具调用结果，内部字段承载具体业务事实。"""

    status: Literal[
        "success",
        "invalid_argument",
        "employee_not_found",
        "system_not_found",
        "entitlement_not_found",
        "validation_failed",
        "request_not_found",
    ]
    employee:EmployeeContext | None = None
    manager:ManagerSummary | None = None
    systems:list[SystemSummary] | None = None
    entitlements: list[EntitlementSummary] | None = None
    issues: list[ValidationIssue] | None = None
    request_status: RequestStatusSummary | None = None

def get_employee_context(session: Session, employee_id: str) -> ToolResult:
    """按员工编号查询审批所需的员工和直属经理信息。"""

    # 工号为空时没有明确查询目标，不访问数据库。
    if not employee_id:
        return ToolResult(status="invalid_argument")
    
    # employee_id 是主键，直接读取目标员工记录。
    employee = session.get(EmployeeRecord, employee_id)
    if employee is None:
        return ToolResult(status="employee_not_found")
    manager: ManagerSummary | None = None

    # 最高负责人没有直属经理；其余员工才需要查询经理记录。
    if employee.manager_id is not None:
        manager_record = session.get(EmployeeRecord, employee.manager_id)
        if manager_record is not None:
            manager = ManagerSummary(
                employee_id=manager_record.employee_id,
                name=manager_record.name,
            )

    # 只返回后续资格校验和审批路由所需的最小员工上下文。
    return ToolResult(
    status="success",
    employee=EmployeeContext(
        employee_id=employee.employee_id,
        name=employee.name,
        department=employee.department,
        roles=employee.roles,
    ),
    manager=manager,
)


def search_systems(session: Session, query: str) -> ToolResult:
    """按名称或编码搜索可申请系统，并返回最小系统摘要。"""

    normalized_query = query.strip()
    # 空白词没有搜索目标，避免返回整份系统目录。
    if not normalized_query:
        return ToolResult(status="invalid_argument")

    pattern = f"%{normalized_query}%"
    # 名称和稳定编码都支持模糊匹配，方便 Agent 处理不同表达。
    records = session.scalars(
        select(SystemRecord)
        .where(
            or_(
                SystemRecord.name.ilike(pattern),
                SystemRecord.code.ilike(pattern),
            )
        )
        .order_by(SystemRecord.code)
    ).all()
    if not records:
        return ToolResult(status="system_not_found")

    return ToolResult(
        status="success",
        systems=[
            SystemSummary(code=record.code, name=record.name)
            for record in records
        ],
    )


def list_entitlements(session: Session, system_code: str) -> ToolResult:
    """列出指定系统下可申请权限的最小摘要。"""

    normalized_code = system_code.strip()
    # 权限列表必须有明确所属系统，不能把所有权限混在一起返回。
    if not normalized_code:
        return ToolResult(status="invalid_argument")

    # 先确认系统存在，避免把“系统不存在”误报成“没有权限”。
    if session.get(SystemRecord, normalized_code) is None:
        return ToolResult(status="system_not_found")

    records = session.scalars(
        select(EntitlementRecord)
        .where(EntitlementRecord.system_code == normalized_code)
        .order_by(EntitlementRecord.code)
    ).all()
    return ToolResult(
        status="success",
        entitlements=[
            EntitlementSummary(
                code=record.code,
                name=record.name,
                risk_level=record.risk_level,
            )
            for record in records
        ],
    )


def validate_access_request(
    session: Session,
    draft: RequestDraft,
) -> ToolResult:
    """按目录规则预校验一份权限申请。"""

    field_labels = {
        "employee_id": "员工编号",
        "entitlement_id": "权限编号",
        "duration_days": "申请期限",
        "justification": "申请理由",
    }
    issues: list[ValidationIssue] = [
        ValidationIssue(
            code=f"missing_fields:{field_name}",
            message=f"缺少必填字段：{field_labels[field_name]}",
        )
        for field_name in draft.missing_fields()
    ]

    # 草稿字段不完整时无法查询目录；一次返回全部缺项供 Agent 继续补全。
    if draft.employee_id is None or draft.entitlement_id is None:
        return ToolResult(status="validation_failed", issues=issues)

    employee = session.get(EmployeeRecord, draft.employee_id)
    if employee is None:
        return ToolResult(status="employee_not_found")

    entitlement = session.get(EntitlementRecord, draft.entitlement_id)
    if entitlement is None:
        return ToolResult(status="entitlement_not_found")

    # 目录禁止自助时，普通 Agent 流程不能创建该申请。
    if not entitlement.self_service_allowed:
        issues.append(
            ValidationIssue(
                code="self_service_not_allowed",
                message="该权限不允许自助申请，请转人工安全流程。",
            )
        )

    department_matches = employee.department in entitlement.eligible_departments
    role_matches = bool(set(employee.roles) & set(entitlement.eligible_roles))
    if not department_matches and not role_matches:
        issues.append(
            ValidationIssue(
                code="requester_not_eligible",
                message="当前员工的部门和角色均不符合该权限的申请资格。",
            )
        )

    if (
        draft.duration_days is not None
        and entitlement.max_duration_days is not None
        and draft.duration_days > entitlement.max_duration_days
    ):
        issues.append(
            ValidationIssue(
                code="duration_exceeds_maximum",
                message="申请期限超过该权限允许的最长时限。",
            )
        )

    if issues:
        return ToolResult(status="validation_failed", issues=issues)
    return ToolResult(status="success")


def get_request_status(session: Session, request_id: str) -> ToolResult:
    """查询申请、审批和真实授权的已持久化状态。"""

    try:
        parsed_request_id = UUID(request_id)
    except (TypeError, ValueError):
        return ToolResult(status="invalid_argument")

    request = session.get(AccessRequestRecord, parsed_request_id)
    if request is None:
        return ToolResult(status="request_not_found")

    approval_case = session.scalar(
        select(ApprovalCaseRecord).where(
            ApprovalCaseRecord.request_id == request.id
        )
    )
    # AccessGrant 只在 IAM 真正开通成功后创建，存在即代表已授权。
    access_granted = session.scalar(
        select(AccessGrantRecord.id).where(AccessGrantRecord.request_id == request.id)
    ) is not None
    return ToolResult(
        status="success",
        request_status=RequestStatusSummary(
            request_status=request.request_status,
            approval_status=(
                approval_case.approval_status if approval_case is not None else None
            ),
            access_granted=access_granted,
        ),
    )
