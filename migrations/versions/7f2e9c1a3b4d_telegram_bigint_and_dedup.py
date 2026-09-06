"""telegram_bigint_and_dedup

Revision ID: 7f2e9c1a3b4d
Revises: b2c3d4e5f6a7b
Create Date: 2026-08-25

Add Telegram BIGINT identifiers and deduplication support.
"""
from typing import Union
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = '7f2e9c1a3b4d'
down_revision: str | None = 'b2c3d4e5f6a7b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # TelegramIdentity BIGINT (Integer -> BigInteger)
    op.alter_column('telegram_identities', 'telegram_user_id',
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=False,
        postgresql_using='telegram_user_id::bigint')
    # Telegram dedup table (update_id BIGINT primary key)
    op.create_table('telegram_updates',
        sa.Column('update_id', sa.BigInteger(), nullable=False),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('payload_hash', sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint('update_id'),
        sa.UniqueConstraint('update_id', name='uq_tg_update_id')
    )


def downgrade() -> None:
    op.drop_table('telegram_updates')
    op.alter_column('telegram_identities', 'telegram_user_id',
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=False)
