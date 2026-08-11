"""T19 schema/migration contract checks."""

from pathlib import Path

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.db.models import WorkspaceRecord


def test_0008_revision_is_head_and_has_no_fake_cursor_backfill() -> None:
    migration_path = (
        Path(__file__).parents[2]
        / "migrations"
        / "versions"
        / "20260811_0008_add_auth_sessions.py"
    )
    source = migration_path.read_text(encoding="utf-8")
    assert 'revision: str = "20260811_0008"' in source
    assert 'down_revision: str | Sequence[str] | None = "20260811_0007"' in source
    assert "cursor_auth_session_id IS NULL" in source
    assert "cursor_auth_session_id = '" not in source
    assert "cursor_expected_field = NULL" in source
    assert "cursor_auth_session_required" in source
    assert "DROP CONSTRAINT IF EXISTS" in source
    assert "ck_workspaces_cursor_auth_session_required" in source


def test_auth_sessions_table_has_hash_only_schema(
    database_session_factory: sessionmaker[Session],
) -> None:
    with database_session_factory() as session:
        bind = session.get_bind()
        inspector = inspect(bind)
        columns = {
            column["name"]: column for column in inspector.get_columns("auth_sessions")
        }
        assert {
            "id",
            "token_hash",
            "employee_id",
            "workspace_id",
            "csrf_hash",
            "expires_at",
            "revoked_at",
            "created_at",
        } <= set(columns)
        assert columns["token_hash"]["nullable"] is False
        assert columns["csrf_hash"]["nullable"] is False
        indexes = inspector.get_indexes("auth_sessions")
        token_indexes = [
            index for index in indexes if index["column_names"] == ["token_hash"]
        ]
        assert token_indexes and any(index["unique"] for index in token_indexes)
        workspace_indexes = [
            index for index in inspector.get_indexes("auth_sessions")
            if index["column_names"] == ["workspace_id"]
        ]
        assert workspace_indexes and any(index["unique"] for index in workspace_indexes)


def test_no_legacy_active_cursor_without_session_survives_upgrade(
    database_session_factory: sessionmaker[Session],
) -> None:
    with database_session_factory() as session:
        legacy_active = session.scalars(
            select(WorkspaceRecord).where(
                WorkspaceRecord.cursor_expected_field.is_not(None),
                WorkspaceRecord.cursor_auth_session_id.is_(None),
                WorkspaceRecord.cursor_consumed_at.is_(None),
            )
        ).all()
        assert legacy_active == []
