"""search_systems 的行为合同。"""

from sqlalchemy.orm import Session

from accesspilot.db.seed import seed_catalog
from accesspilot.tools.catalog import list_entitlements, search_systems


def test_searches_systems_by_name_or_code(database_session: Session) -> None:
    """系统名称或编码都可以定位到真实目录中的系统。"""

    seed_catalog(database_session)

    by_name = search_systems(database_session, "数据洞察")
    by_code = search_systems(database_session, "codeforge")

    assert by_name.status == "success"
    assert by_name.systems is not None
    assert [(system.code, system.name) for system in by_name.systems] == [
        ("insighthub", "数据洞察中心"),
    ]
    assert by_code.status == "success"
    assert by_code.systems is not None
    assert [(system.code, system.name) for system in by_code.systems] == [
        ("codeforge", "代码协作平台"),
    ]


def test_rejects_blank_system_search_query(database_session: Session) -> None:
    """空白搜索词没有查询目标。"""

    result = search_systems(database_session, "   ")

    assert result.status == "invalid_argument"
    assert result.systems is None


def test_reports_system_not_found(database_session: Session) -> None:
    """无匹配的搜索词应返回明确业务结果。"""

    seed_catalog(database_session)

    result = search_systems(database_session, "不存在的系统")

    assert result.status == "system_not_found"
    assert result.systems is None


def test_lists_entitlements_for_a_known_system(database_session: Session) -> None:
    """工具应只返回目标系统下可申请权限的最小摘要。"""

    seed_catalog(database_session)

    result = list_entitlements(database_session, "insighthub")

    assert result.status == "success"
    assert result.entitlements is not None
    assert [(item.code, item.name, item.risk_level) for item in result.entitlements] == [
        ("insighthub.customer_export", "脱敏客户数据导出", "high"),
        ("insighthub.dashboard_view", "InsightHub 仪表盘查看", "low"),
        ("insighthub.raw_customer_export", "原始客户数据导出", "critical"),
    ]


def test_rejects_blank_system_code_for_entitlements(
    database_session: Session,
) -> None:
    """权限列表必须绑定一个明确系统。"""

    result = list_entitlements(database_session, " ")

    assert result.status == "invalid_argument"
    assert result.entitlements is None


def test_reports_unknown_system_for_entitlements(database_session: Session) -> None:
    """不存在的系统不能被当成一个空权限列表。"""

    seed_catalog(database_session)

    result = list_entitlements(database_session, "missing-system")

    assert result.status == "system_not_found"
    assert result.entitlements is None
