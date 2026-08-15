"""add draft revision and ConversationCursor state

Revision ID: 20260811_0007
Revises: 20260810_0006
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260811_0007"
down_revision: str | Sequence[str] | None = "20260810_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为 Workspace 增加 T18 草稿 revision 与服务端游标合同。"""

    op.add_column(
        "workspaces",
        sa.Column(
            "draft_revision",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column("cursor_actor_id", sa.String(length=30), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("cursor_auth_session_id", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("cursor_expected_field", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("cursor_last_question_kind", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("cursor_issued_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("cursor_consumed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """移除 T18 游标字段，不删除 Workspace 草稿或事件事实。"""

    op.drop_column("workspaces", "cursor_consumed_at")
    op.drop_column("workspaces", "cursor_issued_at")
    op.drop_column("workspaces", "cursor_last_question_kind")
    op.drop_column("workspaces", "cursor_expected_field")
    op.drop_column("workspaces", "cursor_auth_session_id")
    op.drop_column("workspaces", "cursor_actor_id")
    op.drop_column("workspaces", "draft_revision")
