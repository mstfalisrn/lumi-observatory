"""agent_evaluations.evaluated_at + tclk frames/audits contract indeksleri

Revision ID: c9f1a2b3d4e5
Revises: b8d2f4a6c9e1

Neden: canlı günlük sayfası (apps/logs) "son N karar" sorgularını
evaluated_at/contract üzerinden yapıyor. agent_evaluations 2.6M satırda
sıralama indeksi olmadan tam tarama yapıyordu (sayfa açılışı ~5 sn).
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
