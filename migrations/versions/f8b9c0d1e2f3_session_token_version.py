"""session token version — logout / password change revoke issued JWTs

Revision ID: f8b9c0d1e2f3
Revises: e7a1b2c3d4e5
Create Date: 2026-10-05

Why: session JWTs stayed valid for their full TTL after logout, a password
change, an env-driven password reset or account deactivation. Every token now
carries the user's current `token_version`; bumping the column invalidates
every outstanding token on the next request.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f8b9c0d1e2f3"
down_revision: str | None = "e7a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("users", "token_version")
