"""auth_password_hash

Revision ID: c1d2e3f4a5b6
Revises: 7f2e9c1a3b4d
Create Date: 2026-08-25
"""
from typing import Union
from collections.abc import Sequence
from alembic import op
import sqlalchemy as sa

revision: str = 'c1d2e3f4a5b6'
down_revision: str | None = '7f2e9c1a3b4d'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('users', sa.Column('password_hash', sa.String(length=255), nullable=False, server_default=''))
    op.add_column('runs', sa.Column('control_request', sa.String(length=16), nullable=True))
    op.add_column('runs', sa.Column('retry_count', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('telegram_updates', sa.Column('status', sa.String(length=16), nullable=False, server_default='PENDING'))
    op.add_column('telegram_updates', sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('run_events', sa.Column('global_seq', sa.BigInteger(), sa.Identity(), nullable=False))
    op.create_index('ix_run_events_global_seq', 'run_events', ['global_seq'])


def downgrade() -> None:
    op.drop_column('telegram_updates', 'attempt_count')
    op.drop_column('telegram_updates', 'status')
    op.drop_column('runs', 'retry_count')
    op.drop_column('runs', 'control_request')
    op.drop_column('users', 'password_hash')
