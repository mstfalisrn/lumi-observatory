"""proactive_observation_and_digest_workflows

Revision ID: d5e6f7a8b9c0
Revises: a9c8d7e6f5b4
Create Date: 2026-09-07
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d5e6f7a8b9c0"
down_revision: str | None = "a9c8d7e6f5b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Durable, content-addressed source state. Existing sources stay usable;
    # monitoring remains globally disabled until explicitly configured.
    op.add_column("sources", sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "sources",
        sa.Column("last_content_hash", sa.String(length=64), nullable=False, server_default=""),
    )
    op.create_index("ix_sources_enabled_last_observed", "sources", ["is_enabled", "last_observed_at"], unique=False)

    # Report-only schedule metadata. This table contains no destination URL,
    # token, or delivery credential: producing a digest is a local DB write.
    op.create_table(
        "digest_schedules",
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("interval_minutes", sa.Integer(), nullable=False, server_default="1440"),
        sa.Column(
            "source_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("minimum_tier", sa.String(length=16), nullable=False, server_default="WATCH"),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("delivery_mode", sa.String(length=32), nullable=False, server_default="report_only"),
        sa.Column("last_generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_digest_schedule_name"),
    )
    op.create_index(
        "ix_digest_schedules_enabled_last_generated",
        "digest_schedules",
        ["is_enabled", "last_generated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_digest_schedules_enabled_last_generated", table_name="digest_schedules")
    op.drop_table("digest_schedules")
    op.drop_index("ix_sources_enabled_last_observed", table_name="sources")
    op.drop_column("sources", "last_content_hash")
    op.drop_column("sources", "last_observed_at")
