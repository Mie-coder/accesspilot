"""将原创虚构业务目录幂等加载到 PostgreSQL。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from accesspilot.db.models import (
    EmployeeRecord,
    EntitlementRecord,
    PolicyChunkRecord,
    SystemRecord,
)
from accesspilot.domain.catalog import EMPLOYEES, ENTITLEMENTS, POLICIES, SYSTEMS


def seed_catalog(session: Session) -> None:
    """创建或更新虚构目录，重复调用不会产生重复数据。"""

    try:
        # 第一遍先写入所有员工，不立即设置 manager_id。
        # 否则 EMP-001 可能在 EMP-002 存在之前就触发自关联外键。
        employees: dict[str, EmployeeRecord] = {}
        for employee in EMPLOYEES.values():
            employees[employee.employee_id] = session.merge(
                EmployeeRecord(
                    employee_id=employee.employee_id,
                    name=employee.name,
                    department=employee.department,
                    manager_id=None,
                    roles=sorted(employee.roles),
                )
            )
        session.flush()

        # 所有员工主键都已存在，第二遍再安全建立上下级关系。
        for employee in EMPLOYEES.values():
            employees[employee.employee_id].manager_id = employee.manager_id

        # merge 以主键 code 查找旧记录：存在就更新，不存在才插入。
        for system in SYSTEMS.values():
            session.merge(SystemRecord(code=system.code, name=system.name))
        session.flush()

        # 权限引用系统和员工 Owner，因此必须在两类父记录存在后写入。
        for entitlement in ENTITLEMENTS.values():
            session.merge(
                EntitlementRecord(
                    code=entitlement.code,
                    system_code=entitlement.system_code,
                    name=entitlement.name,
                    risk_level=entitlement.risk_level.value,
                    approval_policy=entitlement.approval_policy.value,
                    owner_id=entitlement.owner_id,
                    self_service_allowed=entitlement.self_service_allowed,
                    eligible_departments=sorted(entitlement.eligible_departments),
                    eligible_roles=sorted(entitlement.eligible_roles),
                    max_duration_days=entitlement.max_duration_days,
                )
            )

        # PolicyChunk 的主键是随机 UUID，幂等查找要使用业务唯一键。
        for policy in POLICIES:
            chunk = session.scalar(
                select(PolicyChunkRecord).where(
                    PolicyChunkRecord.policy_code == policy.code,
                    PolicyChunkRecord.chunk_index == 0,
                )
            )
            if chunk is None:
                session.add(
                    PolicyChunkRecord(
                        policy_code=policy.code,
                        title=policy.title,
                        chunk_index=0,
                        content=policy.content,
                        chunk_metadata={"source": "fictional_access_policy"},
                        embedding=None,
                    )
                )
                continue

            # 政策正文变化后旧向量已不再对应新内容，置空后等待重新向量化。
            if chunk.title != policy.title or chunk.content != policy.content:
                chunk.embedding = None
            chunk.title = policy.title
            chunk.content = policy.content
            chunk.chunk_metadata = {"source": "fictional_access_policy"}

        # 全部目录作为一个事务提交，不留只有部分目录的半成品。
        session.commit()
    except Exception:
        # 失败后回滚并继续抛出原始异常，便于调用方记录真实原因。
        session.rollback()
        raise
