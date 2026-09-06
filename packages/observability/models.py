# LUMI — data model (database tables)
# Minimum tables in the specification: 23. Event/audit tables are designed append-only.
# Timestamps are stored in UTC; UI displays app timezone (APP_TIMEZONE, default UTC).

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column, relationship

# Portable JSON: SQLite test'te JSON, PostgreSQL production'da JSONB (test+prod uyumu)
JSONType = JSON().with_variant(JSONB, "postgresql")

# pgvector support — if not available, JSONB fallback (to preserve the claim)
try:
    from pgvector.sqlalchemy import Vector  # type: ignore
except ImportError:  # pragma: no cover
    Vector = None  # type: ignore


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class _UUIDMixin:
    @declared_attr
    def id(cls):
        return mapped_column(
            PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True
        )


class _TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


# ----------------------------------------------------------------------------
# Enums
# ----------------------------------------------------------------------------
class RunStatus(enum.StrEnum):
    QUEUED = "QUEUED"
    CONTEXT_BUILDING = "CONTEXT_BUILDING"
    PLANNING = "PLANNING"
    POLICY_CHECK = "POLICY_CHECK"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    PERSISTING = "PERSISTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    PAUSED = "PAUSED"


class MemoryStatus(enum.StrEnum):
    CANDIDATE = "CANDIDATE"
    APPROVED = "APPROVED"
    AUTO_APPROVED = "AUTO_APPROVED"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    DELETED = "DELETED"


class ApprovalStatus(enum.StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CONSUMED = "CONSUMED"


class ActionClass(enum.StrEnum):
    READ_ONLY = "READ_ONLY"
    SAFE_WRITE = "SAFE_WRITE"
    PUBLIC_WRITE = "PUBLIC_WRITE"
    PRIVILEGED_HOST = "PRIVILEGED_HOST"
    DESTRUCTIVE = "DESTRUCTIVE"


class SourceType(enum.StrEnum):
    TECHNOCORE_ROOM = "technocore_room"
    GITHUB_REPO = "github_repo"
    HTTP_JSON = "http_json"
    INTERNAL_HEALTH = "internal_health"


class UNTRUSTED(enum.StrEnum):
    DATA = "UNTRUSTED_DATA"


# ----------------------------------------------------------------------------
# Kimlik / hesap
# ----------------------------------------------------------------------------
class User(_UUIDMixin, _TimestampMixin, Base):
    __tablename__ = "users"
    username: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    role: Mapped[str] = mapped_column(String(40), nullable=False, default="admin")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    telegram_identities: Mapped[list[TelegramIdentity]] = relationship(back_populates="user")


class TelegramIdentity(_UUIDMixin, _TimestampMixin, Base):
    __tablename__ = "telegram_identities"
    __table_args__ = (UniqueConstraint("telegram_user_id", name="uq_tg_user"),)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    username: Mapped[str] = mapped_column(String(120), nullable=True)
    first_name: Mapped[str] = mapped_column(String(255), nullable=True)
    is_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    user: Mapped[User] = relationship(back_populates="telegram_identities")


class AgentProfile(_UUIDMixin, _TimestampMixin, Base):
    __tablename__ = "agent_profiles"
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    system_policy: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tool_allowlist: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


# ----------------------------------------------------------------------------
# Task / run
# ----------------------------------------------------------------------------
class Task(_UUIDMixin, _TimestampMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_idempotency_key", "idempotency_key", unique=True,
                           postgresql_where=text("idempotency_key IS NOT NULL")),)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    budget: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=True, unique=False)
    runs: Mapped[list[Run]] = relationship(back_populates="task")


class Run(_UUIDMixin, _TimestampMixin, Base):
    __tablename__ = "runs"
    __table_args__ = (
        Index("ix_runs_source_run_id", "source_run_id"),
        # composite idempotency: same (source_run_id, key) -> same retry; different key -> new retry
        UniqueConstraint("source_run_id", "retry_idempotency_key", name="uq_runs_source_retry_key"),
        Index("ix_runs_source_retry", "source_run_id", "retry_idempotency_key"),
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False
    )
    source_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id"), nullable=True
    )
    retry_idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=RunStatus.QUEUED.value)
    plan_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=True)  # plans FK cycle is prevented; plans.run_id holds FK to runs
    iteration: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_budget: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_budget: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    token_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_used: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str] = mapped_column(Text, nullable=True)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[str] = mapped_column(String(64), nullable=True)
    control_request: Mapped[str | None] = mapped_column(String(16), nullable=True)  # "pause" | "stop" | None
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    task: Mapped[Task] = relationship(back_populates="runs")
    source_run: Mapped[Run | None] = relationship(remote_side="Run.id", back_populates="retries")
    retries: Mapped[list[Run]] = relationship(back_populates="source_run")
    events: Mapped[list[RunEvent]] = relationship(back_populates="run")
    tool_calls: Mapped[list[ToolCall]] = relationship(back_populates="run")


class RunEvent(_UUIDMixin, Base):
    __tablename__ = "run_events"
    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_run_events_run_seq"),
                      Index("ix_run_events_run_seq", "run_id", "seq"),
                      UniqueConstraint("global_seq", name="uq_run_events_global_seq"),)
    run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # global SSE cursor — independent of run-internal seq, monotonic global event ID
    # In PG, Identity (BY DEFAULT); in SQLite, generated manually with MAX+1
    global_seq: Mapped[int] = mapped_column(BigInteger, Identity(start=1), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    run: Mapped[Run] = relationship(back_populates="events")


class Plan(_UUIDMixin, _TimestampMixin, Base):
    __tablename__ = "plans"
    run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id"), nullable=False
    )
    plan_json: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    expected_evidence: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")


class ToolCall(_UUIDMixin, Base):
    __tablename__ = "tool_calls"
    __table_args__ = (Index("ix_tool_calls_run_id", "run_id"),)
    run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id"), nullable=False
    )
    tool_name: Mapped[str] = mapped_column(String(120), nullable=False)
    input_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    input_redacted: Mapped[str] = mapped_column(Text, nullable=False, default="")
    result_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    action_class: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_decision: Mapped[str] = mapped_column(String(24), nullable=False, default="ALLOW")
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    run: Mapped[Run] = relationship(back_populates="tool_calls")


# ----------------------------------------------------------------------------
# Approval / epsilon
# ----------------------------------------------------------------------------
class Approval(_UUIDMixin, _TimestampMixin, Base):
    __tablename__ = "approvals"
    action_class: Mapped[str] = mapped_column(String(32), nullable=False)
    action_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target: Mapped[str] = mapped_column(Text, nullable=False, default="")
    impact_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=ApprovalStatus.PENDING.value)
    requester_user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    decided_by_user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id"), nullable=True
    )


# ----------------------------------------------------------------------------
# Context
# ----------------------------------------------------------------------------
class ContextSnapshot(_UUIDMixin, _TimestampMixin, Base):
    __tablename__ = "context_snapshots"
    run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id"), nullable=False
    )
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    context_segments: Mapped[list[ContextSegment]] = relationship(back_populates="snapshot")


class ContextSegment(_UUIDMixin, Base):
    __tablename__ = "context_segments"
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("context_snapshots.id"), nullable=False
    )
    segment_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    relevance_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    freshness: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    included_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    contains_untrusted_input: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    redaction_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshot: Mapped[ContextSnapshot] = relationship(back_populates="context_segments")


# ----------------------------------------------------------------------------
# Memory
# ----------------------------------------------------------------------------
class MemoryItem(_UUIDMixin, _TimestampMixin, Base):
    __tablename__ = "memory_items"
    __table_args__ = (Index("ix_memory_status", "status"),)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    ttl: Mapped[int] = mapped_column(Integer, nullable=True)  # seconds; None = immortal
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=MemoryStatus.CANDIDATE.value)
    verification_status: Mapped[str] = mapped_column(String(24), nullable=False, default="unverified")
    embedding: Mapped[list] = mapped_column(JSONType, nullable=True)  # legacy JSONB, stored together with pgvector
    # Phase 4: pgvector vector column — 1536 dimensions (OpenAI ada-002 compatible); extension already CREATE EXTENSION vector in initdb
    embedding_vector: Mapped[list] = mapped_column(
        (Vector(1536) if Vector is not None else JSONType),
        nullable=True,
    )  # type: ignore[arg-type]
    category: Mapped[str] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class MemoryRelation(_UUIDMixin, _TimestampMixin, Base):
    __tablename__ = "memory_relations"
    from_memory_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("memory_items.id"), nullable=False
    )
    to_memory_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("memory_items.id"), nullable=False
    )
    relation_type: Mapped[str] = mapped_column(String(40), nullable=False)  # contradicts | supersedes | related


# ----------------------------------------------------------------------------
# Resources / observations / evidence
# ----------------------------------------------------------------------------
class Source(_UUIDMixin, _TimestampMixin, Base):
    __tablename__ = "sources"
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    config: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_accessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    error_series: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    backoff_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class SourceObservation(_UUIDMixin, _TimestampMixin, Base):
    __tablename__ = "source_observations"
    source_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sources.id"), nullable=False
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    change_type: Mapped[str] = mapped_column(String(64), nullable=False)
    change: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")


class EvidenceItem(_UUIDMixin, _TimestampMixin, Base):
    __tablename__ = "evidence_items"
    run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id"), nullable=True
    )
    source_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    claim: Mapped[str] = mapped_column(Text, nullable=False, default="")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Report(_UUIDMixin, _TimestampMixin, Base):
    __tablename__ = "reports"
    report_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    signed_did: Mapped[str] = mapped_column(Text, nullable=True)


class PublicationAttempt(_UUIDMixin, _TimestampMixin, Base):
    __tablename__ = "publication_attempts"
    __table_args__ = (Index("ix_pub_idempotency_key", "idempotency_key", unique=True,
                           postgresql_where=text("idempotency_key <> ''")),)
    report_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("reports.id"), nullable=True
    )
    target: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    response: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, default="")


# ----------------------------------------------------------------------------
# Technocore cursor / nonce / prompt / policy / audit
# ----------------------------------------------------------------------------
class TechnocoreCursor(_UUIDMixin, _TimestampMixin, Base):
    __tablename__ = "technocore_cursors"
    room: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    last_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class TechnocoreNonce(_UUIDMixin, _TimestampMixin, Base):
    """Per-room DID nonce monotonic — separate table for atomic increment."""

    __tablename__ = "technocore_nonces"
    __table_args__ = (
        UniqueConstraint("room", "did", name="uq_technocore_nonce_room_did"),
        Index("ix_technocore_nonce_room_did", "room", "did"),
    )
    room: Mapped[str] = mapped_column(String(255), nullable=False)
    did: Mapped[str] = mapped_column(String(80), nullable=False)
    last_nonce: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class PromptVersion(_UUIDMixin, _TimestampMixin, Base):
    __tablename__ = "prompt_versions"
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (UniqueConstraint("name", "version", name="uq_prompt_name_ver"),)


class PolicyVersion(_UUIDMixin, _TimestampMixin, Base):
    __tablename__ = "policy_versions"
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (UniqueConstraint("name", "version", name="uq_policy_name_ver"),)


class AuditEvent(_UUIDMixin, Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_ts", "ts"),)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    actor: Mapped[str] = mapped_column(String(120), nullable=False, default="system")
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    resource_id: Mapped[str] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# ----------------------------------------------------------------------------
# Telegram dedup — update_id idempotency (BIGINT)
# ----------------------------------------------------------------------------
class TelegramUpdate(Base):
    __tablename__ = "telegram_updates"
    __table_args__ = (UniqueConstraint("update_id", name="uq_tg_update_id"),)
    # BIGINT — Telegram update_id fits in 64-bit; prevents Integer overflow
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False)  # PENDING|PROCESSING|PROCESSED|FAILED
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


# ----------------------------------------------------------------------------
# Action execution — approval/action-based idempotent execution record
# action should not be lost due to consume-then-crash; same approval/action is not executed a second time
# ----------------------------------------------------------------------------
class ActionExecution(_UUIDMixin, _TimestampMixin, Base):
    __tablename__ = "action_executions"
    __table_args__ = (
        UniqueConstraint("approval_id", name="uq_action_exec_approval"),
        Index("ix_action_exec_run", "run_id"),
        Index("ix_action_exec_approval", "approval_id"),
    )
    approval_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("approvals.id"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id"), nullable=False
    )
    action_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    tool: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")  # PENDING|SUCCEEDED|FAILED|AMBIGUOUS
    result: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    approval: Mapped[Approval] = relationship()
    run: Mapped[Run] = relationship()


# ----------------------------------------------------------------------------
# Outbox — reliable queue (transactionel outbox pattern)
# ----------------------------------------------------------------------------
class OutboxMessage(_UUIDMixin, Base):
    __tablename__ = "outbox_messages"
    __table_args__ = (
        Index("ix_outbox_processed", "processed", "created_at"),
        Index("ix_outbox_topic", "topic"),
        UniqueConstraint("idempotency_key", name="uq_outbox_idempotency"),
    )
    topic: Mapped[str] = mapped_column(String(120), nullable=False, default="lumi.run_queued")
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    processed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str] = mapped_column(Text, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    # Redis Streams entry id after publish (for tracing)
    stream_id: Mapped[str] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

# ----------------------------------------------------------------------------
# M2: Agent Evaluator — 5-dimensional risk assessment of agent messages
# ----------------------------------------------------------------------------
class AgentEvaluation(_UUIDMixin, Base):
    __tablename__ = "agent_evaluations"
    __table_args__ = (
        UniqueConstraint("room", "seq", name="uq_agent_eval_room_seq"),
        Index("ix_agent_eval_tier", "tier"),
        Index("ix_agent_eval_score", "score"),
        Index("ix_agent_eval_room_seq", "room", "seq"),
    )
    room: Mapped[str] = mapped_column(String(64), nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    global_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    nick: Mapped[str] = mapped_column(String(120), nullable=False)
    did: Mapped[str | None] = mapped_column(String(80), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    raw_json: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    tier: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    dimensions: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    model: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

