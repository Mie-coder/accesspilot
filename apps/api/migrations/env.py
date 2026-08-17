from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from accesspilot.config import Settings
from accesspilot.db import models  # noqa: F401
from accesspilot.db.base import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# 数据库地址统一由 Settings/环境变量提供，不在 alembic.ini 中复制密码。
# ConfigParser 把 ``%`` 当作插值语法；URL 编码凭据和 search_path
# options 可以合法包含 ``%xx``，写入 Alembic Config 前必须转义。
config.set_main_option("sqlalchemy.url", Settings().database_url.replace("%", "%%"))

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 导入 models 后，所有应用表会注册到 Base.metadata，供 autogenerate 比较差异。
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        # AccessPilot Alembic owns only its default application schema.  The
        # separately named LangGraph checkpoint schema is never reflected or
        # considered by autogenerate/check/downgrade.
        include_schemas=False,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Explicitly exclude every non-default schema, including the
            # official checkpointer schema managed outside AccessPilot Alembic.
            include_schemas=False,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
