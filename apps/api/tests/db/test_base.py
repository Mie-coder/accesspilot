"""SQLAlchemy 公共 Base 的约束命名测试。"""

from importlib import import_module


def test_base_uses_stable_foreign_key_names() -> None:
    """Alembic 应能为外键生成稳定、可预测的名称。"""

    db_base = import_module("accesspilot.db.base")

    assert db_base.Base.metadata.naming_convention["fk"] == (
        "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
    )
