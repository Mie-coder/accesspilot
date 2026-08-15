"""add provisioning attempts

Revision ID: 20260805_0003
Revises: 20260805_0002
Create Date: 2026-08-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260805_0003"
down_revision: str | Sequence[str] | None = "20260805_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建可恢复且每份申请唯一的开通操作记录。"""

    op.create_table(
        "provisioning_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=100), nullable=False),
        sa.Column("provisioning_status", sa.String(length=30), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "attempt_count > 0",
            name=op.f("ck_provisioning_attempts_attempt_count_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "request_id"],
            ["access_requests.workspace_id", "access_requests.id"],
            name=op.f("fk_provisioning_attempts_workspace_id_access_requests"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_provisioning_attempts_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provisioning_attempts")),
        sa.UniqueConstraint(
            "idempotency_key",
            name=op.f("uq_provisioning_attempts_idempotency_key"),
        ),
        sa.UniqueConstraint(
            "request_id",
            name=op.f("uq_provisioning_attempts_request_id"),
        ),
    )
    op.create_index(
        op.f("ix_provisioning_attempts_workspace_id"),
        "provisioning_attempts",
        ["workspace_id"],
        unique=False,
    )


def downgrade() -> None:
    """删除开通尝试表，不触碰已存在的授权事实。"""

    op.drop_index(
        op.f("ix_provisioning_attempts_workspace_id"),
        table_name="provisioning_attempts",
    )
    op.drop_table("provisioning_attempts")
