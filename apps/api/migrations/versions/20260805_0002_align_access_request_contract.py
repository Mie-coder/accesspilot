"""align access request with the five-field draft

Revision ID: 20260805_0002
Revises: 20260715_0001
Create Date: 2026-08-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260805_0002"
down_revision: str | Sequence[str] | None = "20260715_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """将正式申请收敛到当前 RequestDraft 业务字段。"""

    op.add_column(
        "access_requests",
        sa.Column("justification", sa.Text(), nullable=True),
    )
    # 旧演示记录的 business_reason 与新 justification 语义一致，先迁移再收紧。
    op.execute(
        "UPDATE access_requests "
        "SET justification = business_reason "
        "WHERE justification IS NULL"
    )
    op.alter_column("access_requests", "justification", nullable=False)
    op.drop_column("access_requests", "project_code")
    op.drop_column("access_requests", "data_scope")
    op.drop_column("access_requests", "business_reason")
    op.drop_column("access_requests", "start_date")


def downgrade() -> None:
    """恢复旧演示字段，并为已有记录提供可识别的迁移占位值。"""

    op.add_column(
        "access_requests",
        sa.Column("project_code", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "access_requests",
        sa.Column("data_scope", sa.Text(), nullable=True),
    )
    op.add_column(
        "access_requests",
        sa.Column("business_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "access_requests",
        sa.Column("start_date", sa.Date(), nullable=True),
    )
    op.execute(
        "UPDATE access_requests SET "
        "project_code = 'LEGACY-MIGRATION', "
        "data_scope = '旧版演示记录', "
        "business_reason = justification, "
        "start_date = confirmed_at::date"
    )
    op.alter_column("access_requests", "project_code", nullable=False)
    op.alter_column("access_requests", "data_scope", nullable=False)
    op.alter_column("access_requests", "business_reason", nullable=False)
    op.alter_column("access_requests", "start_date", nullable=False)
    op.drop_column("access_requests", "justification")
