"""add explicit demo session state

Revision ID: 20260810_0006
Revises: 20260806_0005
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260810_0006"
down_revision: str | Sequence[str] | None = "20260806_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为 Workspace 增加显式 Demo 身份覆盖和会话开关。"""

    op.add_column(
        "workspaces",
        sa.Column("demo_actor_id", sa.String(length=30), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "demo_session_active",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


    op.add_column(
        "workspaces",
        sa.Column(
            "model_retry_consumed",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )

    op.create_check_constraint(
        "model_retry_consumed_non_negative",
        "workspaces",
        "model_retry_consumed >= 0",
    )
    op.create_check_constraint(
        "model_retry_within_calls",
        "workspaces",
        "model_retry_consumed <= model_calls_used",
    )
    op.create_check_constraint(
        "demo_session_actor_consistent",
        "workspaces",
        (
            "(demo_session_active AND demo_actor_id IS NOT NULL) OR "
            "(NOT demo_session_active AND demo_actor_id IS NULL)"
        ),
    )
def downgrade() -> None:
    """删除 Demo 控制状态，不触碰草稿、申请和审计事实。"""

    op.drop_constraint("model_retry_consumed_non_negative", "workspaces", type_="check")
    op.drop_constraint("demo_session_actor_consistent", "workspaces", type_="check")
    op.drop_constraint("model_retry_within_calls", "workspaces", type_="check")
    op.drop_column("workspaces", "demo_session_active")
    op.drop_column("workspaces", "demo_actor_id")
    op.drop_column("workspaces", "model_retry_consumed")
