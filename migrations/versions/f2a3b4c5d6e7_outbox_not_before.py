"""outbox_not_before

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-08-31

Add delayed OutboxMessage publishing with not_before.
"""
from typing import Union
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "f2a3b4c5d6e7"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("outbox_messages", sa.Column("not_before", sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f("ix_outbox_messages_not_before"), "outbox_messages", ["not_before"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_outbox_messages_not_before"), table_name="outbox_messages")
    op.drop_column("outbox_messages", "not_before")
