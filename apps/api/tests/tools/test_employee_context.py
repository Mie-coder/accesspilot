"""get_employee_context 的行为合同。"""

from sqlalchemy.orm import Session

from accesspilot.db.seed import seed_catalog
from accesspilot.tools.catalog import get_employee_context


def test_returns_employee_and_minimal_manager_context(
    database_session: Session,
) -> None:
    """有直属经理的员工应返回员工资格信息和最小经理身份。"""

    seed_catalog(database_session)

    result = get_employee_context(database_session, "EMP-001")

    assert result.status == "success"
    assert result.employee is not None
    assert result.employee.employee_id == "EMP-001"
    assert result.employee.name == "林晓"
    assert result.employee.department == "product_operations"
    assert result.employee.roles == ["employee"]
    assert result.manager is not None
    assert result.manager.employee_id == "EMP-002"
    assert result.manager.name == "周敏"


def test_returns_success_when_employee_has_no_manager(
    database_session: Session,
) -> None:
    """最高负责人没有直属经理仍是一次成功查询。"""

    seed_catalog(database_session)

    result = get_employee_context(database_session, "EMP-002")

    assert result.status == "success"
    assert result.employee is not None
    assert result.employee.employee_id == "EMP-002"
    assert result.manager is None


def test_rejects_missing_employee_id(database_session: Session) -> None:
    """缺少员工编号时不应尝试查询目录。"""

    result = get_employee_context(database_session, "")

    assert result.status == "invalid_argument"
    assert result.employee is None
    assert result.manager is None


def test_reports_employee_not_found(database_session: Session) -> None:
    """合法但不存在的员工编号应返回明确业务结果。"""

    seed_catalog(database_session)

    result = get_employee_context(database_session, "EMP-404")

    assert result.status == "employee_not_found"
    assert result.employee is None
    assert result.manager is None
