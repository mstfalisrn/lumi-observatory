"""technocore_nonce

Revision ID: d4e5f6a7b8c9
Revises: c1d2e3f4a5b6
Create Date: 2026-08-27

Create the missing technocore_nonces schema to remove model/migration drift.
The Alembic revision creates the table, unique constraint, and index.
No schema creation is hidden behind metadata.create_all.
"""
from typing import Union
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "technocore_nonces",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("room", sa.String(length=255), nullable=False),
        sa.Column("did", sa.String(length=80), nullable=False),
        sa.Column("last_nonce", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("room", "did", name="uq_technocore_nonce_room_did"),
    )
    op.create_index("ix_technocore_nonce_room_did", "technocore_nonces", ["room", "did"], unique=False)
    op.create_index(op.f("ix_technocore_nonces_id"), "technocore_nonces", ["id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_technocore_nonces_id"), table_name="technocore_nonces")
    op.drop_index("ix_technocore_nonce_room_did", table_name="technocore_nonces")
    op.drop_table("technocore_nonces")
