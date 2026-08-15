"""数据库 Engine 和 Session 工厂的最小行为测试。"""

from importlib import import_module

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def test_builds_sync_postgresql_session_factory() -> None:
    """工厂应创建同步 PostgreSQL Engine 和标准 Session。"""

    db_session = import_module("accesspilot.db.session")

    engine = db_session.build_engine(
        "postgresql+psycopg://accesspilot@127.0.0.1:55432/accesspilot"
    )
    factory = db_session.build_session_factory(engine)

    assert isinstance(engine, Engine)
    assert isinstance(factory, sessionmaker)
    # sessionmaker 会在内部创建 Session 子类，因此验证实际产生的对象。
    with factory() as session:
        assert isinstance(session, Session)
        assert session.bind is engine
