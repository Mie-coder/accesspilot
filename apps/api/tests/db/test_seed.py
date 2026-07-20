"""虚构目录种子数据集成测试。"""

from importlib import import_module

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from accesspilot.db.models import (
    EmployeeRecord,
    EntitlementRecord,
    PolicyChunkRecord,
    SystemRecord,
)


def test_seed_catalog_is_idempotent(database_session: Session) -> None:
    """重复加载目录时应更新现有数据，而不是插入副本。"""

    seed_catalog = import_module("accesspilot.db.seed").seed_catalog

    seed_catalog(database_session)
    seed_catalog(database_session)

    assert database_session.scalar(
        select(func.count()).select_from(SystemRecord)
    ) == 3
    assert database_session.scalar(
        select(func.count()).select_from(EmployeeRecord)
    ) == 5
    assert database_session.scalar(
        select(func.count()).select_from(EntitlementRecord)
    ) == 7
    assert database_session.scalar(
        select(func.count()).select_from(PolicyChunkRecord)
    ) == 8

    system = database_session.get(SystemRecord, "insighthub")
    assert system is not None
    assert system.name == "数据洞察中心"

    chunks = database_session.scalars(select(PolicyChunkRecord)).all()
    assert all(chunk.embedding is None for chunk in chunks)
