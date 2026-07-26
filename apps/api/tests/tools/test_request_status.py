"""get_request_status 的行为合同。"""

from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

from sqlalchemy.orm import Session

from accesspilot.db.models import (
    AccessGrantRecord,
    AccessRequestRecord,
    ApprovalCaseRecord,
    WorkspaceRecord,
)
from accesspilot.db.seed import seed_catalog
from accesspilot.tools.catalog import get_request_status


def create_request(database_session: Session) -> AccessRequestRecord:
    """写入一份已提交的虚构申请，供状态查询测试使用。"""

    seed_catalog(database_session)
    workspace = WorkspaceRecord(token_hash=sha256(uuid4().bytes).hexdigest())
    database_session.add(workspace)
    database_session.flush()
    request = AccessRequestRecord(
        workspace_id=workspace.id,
        requester_id="EMP-001",
        entitlement_code="insighthub.customer_export",
        project_code="PROJECT-001",
        data_scope="华东地区脱敏客户数据",
        business_reason="产品运营分析",
        start_date=date(2026, 7, 26),
        duration_days=30,
        request_status="submitted",
        confirmed_at=datetime.now(UTC),
    )
    database_session.add(request)
    database_session.flush()
    return request


def test_returns_request_status_without_approval_or_grant(
    database_session: Session,
) -> None:
    """只有申请记录时，不应虚构审批或开通状态。"""

    request = create_request(database_session)

    result = get_request_status(database_session, str(request.id))

    assert result.status == "success"
    assert result.request_status is not None
    assert result.request_status.request_status == "submitted"
    assert result.request_status.approval_status is None
    assert result.request_status.access_granted is False


def test_returns_approval_and_grant_facts(database_session: Session) -> None:
    """审批与真实授权存在时，应一并返回。"""

    request = create_request(database_session)
    database_session.add(
        ApprovalCaseRecord(
            workspace_id=request.workspace_id,
            request_id=request.id,
            approval_status="approved",
        )
    )
    now = datetime.now(UTC)
    database_session.add(
        AccessGrantRecord(
            workspace_id=request.workspace_id,
            request_id=request.id,
            idempotency_key=f"grant-status-{uuid4()}",
            starts_at=now,
            expires_at=now + timedelta(days=30),
        )
    )
    database_session.commit()

    result = get_request_status(database_session, str(request.id))

    assert result.status == "success"
    assert result.request_status is not None
    assert result.request_status.approval_status == "approved"
    assert result.request_status.access_granted is True


def test_reports_request_not_found(database_session: Session) -> None:
    """格式正确但不存在的申请编号应返回明确业务结果。"""

    result = get_request_status(database_session, str(uuid4()))

    assert result.status == "request_not_found"
    assert result.request_status is None


def test_rejects_invalid_request_id(database_session: Session) -> None:
    """不是 UUID 的输入不应进入数据库查询。"""

    result = get_request_status(database_session, "not-a-request-id")

    assert result.status == "invalid_argument"
    assert result.request_status is None
