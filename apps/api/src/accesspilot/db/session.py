"""数据库 Engine 与 Session 工厂。"""

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def build_engine(database_url: str) -> Engine:
    """根据数据库地址创建同步 Engine 和连接池。"""

    return create_engine(database_url, pool_pre_ping=True)


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    """创建能够按需生产独立 Session 的工厂。"""

    return sessionmaker(bind=engine, expire_on_commit=False)
