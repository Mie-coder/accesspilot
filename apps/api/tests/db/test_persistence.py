"""跨 Session 的 PostgreSQL 持久化集成测试。"""

from datetime import UTC, date, datetime
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.db.models import (
    AccessRequestRecord,
    AuditEventRecord,
    WorkspaceRecord,
)
from accesspilot.db.seed import seed_catalog


def test_new_session_reads_committed_request_and_audit(
    database_session_factory: sessionmaker[Session],
) -> None:
    """第一个 Session 关闭后，新 Session 仍能读到已提交数据。"""

    with database_session_factory() as first:
        seed_catalog(first)
        workspace = WorkspaceRecord(
            token_hash=sha256(uuid4().bytes).hexdigest(),
        )
        first.add(workspace)
        # flush 先让 ORM 生成 workspace.id，但此时事务还没有提交。
        first.flush()

        request = AccessRequestRecord(
            workspace_id=workspace.id,
            requester_id="EMP-001",
            entitlement_code="insighthub.customer_export",
            project_code="PRJ-AURORA",
            data_scope="华南区脱敏客户数据",
            business_reason="核验项目运营数据",
            start_date=date(2026, 7, 20),
            duration_days=14,
            request_status="submitted",
            confirmed_at=datetime.now(UTC),
        )
        first.add(request)
        # 审计事件需要 request.id，所以再次 flush 获取 UUID。
        first.flush()

        first.add(
            AuditEventRecord(
                workspace_id=workspace.id,
                request_id=request.id,
                actor_type="employee",
                actor_id="EMP-001",
                event_type="request.submitted",
                details={"project_code": "PRJ-AURORA"},
            )
        )
        request_id = request.id
        # commit 完成事务，使数据可被其他 Session 读取。
        first.commit()

    with database_session_factory() as second:
        stored_request = second.get(AccessRequestRecord, request_id)
        stored_audit = second.scalar(
            select(AuditEventRecord).where(
                AuditEventRecord.request_id == request_id
            )
        )

        assert stored_request is not None
        assert stored_request.project_code == "PRJ-AURORA"
        assert stored_audit is not None
        assert stored_audit.event_type == "request.submitted"
