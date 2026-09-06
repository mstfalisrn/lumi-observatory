"""p0_retry_and_action_execution

Revision ID: e1f2a3b4c5d6
Revises: d4e5f6a7b8c9
Create Date: 2026-08-27

Add retry and action-execution persistence with source-run and sequence constraints.
"""
from typing import Union
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "e1f2a3b4c5d6"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("source_run_id", sa.Uuid(), nullable=True))
    op.add_column("runs", sa.Column("retry_idempotency_key", sa.String(length=128), nullable=True))
    op.create_foreign_key("fk_runs_source_run_id", "runs", "runs", ["source_run_id"], ["id"])
    op.create_index("ix_runs_source_run_id", "runs", ["source_run_id"], unique=False)
    op.create_index("ix_runs_source_retry", "runs", ["source_run_id", "retry_idempotency_key"], unique=False)
    op.create_unique_constraint("uq_runs_source_retry_key", "runs", ["source_run_id", "retry_idempotency_key"])

    op.create_table(
        "action_executions",
        sa.Column("approval_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("action_id", sa.String(length=128), nullable=False),
        sa.Column("tool", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["approval_id"], ["approvals.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("approval_id", name="uq_action_exec_approval"),
    )
    op.create_index("ix_action_exec_run", "action_executions", ["run_id"], unique=False)
    op.create_index("ix_action_exec_approval", "action_executions", ["approval_id"], unique=False)
    op.create_index(op.f("ix_action_executions_id"), "action_executions", ["id"], unique=False)
    # global_seq uniqueness for SSE cursor guarantee across runs (unique creates implicit index)
    op.create_unique_constraint("uq_run_events_global_seq", "run_events", ["global_seq"])


def downgrade() -> None:
    op.drop_constraint("uq_run_events_global_seq", "run_events", type_="unique")
    op.drop_index(op.f("ix_action_executions_id"), table_name="action_executions")
    op.drop_index("ix_action_exec_approval", table_name="action_executions")
    op.drop_index("ix_action_exec_run", table_name="action_executions")
    op.drop_table("action_executions")
    op.drop_constraint("uq_runs_source_retry_key", "runs", type_="unique")
    op.drop_index("ix_runs_source_retry", table_name="runs")
    op.drop_index("ix_runs_source_run_id", table_name="runs")
    op.drop_constraint("fk_runs_source_run_id", "runs", type_="foreignkey")
    op.drop_column("runs", "retry_idempotency_key")
    op.drop_column("runs", "source_run_id")
