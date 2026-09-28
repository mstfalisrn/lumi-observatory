"""agent_evaluations.evaluated_at + tclk frames/audits contract indexes

Revision ID: c9f1a2b3d4e5
Revises: b8d2f4a6c9e1

Why: the live log page (apps/logs) runs its "last N decisions" queries through
evaluated_at/contract. With 2.6M rows agent_evaluations was doing a full scan
without a sort index (page open took ~5s).
"""

from alembic import op

revision = "c9f1a2b3d4e5"
down_revision = "b8d2f4a6c9e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_eval_evaluated_at "
        "ON agent_evaluations (evaluated_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tclk_frames_contract "
        "ON tclk_frames (contract)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tclk_audit_contract "
        "ON tclk_offer_audits (contract)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_tclk_audit_contract")
    op.execute("DROP INDEX IF EXISTS ix_tclk_frames_contract")
    op.execute("DROP INDEX IF EXISTS ix_agent_eval_evaluated_at")
