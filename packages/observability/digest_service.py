"""Deterministic, report-only observatory digests.

A digest aggregates already persisted observations and risk metadata. It does
not call external services and it never sends a message; delivery requires a
separate approval-gated connector.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from observability import models
from observability.security import redact

_TIER_RANK = {"SAFE": 0, "WATCH": 1, "RISKY": 2, "DANGEROUS": 3}
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{2,119}$")


class DigestValidationError(ValueError):
    pass


@dataclass(frozen=True)
class DigestResult:
    schedule_id: str
    schedule_name: str
    since: str
    generated_at: str
    source_change_count: int
    risk_item_count: int
    report_id: str | None
    created: bool

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_digest_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    raw = payload if isinstance(payload, dict) else {}
    name = str(raw.get("name") or "").strip()
    if not _NAME_RE.fullmatch(name):
        raise DigestValidationError("invalid_digest_name")
    interval = raw.get("interval_minutes", 1440)
    if not isinstance(interval, int) or isinstance(interval, bool) or not 15 <= interval <= 7 * 24 * 60:
        raise DigestValidationError("invalid_digest_interval")
    source_ids_raw = raw.get("source_ids", [])
    if not isinstance(source_ids_raw, list) or len(source_ids_raw) > 50:
        raise DigestValidationError("invalid_digest_sources")
    source_ids: list[str] = []
    for value in source_ids_raw:
        try:
            source_ids.append(str(uuid.UUID(str(value))))
        except (TypeError, ValueError) as exc:
            raise DigestValidationError("invalid_digest_source_id") from exc
    tier = str(raw.get("minimum_tier") or "WATCH").upper()
    if tier not in _TIER_RANK:
        raise DigestValidationError("invalid_digest_minimum_tier")
    enabled = raw.get("is_enabled", False)
    if not isinstance(enabled, bool):
        raise DigestValidationError("invalid_digest_enabled")
    return {
        "name": name,
        "interval_minutes": interval,
        "source_ids": sorted(set(source_ids)),
        "minimum_tier": tier,
        "is_enabled": enabled,
        # It is deliberately not configurable through the API.
        "delivery_mode": "report_only",
    }


def _safe_datetime(value: datetime | None, fallback: datetime) -> datetime:
    if value is None:
        return fallback
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class DigestService:
    async def preview(
        self,
        session: AsyncSession,
        *,
        since: datetime,
        source_ids: list[str] | None = None,
        minimum_tier: str = "WATCH",
    ) -> dict[str, Any]:
        source_uuid_ids = [uuid.UUID(value) for value in (source_ids or [])]
        observations_stmt = (
            select(models.SourceObservation, models.Source)
            .join(models.Source, models.Source.id == models.SourceObservation.source_id)
            .where(
                models.SourceObservation.observed_at >= since,
                models.SourceObservation.change_type.in_(("INITIAL", "CHANGED")),
            )
            .order_by(models.SourceObservation.observed_at.asc(), models.Source.name.asc())
            .limit(200)
        )
        if source_uuid_ids:
            observations_stmt = observations_stmt.where(models.SourceObservation.source_id.in_(source_uuid_ids))
        observation_rows = (await session.execute(observations_stmt)).all()
        source_changes = [
            {
                "source_id": str(source.id),
                "source_name": source.name,
                "source_type": source.source_type,
                "observed_at": observation.observed_at.isoformat(),
                "change_type": observation.change_type,
                "content_hash": observation.hash,
                "metadata": observation.change.get("metadata", {}) if isinstance(observation.change, dict) else {},
                "untrusted": True,
            }
            for observation, source in observation_rows
        ]

        threshold = _TIER_RANK[minimum_tier]
        evaluation_stmt = (
            select(models.AgentEvaluation)
            .where(models.AgentEvaluation.evaluated_at >= since)
            .order_by(
                models.AgentEvaluation.evaluated_at.asc(),
                models.AgentEvaluation.room.asc(),
                models.AgentEvaluation.seq.asc(),
            )
            .limit(200)
        )
        evaluations = list((await session.execute(evaluation_stmt)).scalars().all())
        risk_items = [
            {
                "room": item.room,
                "seq": item.seq,
                "nick": redact(item.nick)[:120],
                "tier": item.tier,
                "score": item.score,
                "reason": redact(item.reason)[:240],
                "evaluated_at": item.evaluated_at.isoformat(),
                "untrusted": True,
            }
            for item in evaluations
            if _TIER_RANK.get(item.tier.upper(), -1) >= threshold
        ]
        return {
            "schema_version": 1,
            "since": since.isoformat(),
            "generated_at": datetime.now(UTC).isoformat(),
            "source_changes": source_changes,
            "risk_items": risk_items,
            "delivery_mode": "report_only",
        }

    async def generate(
        self,
        session: AsyncSession,
        schedule: models.DigestSchedule,
        *,
        now: datetime | None = None,
        force: bool = False,
    ) -> DigestResult:
        generated_at = now or datetime.now(UTC)
        since = _safe_datetime(schedule.last_generated_at, generated_at - timedelta(minutes=schedule.interval_minutes))
        body = await self.preview(
            session,
            since=since,
            source_ids=[str(value) for value in schedule.source_ids],
            minimum_tier=schedule.minimum_tier,
        )
        changes = body["source_changes"]
        risks = body["risk_items"]
        schedule.last_generated_at = generated_at
        if not force and not changes and not risks:
            return DigestResult(
                schedule_id=str(schedule.id),
                schedule_name=schedule.name,
                since=since.isoformat(),
                generated_at=generated_at.isoformat(),
                source_change_count=0,
                risk_item_count=0,
                report_id=None,
                created=False,
            )

        summary = f"{len(changes)} source change(s); {len(risks)} risk item(s) at {schedule.minimum_tier}+ threshold."
        report = models.Report(
            report_type="observatory_digest",
            subject=schedule.name,
            summary=summary,
            body=body,
            confidence=1.0,
        )
        session.add(report)
        await session.flush()
        return DigestResult(
            schedule_id=str(schedule.id),
            schedule_name=schedule.name,
            since=since.isoformat(),
            generated_at=generated_at.isoformat(),
            source_change_count=len(changes),
            risk_item_count=len(risks),
            report_id=str(report.id),
            created=True,
        )

    async def run_due(
        self, session: AsyncSession, *, now: datetime | None = None, limit: int = 10
    ) -> list[DigestResult]:
        current = now or datetime.now(UTC)
        rows = list(
            (
                await session.execute(
                    select(models.DigestSchedule)
                    .where(models.DigestSchedule.is_enabled.is_(True))
                    .order_by(models.DigestSchedule.name.asc())
                    .limit(max(1, min(limit, 50)))
                )
            )
            .scalars()
            .all()
        )
        results: list[DigestResult] = []
        for schedule in rows:
            last = _safe_datetime(schedule.last_generated_at, current - timedelta(minutes=schedule.interval_minutes))
            if schedule.last_generated_at is not None and current - last < timedelta(minutes=schedule.interval_minutes):
                continue
            results.append(await self.generate(session, schedule, now=current))
        return results
