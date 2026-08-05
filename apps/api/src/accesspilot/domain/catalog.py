"""虚构企业访问权限目录。"""

from dataclasses import dataclass
from enum import StrEnum


# 风险等级决定资格校验、审批级别和后续风险审查的严格程度。
class RiskLevel(StrEnum):
    LOW = "low"
    HIGH = "high"
    CRITICAL = "critical"


# 审批策略规定申请被提交后必须经过哪些人工决策。
class ApprovalPolicy(StrEnum):
    MANAGER = "manager"
    MANAGER_AND_DATA_OWNER = "manager_and_data_owner"
    MANUAL_SECURITY = "manual_security"


@dataclass(frozen=True)
class Employee:
    """权限目录中的员工身份、组织归属与角色。"""

    # 在整个演示系统中唯一，用于关联申请、审批和授权记录。
    employee_id: str
    name: str
    department: str
    # 直属经理；没有经理时使用 None。
    manager_id: str | None
    # 例如 employee、manager、data_owner、on_call_sre。
    roles: frozenset[str]


@dataclass(frozen=True)
class System:
    """可申请权限的原创虚构企业系统。"""

    # code 是数据库外键和工具调用使用的稳定标识。
    code: str
    # name 是 Agent 和前端展示给用户的中文名称。
    name: str


@dataclass(frozen=True)
class Entitlement:
    """可以被授予的最小权限单位及其安全约束。"""

    # 例如 insighthub.customer_export，是工具和数据库使用的稳定标识。
    code: str
    system_code: str
    name: str
    risk_level: RiskLevel
    approval_policy: ApprovalPolicy
    # 高风险权限的资源/数据所有者，用于第二级审批；无固定所有者时为 None。
    owner_id: str | None
    # False 表示 Agent 不得创建自助申请，必须转人工安全流程。
    self_service_allowed: bool
    # 任一部门或角色匹配，才可能具备申请资格。
    eligible_departments: frozenset[str]
    eligible_roles: frozenset[str]
    # 限时权限的最大天数；None 表示不在目录层设置期限上限。
    max_duration_days: int | None


@dataclass(frozen=True)
class PolicyClause:
    """用于风险审查和后续 RAG 检索的一条虚构政策条款。"""

    # code 用于审计引用；title 用于界面展示；content 是条款正文。
    code: str
    title: str
    content: str

POLICIES = (
    PolicyClause(
        code="POL-001",
        title="申请字段完整性",
        content="权限申请必须包含申请人、具体权限、授权期限和明确的业务理由。",
    ),
    PolicyClause(
        code="POL-002",
        title="最小权限与申请资格",
        content="员工只能申请与其部门或岗位职责相关的最小权限，系统必须先校验申请资格。",
    ),
    PolicyClause(
        code="POL-003",
        title="高风险权限双审批",
        content="高风险权限必须依次经过直属经理和数据所有者审批，任何一级未通过都不得开通。",
    ),
    PolicyClause(
        code="POL-004",
        title="客户数据导出期限与用途",
        content="客户数据导出权限必须说明业务用途并限制使用期限，不得授予无限期访问。",
    ),
    PolicyClause(
        code="POL-005",
        title="原始客户数据禁止自助",
        content=(
            "客户数据导出权限必须限制数据范围和使用期限，"
            "不得授予无限期访问。"
        ),
    ),
    PolicyClause(
        code="POL-006",
        title="禁止自审批",
        content="申请人不得审批自己的权限申请，直属经理和数据所有者必须与申请人身份不同。",
    ),
    PolicyClause(
        code="POL-007",
        title="限时授权与自动回收",
        content="临时权限必须设置最大授权期限，到期后系统应自动回收或进入回收待处理状态。",
    ),
    PolicyClause(
        code="POL-008",
        title="开通失败审计与幂等重试",
        content="权限开通失败必须记录审计事件；重试必须使用幂等键，不能产生重复授权。",
    ),
)    

EMPLOYEES = {
    "EMP-001": Employee(
        employee_id="EMP-001",
        name="林晓",
        department="product_operations",
        manager_id="EMP-002",
        roles=frozenset({"employee"}),
    ),
    "EMP-002": Employee(
        employee_id="EMP-002",
        name="周敏",
        department="product_operations",
        manager_id=None,
        roles=frozenset({"employee", "manager"}),
    ),
    "EMP-003": Employee(
        employee_id="EMP-003",
        name="赵岩",
        department="data_governance",
        manager_id="EMP-004",
        roles=frozenset({"employee", "data_owner"}),
    ),
    "EMP-004": Employee(
        employee_id="EMP-004",
        name="何川",
        department="security_operations",
        manager_id=None,
        roles=frozenset({"employee", "permissions_admin"}),
    ),
    "EMP-005": Employee(
        employee_id="EMP-005",
        name="李然",
        department="sre",
        manager_id="EMP-004",
        roles=frozenset({"employee", "on_call_sre"}),
    ),
}

# 字典键和 code 保持一致，用户界面只展示中文 name。
SYSTEMS = {
    "insighthub": System(code="insighthub", name="数据洞察中心"),
    "codeforge": System(code="codeforge", name="代码协作平台"),
    "opsdesk": System(code="opsdesk", name="运维工作台"),
}

ENTITLEMENTS = {
    "insighthub.dashboard_view": Entitlement(
        code="insighthub.dashboard_view",
        system_code="insighthub",
        name="InsightHub 仪表盘查看",
        risk_level=RiskLevel.LOW,
        approval_policy=ApprovalPolicy.MANAGER,
        owner_id="EMP-003",
        self_service_allowed=True,
        eligible_departments=frozenset(
            {"product_operations", "data_governance"},
        ),
        eligible_roles=frozenset(),
        max_duration_days=180,
    ),
    "insighthub.customer_export": Entitlement(
        code="insighthub.customer_export",
        system_code="insighthub",
        name="脱敏客户数据导出",
        risk_level=RiskLevel.HIGH,
        approval_policy=ApprovalPolicy.MANAGER_AND_DATA_OWNER,
        owner_id="EMP-003",
        self_service_allowed=True,
        eligible_departments=frozenset(
            {"product_operations", "data_governance"},
        ),
        eligible_roles=frozenset(),
        max_duration_days=30,
    ),
    "insighthub.raw_customer_export": Entitlement(
        code="insighthub.raw_customer_export",
        system_code="insighthub",
        name="原始客户数据导出",
        risk_level=RiskLevel.CRITICAL,
        approval_policy=ApprovalPolicy.MANUAL_SECURITY,
        owner_id="EMP-003",
        self_service_allowed=False,
        eligible_departments=frozenset({"data_governance"}),
        eligible_roles=frozenset({"data_owner"}),
        max_duration_days=None,
    ),
    "codeforge.repo_read": Entitlement(
        code="codeforge.repo_read",
        system_code="codeforge",
        name="代码仓库只读",
        risk_level=RiskLevel.LOW,
        approval_policy=ApprovalPolicy.MANAGER,
        owner_id="EMP-002",
        self_service_allowed=True,
        eligible_departments=frozenset(
            {"product_operations", "engineering"},
        ),
        eligible_roles=frozenset(),
        max_duration_days=180,
    ),
    "codeforge.repo_maintain": Entitlement(
        code="codeforge.repo_maintain",
        system_code="codeforge",
        name="代码仓库维护",
        risk_level=RiskLevel.HIGH,
        approval_policy=ApprovalPolicy.MANAGER_AND_DATA_OWNER,
        owner_id="EMP-004",
        self_service_allowed=True,
        eligible_departments=frozenset({"engineering"}),
        eligible_roles=frozenset({"engineer"}),
        max_duration_days=90,
    ),
    "opsdesk.log_view": Entitlement(
        code="opsdesk.log_view",
        system_code="opsdesk",
        name="运维日志查看",
        risk_level=RiskLevel.LOW,
        approval_policy=ApprovalPolicy.MANAGER,
        owner_id="EMP-004",
        self_service_allowed=True,
        eligible_departments=frozenset({"sre"}),
        eligible_roles=frozenset({"on_call_sre"}),
        max_duration_days=30,
    ),
    "opsdesk.production_operator": Entitlement(
        code="opsdesk.production_operator",
        system_code="opsdesk",
        name="生产操作",
        risk_level=RiskLevel.HIGH,
        approval_policy=ApprovalPolicy.MANAGER_AND_DATA_OWNER,
        owner_id="EMP-004",
        self_service_allowed=True,
        eligible_departments=frozenset({"sre"}),
        eligible_roles=frozenset({"on_call_sre"}),
        max_duration_days=7,
    ),
}

# 系统是否允许这名员工通过自助 Agent 发起这项权限申请？
# 1. 查员工
# 2. 查权限
# 3. 权限是否禁止自助？
#    是 → 直接 False
# 4. 员工部门是否在允许部门中？
# 5. 员工角色是否与允许角色有交集？
# 6. 部门匹配 或 角色匹配 → True，否则 False
def is_eligible_to_request(
    employee_id: str,
    entitlement_code: str,
) -> bool:
    employee = EMPLOYEES[employee_id]
    entitlement = ENTITLEMENTS[entitlement_code]

    if not entitlement.self_service_allowed:
        return False

    department_matches = (
        employee.department in entitlement.eligible_departments
    )
    role_matches = bool(
        employee.roles & entitlement.eligible_roles
    )

    return department_matches or role_matches
