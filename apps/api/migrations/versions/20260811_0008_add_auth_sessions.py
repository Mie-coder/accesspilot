"""add hashed AuthSession records and invalidate legacy null-session cursors

Revision ID: 20260811_0008
Revises: 20260811_0007
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260811_0008"
down_revision: str | Sequence[str] | None = "20260811_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create AuthSession storage and safely clear old active cursors."""

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("employee_id", sa.String(length=30), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("csrf_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["employees.employee_id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_auth_sessions_token_hash", "auth_sessions", ["token_hash"], unique=True
    )
    op.create_index(
        "ix_auth_sessions_employee_id", "auth_sessions", ["employee_id"], unique=False
    )
    op.create_index(
        "ix_auth_sessions_workspace_id", "auth_sessions", ["workspace_id"], unique=True
    )
    # T18 cursors were allowed to have a null session id.  Do not fabricate a
    # backfill; invalidate only active legacy cursors before T19 enforces the
    # AuthSession binding in the request path.
    op.execute(
        sa.text(
            "UPDATE workspaces SET cursor_actor_id = NULL, "
            "cursor_auth_session_id = NULL, cursor_expected_field = NULL, "
            "cursor_last_question_kind = NULL, cursor_issued_at = NULL, "
            "cursor_consumed_at = NULL WHERE cursor_expected_field IS NOT NULL "
            "AND cursor_auth_session_id IS NULL"
        )
    )
    op.create_check_constraint(
        op.f("ck_workspaces_cursor_auth_session_required"),
        "workspaces",
        "cursor_expected_field IS NULL OR cursor_auth_session_id IS NOT NULL",
    )


def downgrade() -> None:
    """Drop AuthSession storage; do not restore invalidated cursors."""

    # Older local databases may have reached 0008 before this constraint was
    # added.  PostgreSQL's IF EXISTS keeps downgrade idempotent across that
    # schema evolution while also handling the convention-expanded name.
    op.execute(
        sa.text(
            "ALTER TABLE workspaces DROP CONSTRAINT IF EXISTS "
            "ck_workspaces_cursor_auth_session_required"
        )
    )
    # Keep a compatibility drop for the unexpanded name emitted by an early
    # development revision; never fail a downgrade because it is absent.
    op.execute(
        sa.text(
            "ALTER TABLE workspaces DROP CONSTRAINT IF EXISTS "
            "cursor_auth_session_required"
        )
    )
    op.drop_index("ix_auth_sessions_workspace_id", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_employee_id", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_token_hash", table_name="auth_sessions")
    op.drop_table("auth_sessions")
