"""app_state

Revision ID: e7a1b2c3d4e5
Revises: d1e2f3a4b5c6
Create Date: 2026-10-05
"""
from typing import Union
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'e7a1b2c3d4e5'
down_revision: str | None = 'd1e2f3a4b5c6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'app_state',
        sa.Column('key', sa.String(length=120), primary_key=True, nullable=False),
        sa.Column('value', sa.Text(), nullable=False, server_default=''),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
    )


def downgrade() -> None:
    op.drop_table('app_state')
