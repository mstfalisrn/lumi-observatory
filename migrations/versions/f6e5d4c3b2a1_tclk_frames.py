"""tclk_frames — tclk/1 escrowed task-marketplace observations

Revision ID: e1f2a3b4c5d6
Revises: d5e6f7a8b9c0
Create Date: 2026-09-08

Read-only surveillance table: one row per tclk/1 frame observed in a
monitored marketplace room. Masked by design — reveal/preimage values are
never stored (they are the escrow-claim secret).
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "f6e5d4c3b2a1"
down_revision: str | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tclk_frames",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("room", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("author", sa.String(length=80), nullable=False),
        sa.Column("signed", sa.Boolean(), nullable=False),
        sa.Column("contract", sa.String(length=80), nullable=False),
        sa.Column("ref", sa.String(length=80), nullable=False),
        sa.Column("rail", sa.String(length=40), nullable=False),
        sa.Column("asset", sa.String(length=20), nullable=False),
        sa.Column("amount", sa.String(length=40), nullable=False),
        sa.Column("summary", sa.String(length=300), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("room", "seq", name="uq_tclk_frame_room_seq"),
    )
    op.create_index("ix_tclk_kind_created", "tclk_frames", ["kind", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_tclk_kind_created", table_name="tclk_frames")
    op.drop_table("tclk_frames")