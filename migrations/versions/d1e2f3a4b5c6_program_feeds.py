"""program feed layer — tclk_verdicts + program_scores + room_archives

Revision ID: d1e2f3a4b5c6
Revises: c9f1a2b3d4e5
Create Date: 2026-09-28

Why: until now the repo stored neither the referee's decision (the room tape)
nor the official program publications (passport/points/kibble/board), so
"did the referee pass our work, what is our passport score, what did the room
answer" could only be answered by looking at the live room and the ring that
scrolls away.

Three tables, all idempotent on rewrite:
  * tclk_verdicts  — decision lines from the room tape, unique per room+seq
  * program_scores — snapshots of the official publications, unique per
                     (source, subject_did, captured_at); captured_at is
                     rounded to the minute
  * room_archives  — ledger of the local JSONL archive files, unique per room

The `line` column is clipped to 2000 characters and masked before it is
written; the reveal preimage (the escrow claim secret) is never stored — same
rule as tclk_frames.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "d1e2f3a4b5c6"
down_revision: str | None = "c9f1a2b3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tclk_verdicts",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("room", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("contract", sa.String(length=80), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("line", sa.Text(), nullable=False),
        sa.Column("payer_did", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("room", "seq", name="uq_tclk_verdict_room_seq"),
    )
    op.create_index("ix_tclk_verdict_contract", "tclk_verdicts", ["contract"], unique=False)
    op.create_index("ix_tclk_verdict_verdict", "tclk_verdicts", ["verdict"], unique=False)
    op.create_index("ix_tclk_verdict_created", "tclk_verdicts", ["created_at"], unique=False)

    op.create_table(
        "program_scores",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("subject_did", sa.String(length=80), nullable=False),
        sa.Column("score", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("payload", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "subject_did", "captured_at", name="uq_program_score_snapshot"),
    )
    op.create_index(
        "ix_program_score_source_captured", "program_scores", ["source", "captured_at"], unique=False
    )
    op.create_index("ix_program_score_subject", "program_scores", ["subject_did"], unique=False)

    op.create_table(
        "room_archives",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("room", sa.String(length=64), nullable=False),
        sa.Column("first_seq", sa.BigInteger(), nullable=True),
        sa.Column("last_seq", sa.BigInteger(), nullable=True),
        sa.Column("records", sa.Integer(), nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("room", name="uq_room_archive_room"),
    )
    op.create_index("ix_room_archive_archived", "room_archives", ["archived_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_room_archive_archived", table_name="room_archives")
    op.drop_table("room_archives")
    op.drop_index("ix_program_score_subject", table_name="program_scores")
    op.drop_index("ix_program_score_source_captured", table_name="program_scores")
    op.drop_table("program_scores")
    op.drop_index("ix_tclk_verdict_created", table_name="tclk_verdicts")
    op.drop_index("ix_tclk_verdict_verdict", table_name="tclk_verdicts")
    op.drop_index("ix_tclk_verdict_contract", table_name="tclk_verdicts")
    op.drop_table("tclk_verdicts")
