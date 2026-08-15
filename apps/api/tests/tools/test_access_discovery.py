"""身份范围内的可申请权限与有效授权工具合同。"""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

from sqlalchemy.orm import Session

from accesspilot.db.models import (
    AccessGrantRecord,
    AccessRequestRecord,
    WorkspaceRecord,
)
from accesspilot.db.seed import seed_catalog
from accesspilot.tools.catalog import list_active_access, list_eligible_access


def create_workspace(
    database_session: Session,
    *,
    actor_id: str = "EMP-001",
) -> tuple[str, WorkspaceRecord]:
    token = f"access-discovery-{uuid4()}"
    workspace = WorkspaceRecord(
        token_hash=sha256(token.encode()).hexdigest(),
        actor_id=actor_id,
    )
    database_session.add(workspace)
    database_session.flush()
    return token, workspace


def add_grant(
    database_session: Session,
    *,
    workspace: WorkspaceRecord,
    requester_id: str,
    entitlement_code: str,
    starts_at: datetime,
    expires_at: datetime,
) -> AccessGrantRecord:
    request = AccessRequestRecord(
        workspace_id=workspace.id,
        requester_id=requester_id,
        entitlement_code=entitlement_code,
        duration_days=14,
        justification="虚构项目权限查询测试",
        request_status="approved",
        confirmed_at=starts_at,
    )
    database_session.add(request)
    database_session.flush()
    grant = AccessGrantRecord(
        workspace_id=workspace.id,
        request_id=request.id,
        idempotency_key=f"grant-{uuid4()}",
        starts_at=starts_at,
        expires_at=expires_at,
    )
    database_session.add(grant)
    database_session.flush()
    return grant


def test_lists_only_self_service_entitlements_the_employee_can_request(
    database_session: Session,
) -> None:
    seed_catalog(database_session)
    token, _ = create_workspace(database_session)

    result = list_eligible_access(database_session, workspace_token=token)

    assert result.status == "success"
    assert result.eligible_access is not None
    assert [item.code for item in result.eligible_access] == [
        "codeforge.repo_read",
        "insighthub.customer_export",
        "insighthub.dashboard_view",
    ]
    customer_export = result.eligible_access[1]
    assert customer_export.name == "脱敏客户数据导出"
    assert customer_export.system_name == "数据洞察中心"
    assert customer_export.risk_level == "high"
    assert customer_export.max_duration_days == 30
    assert customer_export.approval_policy == "manager_and_data_owner"


def test_eligible_access_returns_empty_success_and_unknown_identity_error(
    database_session: Session,
) -> None:
    seed_catalog(database_session)
    empty_token, _ = create_workspace(database_session, actor_id="EMP-004")
    unknown_token, _ = create_workspace(database_session, actor_id="EMP-999")

    empty = list_eligible_access(database_session, workspace_token=empty_token)
    unknown = list_eligible_access(database_session, workspace_token=unknown_token)

    assert empty.status == "success"
    assert empty.eligible_access == []
    assert unknown.status == "employee_not_found"
    assert unknown.eligible_access is None


def test_active_access_returns_only_current_employee_workspace_and_time_range(
    database_session: Session,
) -> None:
    seed_catalog(database_session)
    token, workspace = create_workspace(database_session)
    _, other_workspace = create_workspace(database_session)
    now = datetime.now(UTC)
    active = add_grant(
        database_session,
        workspace=workspace,
        requester_id="EMP-001",
        entitlement_code="insighthub.dashboard_view",
        starts_at=now - timedelta(days=1),
        expires_at=now + timedelta(days=10),
    )
    add_grant(
        database_session,
        workspace=workspace,
        requester_id="EMP-001",
        entitlement_code="insighthub.customer_export",
        starts_at=now - timedelta(days=20),
        expires_at=now - timedelta(days=1),
    )
    add_grant(
        database_session,
        workspace=workspace,
        requester_id="EMP-001",
        entitlement_code="codeforge.repo_read",
        starts_at=now + timedelta(days=1),
        expires_at=now + timedelta(days=10),
    )
    add_grant(
        database_session,
        workspace=workspace,
        requester_id="EMP-002",
        entitlement_code="codeforge.repo_read",
        starts_at=now - timedelta(days=1),
        expires_at=now + timedelta(days=10),
    )
    add_grant(
        database_session,
        workspace=other_workspace,
        requester_id="EMP-001",
        entitlement_code="insighthub.customer_export",
        starts_at=now - timedelta(days=1),
        expires_at=now + timedelta(days=10),
    )
    database_session.commit()

    result = list_active_access(
        database_session,
        workspace_token=token,
        at=now,
    )

    assert result.status == "success"
    assert result.active_access is not None
    assert len(result.active_access) == 1
    item = result.active_access[0]
    assert item.grant_id == str(active.id)
    assert item.code == "insighthub.dashboard_view"
    assert item.name == "InsightHub 仪表盘查看"
    assert item.system_name == "数据洞察中心"
    assert item.starts_at == active.starts_at
    assert item.expires_at == active.expires_at


def test_active_access_has_stable_empty_and_error_results(
    database_session: Session,
) -> None:
    seed_catalog(database_session)
    token, _ = create_workspace(database_session)
    unknown_employee_token, _ = create_workspace(
        database_session,
        actor_id="EMP-999",
    )
    database_session.commit()

    empty = list_active_access(
        database_session,
        workspace_token=token,
    )
    unknown_employee = list_active_access(
        database_session,
        workspace_token=unknown_employee_token,
    )
    unknown_workspace = list_active_access(
        database_session,
        workspace_token="missing-workspace",
    )

    assert empty.status == "success"
    assert empty.active_access == []
    assert unknown_employee.status == "employee_not_found"
    assert unknown_employee.active_access is None
    assert unknown_workspace.status == "workspace_not_found"
    assert unknown_workspace.active_access is None
