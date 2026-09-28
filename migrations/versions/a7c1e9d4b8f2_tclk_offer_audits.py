"""tclk_offer_audits — security audit ("denetim") of every incoming offer

Revision ID: a7c1e9d4b8f2
Revises: f6e5d4c3b2a1
Create Date: 2026-09-23

One row per tclk/1 offer we audited, whether or not we acted on it. The audit
trail is the deliverable: it covers the whole offer stream, not just accepted
deals, and records the deterministic checks, the Jev security verdict, and the
final gate decision with its reason.

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "a7c1e9d4b8f2"
down_revision: str | None = "f6e5d4c3b2a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tclk_offer_audits",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("room", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("ref", sa.String(length=80), nullable=False),
        sa.Column("author", sa.String(length=80), nullable=False),
        sa.Column("rail", sa.String(length=40), nullable=False),
        sa.Column("asset", sa.String(length=20), nullable=False),
        sa.Column("amount", sa.String(length=40), nullable=False),
        sa.Column("spec", sa.String(length=200), nullable=False),
        sa.Column("spec_missing", sa.Boolean(), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("risk", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=220), nullable=False),
        sa.Column("checks", sa.JSON(), nullable=False),
        sa.Column("jev_ran", sa.Boolean(), nullable=False),
        sa.Column("jev_tier", sa.String(length=16), nullable=False),
        sa.Column("jev_confidence", sa.Float(), nullable=True),
        sa.Column("jev_reason", sa.String(length=220), nullable=False),
        sa.Column("jev_model", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("room", "seq", name="uq_tclk_audit_room_seq"),
    )
    op.create_index("ix_tclk_audit_created", "tclk_offer_audits", ["created_at"], unique=False)
    op.create_index("ix_tclk_audit_decision", "tclk_offer_audits", ["decision"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_tclk_audit_decision", table_name="tclk_offer_audits")
    op.drop_index("ix_tclk_audit_created", table_name="tclk_offer_audits")
    op.drop_table("tclk_offer_audits")
