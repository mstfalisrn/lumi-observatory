"""agent_evaluations

Revision ID: a9c8d7e6f5b4
Revises: f2a3b4c5d6e7
Create Date: 2026-09-02

M2: AgentEvaluation — agent message risk assessment (5 dimensions + tier)
"""
from typing import Union
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a9c8d7e6f5b4"
down_revision: str | None = "f2a3b4c5d6e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_evaluations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("room", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("global_seq", sa.BigInteger(), nullable=False),
        sa.Column("nick", sa.String(length=120), nullable=False),
        sa.Column("did", sa.String(length=80), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("raw_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("dimensions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model", sa.String(length=80), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("room", "seq", name="uq_agent_eval_room_seq"),
    )
    op.create_index("ix_agent_evaluations_id", "agent_evaluations", ["id"], unique=False)
    op.create_index("ix_agent_eval_room_seq", "agent_evaluations", ["room", "seq"], unique=False)
    op.create_index("ix_agent_eval_tier", "agent_evaluations", ["tier"], unique=False)
    op.create_index("ix_agent_eval_score", "agent_evaluations", ["score"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_agent_eval_score", table_name="agent_evaluations")
    op.drop_index("ix_agent_eval_tier", table_name="agent_evaluations")
    op.drop_index("ix_agent_eval_room_seq", table_name="agent_evaluations")
    op.drop_index("ix_agent_evaluations_id", table_name="agent_evaluations")
    op.drop_table("agent_evaluations")
