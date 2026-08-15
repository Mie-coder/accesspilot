"""bind demo identity to workspace

Revision ID: 20260806_0005
Revises: 20260805_0004
Create Date: 2026-08-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260806_0005"
down_revision: str | Sequence[str] | None = "20260805_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为旧 Workspace 补默认申请人，以后身份只由后端保存。"""

    op.add_column(
        "workspaces",
        sa.Column(
            "actor_id",
            sa.String(length=30),
            server_default="EMP-001",
            nullable=False,
        ),
    )
    op.create_index(
        op.f("ix_workspaces_actor_id"),
        "workspaces",
        ["actor_id"],
        unique=False,
    )


def downgrade() -> None:
    """删除 Workspace 演示身份字段。"""

    op.drop_index(op.f("ix_workspaces_actor_id"), table_name="workspaces")
    op.drop_column("workspaces", "actor_id")
