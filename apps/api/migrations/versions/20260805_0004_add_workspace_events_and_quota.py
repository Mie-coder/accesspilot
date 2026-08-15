"""add workspace events and model quota

Revision ID: 20260805_0004
Revises: 20260805_0003
Create Date: 2026-08-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260805_0004"
down_revision: str | Sequence[str] | None = "20260805_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加安全事件回放表与 Workspace 模型调用配额。"""

    op.add_column(
        "workspaces",
        sa.Column(
            "model_call_limit",
            sa.Integer(),
            server_default="20",
            nullable=False,
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "model_calls_used",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_workspaces_model_call_limit_non_negative"),
        "workspaces",
        "model_call_limit >= 0",
    )
    op.create_check_constraint(
        op.f("ck_workspaces_model_calls_within_limit"),
        "workspaces",
        "model_calls_used >= 0 AND model_calls_used <= model_call_limit",
    )
    op.create_table(
        "workspace_events",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=False),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=60), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_workspace_events_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspace_events")),
    )
    op.create_index(
        op.f("ix_workspace_events_created_at"),
        "workspace_events",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_workspace_events_event_type"),
        "workspace_events",
        ["event_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_workspace_events_workspace_id"),
        "workspace_events",
        ["workspace_id"],
        unique=False,
    )


def downgrade() -> None:
    """删除事件流和配额列，不触碰业务申请与审计事实。"""

    op.drop_index(
        op.f("ix_workspace_events_workspace_id"),
        table_name="workspace_events",
    )
    op.drop_index(
        op.f("ix_workspace_events_event_type"),
        table_name="workspace_events",
    )
    op.drop_index(
        op.f("ix_workspace_events_created_at"),
        table_name="workspace_events",
    )
    op.drop_table("workspace_events")
    op.drop_constraint(
        op.f("ck_workspaces_model_calls_within_limit"),
        "workspaces",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_workspaces_model_call_limit_non_negative"),
        "workspaces",
        type_="check",
    )
    op.drop_column("workspaces", "model_calls_used")
    op.drop_column("workspaces", "model_call_limit")
