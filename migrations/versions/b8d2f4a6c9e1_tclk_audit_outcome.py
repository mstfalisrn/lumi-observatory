"""tclk offer audit: record the delivery outcome

Revision ID: b8d2f4a6c9e1
Revises: a7c1e9d4b8f2
Create Date: 2026-09-24

The audit row already captured the *decision* (did we accept the offer). It did
not capture the *outcome* (did we actually do the work and ship it), which is
the only thing that can lead to a lock and a payment. These columns close the
loop so the funnel — offer → accept → delivered → locked → paid — is readable
from the database alone.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b8d2f4a6c9e1"
down_revision = "a7c1e9d4b8f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tclk_offer_audits",
        sa.Column("contract", sa.String(length=80), nullable=False, server_default=""),
    )
    op.add_column(
        "tclk_offer_audits",
        sa.Column("outcome", sa.String(length=16), nullable=False, server_default=""),
    )
    op.add_column(
        "tclk_offer_audits",
        sa.Column("answer", sa.String(length=300), nullable=False, server_default=""),
    )
    op.add_column(
        "tclk_offer_audits",
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_tclk_audit_outcome", "tclk_offer_audits", ["outcome"])


def downgrade() -> None:
    op.drop_index("ix_tclk_audit_outcome", table_name="tclk_offer_audits")
    op.drop_column("tclk_offer_audits", "delivered_at")
    op.drop_column("tclk_offer_audits", "answer")
    op.drop_column("tclk_offer_audits", "outcome")
    op.drop_column("tclk_offer_audits", "contract")
