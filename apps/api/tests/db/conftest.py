"""真实 PostgreSQL 集成测试共用夹具。"""

import os
from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.db.session import build_engine, build_session_factory


@pytest.fixture(scope="session")
def database_session_factory() -> sessionmaker[Session]:
    """为集成测试创建独立的 Session 工厂。"""

    url = os.getenv(
        "ACCESSPILOT_TEST_DATABASE_URL",
        "postgresql+psycopg://accesspilot@127.0.0.1:55432/accesspilot_test",
    )
    return build_session_factory(build_engine(url))


@pytest.fixture
def database_session(
    database_session_factory: sessionmaker[Session],
) -> Iterator[Session]:
    """每个测试获得独立 Session，使用后自动关闭连接。"""

    with database_session_factory() as session:
        yield session
