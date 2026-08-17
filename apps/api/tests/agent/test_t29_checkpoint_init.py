"""T29 explicit initialization command contract."""

from pathlib import Path

import pytest

from accesspilot.checkpoint_init import initialize_checkpointing
from accesspilot.config import Settings


def test_explicit_initializer_orders_alembic_setup_then_readiness() -> None:
    calls: list[str] = []

    class Runtime:
        def start(self) -> None:
            calls.append("runtime.start")

        def check_readiness(self) -> None:
            calls.append("readiness")

        def close(self) -> None:
            calls.append("runtime.close")

    settings = Settings(
        migration_database_url="postgresql://migration/app",
        checkpoint_migration_database_url="postgresql://migration/app",
        checkpoint_database_url="postgresql://runtime/app",
        _env_file=None,
    )
    initialize_checkpointing(
        settings,
        alembic_upgrade=lambda active: calls.append("alembic"),
        checkpoint_setup=lambda active: calls.append("official.setup"),
        runtime_factory=lambda active: Runtime(),
    )

    assert calls == [
        "alembic",
        "official.setup",
        "runtime.start",
        "readiness",
        "runtime.close",
    ]


def test_initializer_requires_distinct_explicit_migration_and_runtime_urls() -> None:
    with pytest.raises(ValueError, match="migration_database_url"):
        initialize_checkpointing(Settings(_env_file=None))


def test_ordinary_start_script_contains_no_migration_or_checkpoint_setup() -> None:
    root = Path(__file__).resolve().parents[4]
    source = (root / "scripts" / "start-api.sh").read_text()

    assert "alembic" not in source
    assert ".setup(" not in source
    assert "checkpoint_init" not in source

