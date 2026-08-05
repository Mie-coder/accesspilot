"""validate_access_request 的行为合同。"""

from sqlalchemy.orm import Session

from accesspilot.db.seed import seed_catalog
from accesspilot.domain.models import RequestDraft
from accesspilot.tools.catalog import validate_access_request


def complete_draft(*, duration_days: int = 30) -> RequestDraft:
    """构造一份字段完整的虚构权限申请草稿。"""

    return RequestDraft(
        employee_id="EMP-001",
        entitlement_id="insighthub.customer_export",
        duration_days=duration_days,
        justification="产品运营分析",
    )


def test_accepts_an_eligible_complete_request(database_session: Session) -> None:
    """符合目录资格且字段完整的申请应通过预校验。"""

    seed_catalog(database_session)

    result = validate_access_request(
        database_session,
        complete_draft(),
    )

    assert result.status == "success"
    assert result.issues is None


def test_rejects_unknown_entitlement(database_session: Session) -> None:
    """未知权限必须明确拒绝。"""

    seed_catalog(database_session)

    result = validate_access_request(
        database_session,
        complete_draft().model_copy(
            update={"entitlement_id": "missing.entitlement"}
        ),
    )

    assert result.status == "entitlement_not_found"
    assert result.issues is None


def test_rejects_incomplete_draft(database_session: Session) -> None:
    """缺少申请必填字段时不得通过预校验。"""

    seed_catalog(database_session)

    result = validate_access_request(
        database_session,
        RequestDraft(),
    )

    assert result.status == "validation_failed"
    assert result.issues is not None
    assert result.issues[0].code == "missing_fields:employee_id"
    assert result.issues[0].message == "缺少必填字段：员工编号"


def test_rejects_non_self_service_entitlement(database_session: Session) -> None:
    """禁止自助的权限不能通过普通申请流程。"""

    seed_catalog(database_session)

    result = validate_access_request(
        database_session,
        complete_draft().model_copy(
            update={"entitlement_id": "insighthub.raw_customer_export"}
        ),
    )

    assert result.status == "validation_failed"
    assert result.issues is not None
    assert any(issue.code == "self_service_not_allowed" for issue in result.issues)
    assert any("不允许自助申请" in issue.message for issue in result.issues)


def test_rejects_duration_above_catalog_limit(database_session: Session) -> None:
    """临时权限不能超过目录规定的最长期限。"""

    seed_catalog(database_session)

    result = validate_access_request(
        database_session,
        complete_draft(duration_days=31),
    )

    assert result.status == "validation_failed"
    assert result.issues is not None
    assert any(issue.code == "duration_exceeds_maximum" for issue in result.issues)
    assert any("超过" in issue.message for issue in result.issues)
