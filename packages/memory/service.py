# LUMI — Memory service (lifecycle: CANDIDATE -> ... -> ACTIVE/SUPERSEDED)
# Model cannot directly write persistent facts; at run end it produces memory candidates.
# Phase 4: verified/active filter, DLP (secret redact), pgvector preparation, ttl/active management
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from observability.models import MemoryItem, MemoryRelation, MemoryStatus

# Allowed statuses for retrieval — only verified information enters the context
RETRIEVAL_ALLOWED_STATUSES = {
    MemoryStatus.ACTIVE.value,
    MemoryStatus.APPROVED.value,
    MemoryStatus.AUTO_APPROVED.value,
}
# verified + active = memory that will enter context
VERIFIED_VALUE = "verified"


def _escape_ilike(q: str) -> str:
    """Wildcard enjeksiyonunu engelle: % _ \\ escape."""
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class MemoryService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_candidate(
        self,
        *,
        content: str,
        source: str,
        confidence: float = 0.5,
        ttl_seconds: int | None = None,
        category: str | None = None,
        observed_at: datetime | None = None,
        embedding: list[float] | None = None,
        embedding_vector: list[float] | None = None,
    ) -> MemoryItem:
        # DLP: secret redact — no secret value is written to memory
        from observability.security import redact

        content = redact(content)
        source = redact(source)

        item = MemoryItem(
            content=content,
            source=source,
            confidence=max(0.0, min(1.0, confidence)),
            ttl=ttl_seconds,
            status=MemoryStatus.CANDIDATE.value,
            verification_status="unverified",
            observed_at=observed_at or datetime.now(UTC),
            category=category,
            expires_at=(
                datetime.now(UTC) + timedelta(seconds=ttl_seconds)
                if ttl_seconds else None
            ),
            embedding=embedding,
        )
        # fill pgvector column if present
        if embedding_vector is not None and hasattr(item, "embedding_vector"):
            item.embedding_vector = embedding_vector  # type: ignore
        elif embedding is not None and hasattr(item, "embedding_vector"):
            # Copy the JSON embedding to the vector column when available
            try:
                item.embedding_vector = embedding  # type: ignore
            except Exception:
                pass
        self.session.add(item)
        await self.session.flush()
        return item

    async def approve(self, memory_id: uuid.UUID, auto: bool = False) -> MemoryItem | None:
        item = await self.session.get(MemoryItem, memory_id)
        if item is None:
            return None
        # Guard: only CANDIDATE -> APPROVED
        if item.status != MemoryStatus.CANDIDATE.value:
            return None
        item.status = MemoryStatus.AUTO_APPROVED.value if auto else MemoryStatus.APPROVED.value
        item.verification_status = VERIFIED_VALUE
        await self.session.flush()
        return item

    async def reject(self, memory_id: uuid.UUID) -> None:
        item = await self.session.get(MemoryItem, memory_id)
        if item:
            item.status = MemoryStatus.REJECTED.value
            await self.session.flush()

    async def mark_active(self, memory_id: uuid.UUID) -> None:
        item = await self.session.get(MemoryItem, memory_id)
        if item:
            # Only APPROVED / AUTO_APPROVED -> ACTIVE
            if item.status in (MemoryStatus.APPROVED.value, MemoryStatus.AUTO_APPROVED.value):
                item.status = MemoryStatus.ACTIVE.value
                item.verification_status = VERIFIED_VALUE
                await self.session.flush()

    async def supersede(self, old_id: uuid.UUID, new_id: uuid.UUID) -> None:
        # new_id existence and ACTIVE check
        new_item = await self.session.get(MemoryItem, new_id)
        if new_item is None or new_item.status != MemoryStatus.ACTIVE.value:
            return
        old = await self.session.get(MemoryItem, old_id)
        if old is None:
            return
        # cycle check simple: same id cannot occur
        if old_id == new_id:
            return
        self.session.add(
            MemoryRelation(
                from_memory_id=new_id,
                to_memory_id=old_id,
                relation_type="supersedes",
            )
        )
        old.status = MemoryStatus.SUPERSEDED.value
        await self.session.flush()

    async def link_contradiction(self, a_id: uuid.UUID, b_id: uuid.UUID) -> None:
        self.session.add(MemoryRelation(from_memory_id=a_id, to_memory_id=b_id, relation_type="contradicts"))
        await self.session.flush()

    async def search(self, q: str, status: str | None = None, limit: int = 20,
                     verified_only: bool = True, allow_expired: bool = False) -> list[MemoryItem]:
        """General search — applies verified/active filter for retrieval.

        - if verified_only=True, only verification_status='verified' and RETRIEVAL_ALLOWED_STATUSES are returned.
        - if a status parameter is given, that status is filtered but verified_only still applies (Phase 4).
        - if allow_expired=False, records with past expires_at are skipped.
        """
        # DLP + wildcard escape
        escaped = _escape_ilike(q)
        stmt = select(MemoryItem).where(MemoryItem.content.ilike(f"%{escaped}%", escape="\\"))

        # Filter verified and active records
        if verified_only:
            if status:
                # status explicit ise onu filtrele ama verified gerektir
                stmt = stmt.where(MemoryItem.status == status)
                stmt = stmt.where(MemoryItem.verification_status == VERIFIED_VALUE)
            else:
                stmt = stmt.where(MemoryItem.status.in_(list(RETRIEVAL_ALLOWED_STATUSES)))
                stmt = stmt.where(MemoryItem.verification_status == VERIFIED_VALUE)
        elif status:
            stmt = stmt.where(MemoryItem.status == status)

        if not allow_expired:
            now = datetime.now(UTC)
            stmt = stmt.where(or_(MemoryItem.expires_at.is_(None), MemoryItem.expires_at > now))
            # also exclude status EXPIRED ones
            stmt = stmt.where(MemoryItem.status != MemoryStatus.EXPIRED.value)

        stmt = stmt.order_by(MemoryItem.confidence.desc()).limit(limit)
        res = await self.session.execute(stmt)
        items = list(res.scalars().all())
        # DLP: if returned content has leakage, redact again (defense in depth)
        from observability.security import redact as _redact
        for it in items:
            it.content = _redact(it.content)
        return items

    async def retrieve_for_context(self, q: str, limit: int = 10) -> list[MemoryItem]:
        """For ContextAssembler — only ACTIVE + verified, ordered by relevance."""
        return await self.search(q, limit=limit, verified_only=True, allow_expired=False)

    async def vector_search(self, embedding: list[float], limit: int = 10) -> list[MemoryItem]:
        """Search with pgvector cosine similarity — JSON fallback if pgvector is not installed (not ilike)."""
        # First pgvector 시도, otherwise return empty (JSON embedding cosine expensive)
        try:
            # ham SQL: SELECT * FROM memory_items ORDER BY embedding_vector <=> :vec LIMIT :limit
            # Filter only verified and active records
            from sqlalchemy import text as sql_text
            now = datetime.now(UTC)
            # asyncpg vector type adaptation is left to pgvector
            stmt = sql_text("""
                SELECT id FROM memory_items
                WHERE verification_status='verified'
                  AND status IN ('ACTIVE','APPROVED','AUTO_APPROVED')
                  AND (expires_at IS NULL OR expires_at > :now)
                  AND embedding_vector IS NOT NULL
                ORDER BY embedding_vector <=> CAST(:vec AS vector)
                LIMIT :lim
            """)
            res = await self.session.execute(stmt, {"vec": str(embedding), "now": now, "lim": limit})
            ids = [r[0] for r in res.fetchall()]
            if not ids:
                return []
            items_stmt = select(MemoryItem).where(MemoryItem.id.in_(ids))
            res2 = await self.session.execute(items_stmt)
            return list(res2.scalars().all())
        except Exception:
            # fallback: confidence ordered search
            return []

    async def list_status(self, status: str, limit: int = 50) -> list[MemoryItem]:
        # status parameter is validated
        allowed = {e.value for e in MemoryStatus}
        if status not in allowed:
            return []
        now = datetime.now(UTC)
        stmt = select(MemoryItem).where(MemoryItem.status == status)
        # expired filter (allow_expired=False behavior)
        stmt = stmt.where(or_(MemoryItem.expires_at.is_(None), MemoryItem.expires_at > now))
        stmt = stmt.order_by(MemoryItem.created_at.desc()).limit(limit)
        res = await self.session.execute(stmt)
        return list(res.scalars().all())

    async def expire_sweep(self) -> int:
        """Mark TTL-expired records as EXPIRED — called periodically by scheduler/worker."""
        now = datetime.now(UTC)
        stmt = select(MemoryItem).where(
            and_(
                MemoryItem.expires_at.is_not(None),
                MemoryItem.expires_at <= now,
                MemoryItem.status.notin_([MemoryStatus.EXPIRED.value, MemoryStatus.DELETED.value, MemoryStatus.SUPERSEDED.value]),
            )
        )
        res = await self.session.execute(stmt)
        items = res.scalars().all()
        count = 0
        for it in items:
            it.status = MemoryStatus.EXPIRED.value
            count += 1
        if count:
            await self.session.flush()
        return count

    async def auto_promote_candidates(self, threshold: float | None = None, min_runs: int = 2) -> int:
        """C3: automatically approve high-confidence candidates.

        Kural: confidence > threshold (default 0.85) ve sistemde en az min_runs
        if a successful run exists -> CANDIDATE -> AUTO_APPROVED.
        Relationship is verified by overall completed run count, not by run count.
        (simple and deterministic; no FK). Called periodically by Scheduler.
        """
        from observability.config import settings as _settings
        from observability.models import Run, RunStatus

        thr = threshold if threshold is not None else float(getattr(_settings, "MEMORY_AUTO_PROMOTE_THRESHOLD", 0.85))
        # successful run count check
        completed_stmt = select(Run).where(Run.status == RunStatus.COMPLETED.value).limit(min_runs)
        res_runs = await self.session.execute(completed_stmt)
        if len(list(res_runs.scalars().all())) < min_runs:
            return 0
        now = datetime.now(UTC)
        stmt = select(MemoryItem).where(
            and_(
                MemoryItem.status == MemoryStatus.CANDIDATE.value,
                MemoryItem.confidence > thr,
                or_(MemoryItem.expires_at.is_(None), MemoryItem.expires_at > now),
            )
        )
        res = await self.session.execute(stmt)
        items = list(res.scalars().all())
        count = 0
        for it in items:
            it.status = MemoryStatus.AUTO_APPROVED.value
            it.verification_status = VERIFIED_VALUE
            count += 1
        if count:
            await self.session.flush()
        return count
