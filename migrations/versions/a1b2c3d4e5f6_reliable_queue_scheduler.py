"""reliable_queue_scheduler

Revision ID: a1b2c3d4e5f6
Revises: 5014bc0ab4ea
Create Date: 2026-08-25
"""
from typing import Union
from collections.abc import Sequence
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'a1b2c3d4e5f6'
down_revision: str | None = '5014bc0ab4ea'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- outbox_messages ---
    op.create_table('outbox_messages',
        sa.Column('topic', sa.String(length=120), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('idempotency_key', sa.String(length=128), nullable=False),
        sa.Column('processed', sa.Boolean(), nullable=False),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('ts', sa.DateTime(timezone=True), nullable=False),
        sa.Column('stream_id', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('idempotency_key', name='uq_outbox_idempotency'),
    )
    op.create_index('ix_outbox_processed', 'outbox_messages', ['processed', 'created_at'], unique=False)
    op.create_index('ix_outbox_topic', 'outbox_messages', ['topic'], unique=False)
    op.create_index(op.f('ix_outbox_messages_id'), 'outbox_messages', ['id'], unique=False)

    # --- runs: lease/heartbeat columns ---
    op.add_column('runs', sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('runs', sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('runs', sa.Column('worker_id', sa.String(length=64), nullable=True))

    # --- tasks: idempotency unique partial index ---
    # existing data may have duplicates: deduplicate before creating constraint
    op.execute("""
        DELETE FROM tasks a USING tasks b
        WHERE a.id > b.id AND a.idempotency_key IS NOT NULL AND a.idempotency_key = b.idempotency_key
    """)
    # remove nulls are fine; create partial unique index
    op.create_index('ix_tasks_idempotency_key', 'tasks', ['idempotency_key'], unique=True,
                    postgresql_where=sa.text("idempotency_key IS NOT NULL"))

    # --- run_events: unique (run_id, seq) ---
    # dedup: keep lowest id per (run_id,seq)
    op.execute("""
        DELETE FROM run_events a USING run_events b
        WHERE a.id > b.id AND a.run_id = b.run_id AND a.seq = b.seq
    """)
    op.create_unique_constraint('uq_run_events_run_seq', 'run_events', ['run_id', 'seq'])

    # --- publication_attempts: unique idempotency_key where not empty ---
    op.execute("""
        DELETE FROM publication_attempts a USING publication_attempts b
        WHERE a.id > b.id AND a.idempotency_key <> '' AND a.idempotency_key = b.idempotency_key
    """)
    op.create_index('ix_pub_idempotency_key', 'publication_attempts', ['idempotency_key'], unique=True,
                    postgresql_where=sa.text("idempotency_key <> ''"))

    # --- append-only triggers: prevent UPDATE/DELETE on run_events and audit_events ---
    op.execute("""
        CREATE OR REPLACE FUNCTION lumi_prevent_update_delete() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'append-only table: % UPDATE/DELETE not allowed', TG_TABLE_NAME;
            RETURN NULL;
        END; $$ LANGUAGE plpgsql;
    """)
    op.execute("DROP TRIGGER IF EXISTS trg_run_events_append_only ON run_events;")
    op.execute("""
        CREATE TRIGGER trg_run_events_append_only
        BEFORE UPDATE OR DELETE ON run_events
        FOR EACH ROW EXECUTE FUNCTION lumi_prevent_update_delete();
    """)
    op.execute("DROP TRIGGER IF EXISTS trg_audit_events_append_only ON audit_events;")
    op.execute("""
        CREATE TRIGGER trg_audit_events_append_only
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION lumi_prevent_update_delete();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_audit_events_append_only ON audit_events;")
    op.execute("DROP TRIGGER IF EXISTS trg_run_events_append_only ON run_events;")
    op.execute("DROP FUNCTION IF EXISTS lumi_prevent_update_delete();")
    op.drop_index('ix_pub_idempotency_key', table_name='publication_attempts')
    op.drop_constraint('uq_run_events_run_seq', 'run_events', type_='unique')
    op.drop_index('ix_tasks_idempotency_key', table_name='tasks')
    op.drop_column('runs', 'worker_id')
    op.drop_column('runs', 'lease_expires_at')
    op.drop_column('runs', 'heartbeat_at')
    op.drop_index('ix_outbox_topic', table_name='outbox_messages')
    op.drop_index('ix_outbox_processed', table_name='outbox_messages')
    op.drop_index(op.f('ix_outbox_messages_id'), table_name='outbox_messages')
    op.drop_table('outbox_messages')
