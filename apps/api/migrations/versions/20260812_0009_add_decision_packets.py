"""add immutable Decision Packets

Revision ID: 20260812_0009
Revises: 20260811_0008
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260812_0009"
down_revision: str | Sequence[str] | None = "20260811_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add one source-labelled frozen Packet per existing formal request."""

    op.create_table(
        "decision_packets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("packet_version", sa.String(length=30), nullable=False),
        sa.Column("generation_mode", sa.String(length=30), nullable=False),
        sa.Column("catalog_version", sa.String(length=50), nullable=False),
        sa.Column(
            "frozen_content",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "generation_mode IN ('provider', 'deterministic', 'unavailable')",
            name=op.f("ck_decision_packets_generation_mode_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["access_requests.id"],
            name=op.f("fk_decision_packets_request_id_access_requests"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_decision_packets")),
    )


def downgrade() -> None:
    """Remove Packet snapshots without touching existing v1.1 facts."""

    op.drop_table("decision_packets")
