# LUMI — Scheduler
# Real work: source scanning + task creation, outbox publisher, stuck-run recovery, heartbeat
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from sqlalchemy import select

from observability import models
from observability.config import settings

# Fail-closed in production
if settings.is_production:
    settings.validate_production()
from observability.db import async_session_factory
from observability.events import append_run_event_in_session

# M3: AgentScorer hook — import without errors (apps.scheduler vs scheduler)
try:
    from scheduler.agent_scorer import AgentScorer, _AGENT_SCORER_TASK  # type: ignore  # noqa: I001
except ImportError:
    try:
        from apps.scheduler.agent_scorer import AgentScorer, _AGENT_SCORER_TASK  # type: ignore  # noqa: I001
    except ImportError:
        AgentScorer = None  # type: ignore
        _AGENT_SCORER_TASK = []  # type: ignore

app = FastAPI(title="LUMI Scheduler", version="1.0.0")


@app.get("/health/live")
async def health_live():
    return {"status": "live"}


class SchedulerLoop:
    def __init__(self, interval_seconds: int = 60) -> None:
        import redis as redis_lib

        self.redis = redis_lib.from_url(settings.REDIS_URL, decode_responses=True)
        self.interval = interval_seconds
        self._stuck_threshold = timedelta(minutes=15)
        self._lease_ms = 30000

    async def publish_outbox(self) -> int:
        """Outbox publisher: unprocessed messages -> Redis Streams (reliable)."""
        published = 0
        now = datetime.now(UTC)
        async with async_session_factory() as s:
            res = await s.execute(
                select(models.OutboxMessage)
                .where(models.OutboxMessage.processed.is_(False))
                .order_by(models.OutboxMessage.created_at.asc())
                .limit(20)
            )
            msgs = list(res.scalars().all())
            for m in msgs:
                if m.attempts > 10:
                    continue
                # not_before guard: future scheduled messages are skipped
                nb = getattr(m, "not_before", None)
                if nb is not None:
                    try:
                        if nb.tzinfo is None:
                            nb = nb.replace(tzinfo=UTC)  # type: ignore
                        if nb > now:
                            continue
                    except Exception:
                        pass
                try:
                    from observability.queue import ensure_stream_group, publish_to_stream

                    try:
                        ensure_stream_group(self.redis)
                    except Exception:
                        pass
                    stream_id = publish_to_stream(self.redis, m.payload, idempotency_key=m.idempotency_key)
                    m.processed = True
                    m.processed_at = datetime.now(UTC)
                    m.stream_id = str(stream_id)
                    m.attempts += 1
                    published += 1
                except Exception as e:
                    m.attempts += 1
                    m.last_error = f"{type(e).__name__}: {str(e)[:300]}"
            if published or msgs:
                await s.commit()
        return published

    async def recover_stuck_runs(self) -> int:
        """Stuck-run recovery: EXECUTING runs with heartbeat/lease expired -> FAILED + requeue."""
        recovered = 0
        cutoff = datetime.now(UTC) - self._stuck_threshold
        async with async_session_factory() as s:
            async with s.begin():
                res = await s.execute(
                    select(models.Run).where(
                        models.Run.status == models.RunStatus.EXECUTING.value,
                    )
                )
                runs = list(res.scalars().all())
                for r in runs:
                    heartbeat = r.heartbeat_at or r.updated_at
                    # SQLite returns naive datetimes; normalize to UTC aware for comparison
                    if heartbeat is not None and heartbeat.tzinfo is None:
                        heartbeat = heartbeat.replace(tzinfo=UTC)
                    now = datetime.now(UTC)
                    lease_expired = False
                    if r.lease_expires_at is not None:
                        lep = r.lease_expires_at
                        if lep.tzinfo is None:
                            lep = lep.replace(tzinfo=UTC)
                        lease_expired = lep < now
                    stuck = (heartbeat and heartbeat < cutoff) or lease_expired
                    if not stuck:
                        continue
                    r.status = models.RunStatus.FAILED.value
                    r.error = "stuck_run_recovered"
                    r.finished_at = datetime.now(UTC)
                    r.heartbeat_at = datetime.now(UTC)
                    await append_run_event_in_session(
                        s, r.id, "STUCK_RECOVERED", {"reason": "lease_expired", "worker_id": r.worker_id}
                    )
                    retry = (r.retry_count or 0) + 1
                    r.retry_count = retry
                    if retry > 3:
                        try:
                            from observability.queue import publish_to_dlq

                            publish_to_dlq(
                                self.redis,
                                {"run_id": str(r.id), "task_id": str(r.task_id), "retry_of": str(r.id)},
                                reason="max_retries_exceeded",
                            )
                        except Exception as e:
                            import logging as _log

                            _log.getLogger("lumi.scheduler").warning("DLQ publish failed %s", type(e).__name__)
                        await append_run_event_in_session(s, r.id, "DLQ", {"retry_count": retry})
                    else:
                        backoff_sec = 30 * (2 ** (retry - 1))
                        not_before = datetime.now(UTC) + timedelta(seconds=backoff_sec)
                        new_run = models.Run(
                            task_id=r.task_id,
                            status=models.RunStatus.QUEUED.value,
                            token_budget=r.token_budget,
                            cost_budget=r.cost_budget,
                            retry_count=retry,
                        )
                        s.add(new_run)
                        await s.flush()
                        ob = models.OutboxMessage(
                            topic="lumi.run_queued",
                            payload={"run_id": str(new_run.id), "task_id": str(new_run.task_id), "retry_of": str(r.id)},
                            idempotency_key=f"retry:{r.id}:{new_run.id}",
                            processed=False,
                            not_before=not_before,
                        )
                        s.add(ob)
                        await append_run_event_in_session(
                            s, new_run.id, "RETRY_QUEUED", {"from_run": str(r.id), "backoff_sec": backoff_sec}
                        )
                    recovered += 1
        return recovered

    async def auto_promote_memory(self) -> int:
        """Periodic memory lifecycle maintenance: expire stale items, then promote bounded candidates."""
        try:
            async with async_session_factory() as s:
                async with s.begin():
                    from memory.service import MemoryService

                    svc = MemoryService(s)
                    expired = await svc.expire_sweep()
                    promoted = await svc.auto_promote_candidates()
                    return expired + promoted
        except Exception as e:
            import logging as _log2

            _log2.getLogger("lumi.scheduler").warning("memory lifecycle failed %s", type(e).__name__)
            return 0

    async def observe_sources(self) -> int:
        """Execute opt-in source scans directly; never enqueue an unscoped LLM task."""
        if not settings.SOURCE_MONITOR_ENABLED:
            return 0
        from observability.source_monitor import SourceMonitor

        now = datetime.now(UTC)
        async with async_session_factory() as s:
            res = await s.execute(
                select(models.Source)
                .where(models.Source.is_enabled.is_(True))
                .order_by(models.Source.last_observed_at.asc().nullsfirst(), models.Source.name.asc())
                .limit(max(1, min(settings.SOURCE_MONITOR_MAX_PER_TICK, 50)))
            )
            source_ids = [row.id for row in res.scalars().all()]

        observed = 0
        for source_id in source_ids:
            async with async_session_factory() as s:
                async with s.begin():
                    row = await s.execute(select(models.Source).where(models.Source.id == source_id).with_for_update())
                    source = row.scalar_one_or_none()
                    if source is None or not source.is_enabled:
                        continue
                    backoff = source.backoff_until
                    if backoff is not None:
                        backoff = backoff if backoff.tzinfo is not None else backoff.replace(tzinfo=UTC)
                        if backoff > now:
                            continue
                    outcome = await SourceMonitor().observe(s, source)
                    s.add(
                        models.AuditEvent(
                            actor="system",
                            action="source.observed",
                            resource_type="source",
                            resource_id=str(source.id),
                            detail={
                                "change_type": outcome.change_type,
                                "changed": outcome.changed,
                                "report_id": outcome.report_id,
                                "error_code": outcome.error_code,
                            },
                        )
                    )
                    observed += 1
        return observed

    async def run_due_digests(self) -> int:
        """Generate due local reports only. No external delivery is performed here."""
        if not settings.DIGEST_ENABLED:
            return 0
        from observability.digest_service import DigestService

        async with async_session_factory() as s:
            async with s.begin():
                results = await DigestService().run_due(s, limit=settings.DIGEST_MAX_SCHEDULES_PER_TICK)
                for result in results:
                    s.add(
                        models.AuditEvent(
                            actor="system",
                            action="digest.generated",
                            resource_type="digest_schedule",
                            resource_id=result.schedule_id,
                            detail={
                                "report_id": result.report_id,
                                "created": result.created,
                                "source_change_count": result.source_change_count,
                                "risk_item_count": result.risk_item_count,
                            },
                        )
                    )
                return len(results)

    async def check_sources(self) -> int:
        """Scheduler tick: reliability maintenance, opt-in observation, and opt-in local digests."""
        await self.publish_outbox()
        await self.recover_stuck_runs()
        await self.auto_promote_memory()
        observed = await self.observe_sources()
        await self.run_due_digests()
        return observed

    async def run(self) -> None:
        while True:
            try:
                await self.check_sources()
            except Exception as e:
                import logging as _log2

                _log2.getLogger("lumi.scheduler").warning("scheduler loop failed %s", type(e).__name__)
            await asyncio.sleep(self.interval)


_BG_TASKS: list = []


async def _agent_scorer_loop() -> None:
    """M3: AgentScorer.poll_once loop every 15 seconds."""
    if AgentScorer is None:
        return
    scorer = AgentScorer(interval=15)
    while True:
        try:
            async with async_session_factory() as _s:
                await scorer.poll_once(_s)
        except Exception as e:
            import logging as _lg

            _lg.getLogger("lumi.scheduler").warning("agent_scorer poll error: %s", type(e).__name__)
        await asyncio.sleep(15)


@app.on_event("startup")
async def _start():
    loop = SchedulerLoop(interval_seconds=60)
    _BG_TASKS.append(asyncio.create_task(loop.run()))
    # M3: AgentScorer 15s poller
    try:
        _AGENT_SCORER_TASK.append(asyncio.create_task(_agent_scorer_loop()))
    except Exception:
        pass
