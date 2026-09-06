# LUMI — Worker
# Redis Streams consumer group + ACK/lease, outbox compatible + P0-R2 continuation + safe RunEvent
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid

from fastapi import FastAPI
from sqlalchemy import select, text

from agent_core.coordinator import RunBudget, RunCoordinator
from agent_core.executor import ToolExecutor, build_default_registry
from agent_core.llm import build_provider
from agent_core.planner import Planner
from agent_core.verifier import DefaultVerifier
from context_engine.assembler import ContextAssembler
from observability import models
from observability.config import settings

# Fail-closed in production
if settings.is_production:
    settings.validate_production()
from observability.db import async_session_factory
from observability.events import CriticalEventPersistenceError, append_run_event_safe
from observability.queue import ack, claim_pending, ensure_stream_group, read_group
from policy.engine import PolicyEngine

log = logging.getLogger("lumi.worker")

app = FastAPI(title="LUMI Worker", version="1.0.0")
_worker: WorkerLoop | None = None


@app.get("/health/live")
async def health_live():
    return {"status": "live"}


# backward compat alias for tests that patch worker._append_run_event_safe
_append_run_event_safe = append_run_event_safe


class WorkerLoop:
    def __init__(self) -> None:
        import redis as redis_lib
        self.redis = redis_lib.from_url(settings.REDIS_URL, decode_responses=True)
        self.consumer_name = f"worker-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.registry = build_default_registry(
            http_hosts=set(filter(None, settings.CONNECTOR_ALLOWED_HOSTS.split(","))) if settings.CONNECTOR_ALLOWED_HOSTS else None,
            technocore_key_path=settings.TECHNOCORE_ED25519_KEY_PATH or "./secrets/did.ed25519",
            technocore_base_url=settings.TECHNOCORE_BASE_URL,
        )
        self.provider = build_provider()
        self.planner = Planner(provider=self.provider)
        self.policy = PolicyEngine()
        self.verifier = DefaultVerifier()
        # ensure consumer group exists (idempotent)
        try:
            ensure_stream_group(self.redis)
        except Exception:
            pass
        self._lease_ms = 30000  # pending reclaim after 30s
        self._heartbeat_interval_s = 5  # lease renewal throughout the run

    async def _handle_entry(self, entry_id: str, fields: dict) -> bool:
        # fields: {"data": json, "idempotency_key": ...} (decode_responses=True)
        raw = fields.get("data") or fields.get(b"data")
        if raw is None:
            # malformed -> ack to avoid poison
            try:
                ack(self.redis, entry_id)
            except Exception:
                pass
            return True
        if isinstance(raw, bytes):
            raw = raw.decode()
        try:
            payload = json.loads(raw)
        except Exception:
            try:
                ack(self.redis, entry_id)
            except Exception:
                pass
            return True
        run_id = payload.get("run_id")
        if not run_id:
            try:
                ack(self.redis, entry_id)
            except Exception:
                pass
            return True
        # Critical path: only ACK on successful _process_run return; on exception, do NOT ACK so pending reclaims it
        ok = await self._process_run(run_id)
        try:
            ack(self.redis, entry_id)
        except Exception:
            pass
        return ok

    async def _process_run(self, run_id: str) -> bool:
        async with async_session_factory() as s:
            # idempotency: if run already terminal, skip execution
            run = await s.get(models.Run, uuid.UUID(run_id))
            if run is None:
                return True
            if run.status in (models.RunStatus.COMPLETED.value, models.RunStatus.FAILED.value, models.RunStatus.CANCELLED.value):
                return True
            task = await s.get(models.Task, run.task_id)
            if task is None:
                run.status = models.RunStatus.FAILED.value
                run.error = "task_not_found"
                await s.commit()
                return True
            # lease acquisition: ATOMIC claim (only QUEUED or lease-expired EXECUTING)
            import datetime as _dt
            now = _dt.datetime.now(_dt.UTC)
            lease = now + _dt.timedelta(milliseconds=self._lease_ms)
            res = await s.execute(
                text("""
                    UPDATE runs SET status='EXECUTING', worker_id=:w, heartbeat_at=:now,
                           lease_expires_at=:lease, started_at=COALESCE(started_at, :now)
                    WHERE id=:id AND (
                        status='QUEUED'
                        OR (status='EXECUTING' AND (lease_expires_at IS NULL OR lease_expires_at < :now))
                    )
                """),
                {"w": self.consumer_name, "now": now, "lease": lease, "id": str(uuid.UUID(run_id))},
            )
            await s.commit()
            if res.rowcount != 1:
                # another worker claimed it or it is terminal — skip this message (dedup)
                # if this message arrived before the approve transition made the run QUEUED,
                # it should have been re-queued to outbox/redis; skipping here is not correct
                # but if rowcount is 0, run status is not QUEUED — probably WAITING_APPROVAL has not been approved yet
                # in that case the claim is ignored and the next outbox poll will bring the QUEUED message
                return True

            # ---- P0-R2: continuation check — does the QUEUED run have an APPROVED approval? ----
            # This run may be a continuation that returned to QUEUED via approve; if so, without replanning
            # execute the exact action_id/tool/arguments from the Approval.payload snapshot.
            # Idempotent execution record (approval_id unique) provides consume-then-crash safety.
            continuation_handled = await self._try_continuation(uuid.UUID(run_id))
            if continuation_handled:
                return True

            # need fresh session for execution? reuse s after commit
            task_dict = {"prompt": task.prompt, "scope": task.scope}
            executor = ToolExecutor(self.registry, task=task_dict)
            coordinator = RunCoordinator(
                run_id=run_id,
                budget=RunBudget(),
                allowlist_tools=set(),
            )
            coordinator.status = models.RunStatus.EXECUTING
            assembler = ContextAssembler(max_tokens=rum_budget(run))
            assembler.add("task_goal", task.prompt, title=task.title, relevance=1.0)
            # STAGE 8: active + verified memory retrieval at task start (add to context)
            try:
                from memory.service import MemoryService
                mem = MemoryService(s)
                mem_items = await mem.retrieve_for_context(task.prompt, limit=10)
                for _m in mem_items:
                    assembler.add("memory", _m.content, title=f"memory:{_m.category or 'general'}",
                                  relevance=max(0.0, min(1.0, _m.confidence or 0.5)))
            except Exception:
                pass

            from observability.security import redact as _redact

            async def _sink(run_id: str, etype: str, payload: dict) -> None:
                # write to Plan/ToolCall tables DURING the run (live observation + crash safety) + RunEvent append-only
                try:
                    rid = uuid.UUID(run_id)
                except Exception:
                    return
                # write RunEvent immediately for each etype — safe seq; if critical, error is not swallowed (typed exception)
                try:
                    await _append_run_event_safe(rid, etype, payload)
                except Exception as e:
                    log.warning("event sink append failed %s %s: %s", rid, etype, type(e).__name__)
                    if etype in ("TOOL_CALL", "PLAN", "AWAITING_APPROVAL", "FATAL", "WORKER_ERROR"):
                        raise CriticalEventPersistenceError(f"{etype} event append failed") from e
                try:
                    async with async_session_factory() as s2:
                        if etype == "PLAN":
                            s2.add(models.Plan(run_id=rid, plan_json=payload.get("plan", {}),
                                               expected_evidence={}, status="active"))
                        elif etype == "TOOL_CALL":
                            args_j = json.dumps(payload.get("arguments", {}), default=str)
                            s2.add(models.ToolCall(
                                run_id=rid, tool_name=payload.get("tool", ""),
                                input_summary=args_j[:500],
                                input_redacted=_redact(args_j)[:500],
                                result_summary=json.dumps(payload.get("result", ""), default=str)[:500],
                                action_class="READ_ONLY", policy_decision="ALLOW",
                            ))
                        elif etype == "AWAITING_APPROVAL":
                            from policy.approval import ApprovalService
                            svc = ApprovalService(s2)
                            await svc.create(
                                run_id=run_id,
                                action_id=payload.get("action_id", ""),
                                tool=payload.get("tool", ""),
                                arguments=payload.get("arguments", {}),
                                action_class=payload.get("action_class", "PUBLIC_WRITE"),
                                target=json.dumps(payload.get("arguments", {}), default=str)[:200],
                                impact_summary=f"tool {payload.get('tool','')} awaiting approval",
                            )
                        await s2.commit()
                except Exception as e:
                    log.warning("event sink DB error %s: %s", etype, type(e).__name__)
                    if etype in ("PLAN", "TOOL_CALL", "AWAITING_APPROVAL"):
                        raise CriticalEventPersistenceError(f"critical secondary persistence failure {etype}") from e

            async def _pause_check() -> bool:
                try:
                    async with async_session_factory() as s3:
                        r = await s3.get(models.Run, uuid.UUID(run_id))
                        return bool(r and r.control_request == "pause")
                except Exception:
                    return False

            async def _stop_check() -> bool:
                try:
                    async with async_session_factory() as s3:
                        r = await s3.get(models.Run, uuid.UUID(run_id))
                        return bool(r and r.control_request == "stop")
                except Exception:
                    return False

            # heartbeat: renew lease regularly throughout the run (long run is not considered stuck)
            hb_stop = asyncio.Event()

            async def _heartbeat_loop():
                while not hb_stop.is_set():
                    try:
                        async with async_session_factory() as s_hb:
                            hb_now = _dt.datetime.now(_dt.UTC)
                            await s_hb.execute(
                                text("UPDATE runs SET heartbeat_at=:now, lease_expires_at=:lease WHERE id=:id AND worker_id=:w"),
                                {"now": hb_now, "lease": hb_now + _dt.timedelta(milliseconds=self._lease_ms),
                                 "id": str(uuid.UUID(run_id)), "w": self.consumer_name},
                            )
                            await s_hb.commit()
                    except Exception:
                        pass
                    try:
                        await asyncio.wait_for(hb_stop.wait(), timeout=self._heartbeat_interval_s)
                    except TimeoutError:
                        pass

            hb_task = asyncio.create_task(_heartbeat_loop())
            try:
                status, _executed, _events = await coordinator.run(executor, self.planner,
                                                                 assembler, self.policy,
                                                                 self.provider, self.verifier,
                                                                 event_sink=_sink,
                                                                 pause_check=_pause_check,
                                                                 stop_check=_stop_check)
            except Exception as e:
                from observability.security import redact as _redact2
                if isinstance(e, CriticalEventPersistenceError):
                    raise
                try:
                    await _append_run_event_safe(uuid.UUID(run_id), "FATAL", {"error": type(e).__name__, "detail": _redact2(str(e))[:500]})
                except Exception as e2:
                    log.warning("FATAL event append failed %s: %s", run_id, type(e2).__name__)
                    # do not swallow: propagate so job not ACKed as success; re-raise to mark FAILED visibly
                    raise
                status = models.RunStatus.FAILED.value
                # error will be set below
            finally:
                hb_stop.set()
                hb_task.cancel()
            # reload run for update (avoid stale)
            run2 = await s.get(models.Run, uuid.UUID(run_id))
            if run2 is None:
                return True
            run2.status = status
            run2.iteration = coordinator.iteration
            run2.token_used = coordinator.tokens_used
            run2.cost_used = coordinator.cost_used
            run2.finished_at = _dt.datetime.now(_dt.UTC) if status in (models.RunStatus.COMPLETED.value, models.RunStatus.FAILED.value, models.RunStatus.CANCELLED.value) else run2.finished_at
            if status == models.RunStatus.FAILED.value:
                run2.error = run2.error or "worker_execution_error"
            run2.heartbeat_at = _dt.datetime.now(_dt.UTC)
            try:
                await s.commit()
            except Exception as e:
                await s.rollback()
                log.warning("run commit error %s: %s", run_id, type(e).__name__)
                raise
        return True

    async def _try_continuation(self, run_id: uuid.UUID) -> bool:
        """After QUEUED->EXECUTING claim, if this run has an APPROVED approval, execute with the payload snapshot.

        Returns True if continuation was handled (caller should return), False to proceed normal coordinator.
        """
        import datetime as _dt
        # find APPROVED approval for this run (most recent)
        async with async_session_factory() as s2:
            res = await s2.execute(
                select(models.Approval).where(
                    models.Approval.run_id == run_id,
                    models.Approval.status == models.ApprovalStatus.APPROVED.value,
                ).order_by(models.Approval.created_at.desc()).with_for_update()
            )
            appr = res.scalars().first()
            # also check for crash-after-consume: APPROVED might have been consumed but execution PENDING
            # FAIL-CLOSED: don't auto-retry public write without replay-safe idempotency -> AMBIGUOUS
            if appr is None:
                res2 = await s2.execute(
                    select(models.Approval).where(
                        models.Approval.run_id == run_id,
                        models.Approval.status == models.ApprovalStatus.CONSUMED.value,
                    ).order_by(models.Approval.created_at.desc()).with_for_update()
                )
                appr2 = res2.scalars().first()
                if appr2 is not None:
                    ex_res = await s2.execute(
                        select(models.ActionExecution).where(models.ActionExecution.approval_id == appr2.id)
                    )
                    ex = ex_res.scalar_one_or_none()
                    if ex is not None and ex.status == "PENDING":
                        # fail-closed: duplicate external effect would be unsafe — never re-execute
                        ex.status = "AMBIGUOUS"
                        await s2.flush()
                        await _append_run_event_safe(run_id, "APPROVAL_AMBIGUOUS", {"approval_id": str(appr2.id), "reason": "pending_after_crash_fail_closed"})
                        run_row = await s2.get(models.Run, run_id, with_for_update=True)  # type: ignore[call-arg]
                        if run_row is None:
                            res_r = await s2.execute(select(models.Run).where(models.Run.id == run_id).with_for_update())
                            run_row = res_r.scalar_one_or_none()
                        if run_row is not None and run_row.status == models.RunStatus.EXECUTING.value:
                            run_row.status = models.RunStatus.FAILED.value
                            run_row.error = "needs_reconciliation"
                            run_row.finished_at = _dt.datetime.now(_dt.UTC)
                        await s2.commit()
                        return True
                    elif ex is not None and ex.status == "SUCCEEDED":
                        # already executed successfully — ensure run terminal COMPLETED, do not replan
                        run_row = await s2.get(models.Run, run_id, with_for_update=True)  # type: ignore[call-arg]
                        if run_row is None:
                            res_r = await s2.execute(select(models.Run).where(models.Run.id == run_id).with_for_update())
                            run_row = res_r.scalar_one_or_none()
                        if run_row is not None and run_row.status == models.RunStatus.EXECUTING.value:
                            run_row.status = models.RunStatus.COMPLETED.value
                            run_row.finished_at = _dt.datetime.now(_dt.UTC)
                        await s2.commit()
                        await _append_run_event_safe(run_id, "APPROVAL_REPLAY_BLOCKED", {"approval_id": str(appr2.id), "reason": "already_succeeded"})
                        return True
                    elif ex is not None and ex.status in ("FAILED", "AMBIGUOUS"):
                        run_row = await s2.get(models.Run, run_id, with_for_update=True)  # type: ignore[call-arg]
                        if run_row is None:
                            res_r = await s2.execute(select(models.Run).where(models.Run.id == run_id).with_for_update())
                            run_row = res_r.scalar_one_or_none()
                        if run_row is not None and run_row.status == models.RunStatus.EXECUTING.value:
                            run_row.status = models.RunStatus.FAILED.value
                            run_row.error = "needs_reconciliation"
                            run_row.finished_at = _dt.datetime.now(_dt.UTC)
                        await s2.commit()
                        await _append_run_event_safe(run_id, "APPROVAL_REPLAY_BLOCKED", {"approval_id": str(appr2.id), "reason": ex.status.lower()})
                        return True
                    else:
                        # CONSUMED but no execution or unknown state — fail closed
                        if ex is None:
                            ex_new = models.ActionExecution(approval_id=appr2.id, run_id=run_id, action_id=str(appr2.id), tool="", status="AMBIGUOUS", result={"reason": "consumed_without_execution"})
                            s2.add(ex_new)
                            await s2.flush()
                        run_row = await s2.get(models.Run, run_id, with_for_update=True)  # type: ignore[call-arg]
                        if run_row is None:
                            res_r = await s2.execute(select(models.Run).where(models.Run.id == run_id).with_for_update())
                            run_row = res_r.scalar_one_or_none()
                        if run_row is not None and run_row.status == models.RunStatus.EXECUTING.value:
                            run_row.status = models.RunStatus.FAILED.value
                            run_row.error = "needs_reconciliation"
                            run_row.finished_at = _dt.datetime.now(_dt.UTC)
                        await s2.commit()
                        await _append_run_event_safe(run_id, "APPROVAL_AMBIGUOUS", {"approval_id": str(appr2.id), "reason": "consumed_no_execution"})
                        return True
                else:
                    return False

            # found APPROVED — consume atomically + record
            from policy.approval import ApprovalService
            svc = ApprovalService(s2)
            if appr.status == models.ApprovalStatus.APPROVED.value:
                consumed, appr_locked, ex = await svc.consume_and_record(str(appr.id), str(run_id))
                await s2.commit()
                if not consumed:
                    await _append_run_event_safe(run_id, "APPROVAL_REPLAY_BLOCKED", {"approval_id": str(appr.id)})
                    return True
                payload = appr_locked.payload if appr_locked and appr_locked.payload else appr.payload
                payload = payload or {}
            else:
                # should not happen: CONSUMED path already handled fail-closed above
                return True

            tool = payload.get("tool") or ""
            args = payload.get("arguments") or {}
            ap_action_id = payload.get("action_id") or str(appr.id)
            # Replay guard: if already SUCCEEDED, handled above; do not inject idempotency_key
            # because TechnocoreConnector does not use idempotency_key remotely and nonce is per-call.
            # PENDING/IN_FLIGHT must never be replayed (fail-closed already).

            # (removed redundant replay guard — CONSUMED+SUCCEEDED already handled fail-closed above; no swallow)

            try:
                result = await self.registry.call(tool, **dict(args))
                await _append_run_event_safe(run_id, "TOOL_CALL", {"tool": tool, "arguments": args, "result": result, "action_id": ap_action_id, "approval_id": str(appr.id)})
                # single atomic finalization: ToolCall + ActionExecution SUCCEEDED + Run COMPLETED
                async with async_session_factory() as s_final:
                    from observability.security import redact as _redact  # type: ignore
                    s_final.add(models.ToolCall(
                        run_id=run_id, tool_name=tool,
                        input_summary=json.dumps(args, default=str)[:500],
                        input_redacted=_redact(json.dumps(args, default=str))[:500],
                        result_summary=json.dumps(result, default=str)[:500],
                        action_class="PUBLIC_WRITE", policy_decision="APPROVED",
                    ))
                    # ActionExecution SUCCEEDED
                    ex_res2 = await s_final.execute(select(models.ActionExecution).where(models.ActionExecution.approval_id == appr.id))
                    ex2 = ex_res2.scalar_one_or_none()
                    if ex2 is not None:
                        ex2.status = "SUCCEEDED"
                        ex2.result = {"result": result}
                    r_final = await s_final.get(models.Run, run_id)
                    if r_final:
                        r_final.status = models.RunStatus.COMPLETED.value
                        r_final.finished_at = _dt.datetime.now(_dt.UTC)
                    await s_final.commit()
                return True
            except Exception as e:
                log.warning("continuation tool error %s: %s", tool, type(e).__name__)
                await _append_run_event_safe(run_id, "TOOL_ERROR", {"tool": tool, "error": type(e).__name__, "approval_id": str(appr.id)})
                # PUBLIC_WRITE timeout/exception does not prove remote did NOT happen -> AMBIGUOUS
                # for safety, treat continuation failure as AMBIGUOUS/needs_reconciliation
                async with async_session_factory() as s_final:
                    ex_res2 = await s_final.execute(select(models.ActionExecution).where(models.ActionExecution.approval_id == appr.id))
                    ex2 = ex_res2.scalar_one_or_none()
                    if ex2 is not None:
                        from observability.security import redact as _redact3  # type: ignore
                        ex2.status = "AMBIGUOUS"
                        ex2.result = {"error": type(e).__name__, "detail": _redact3(str(e))[:500]}
                    r_final = await s_final.get(models.Run, run_id)
                    if r_final:
                        r_final.status = models.RunStatus.FAILED.value
                        r_final.error = "needs_reconciliation"
                        r_final.finished_at = _dt.datetime.now(_dt.UTC)
                    await s_final.commit()
                return True
        return False

    async def _fallback_legacy_queue(self) -> bool:
        """Consume legacy list lumi:queue for backward compatibility during rollout."""
        try:
            raw = self.redis.rpop("lumi:queue")
        except Exception:
            return False
        if not raw:
            return False
        if isinstance(raw, bytes):
            raw = raw.decode()
        try:
            payload = json.loads(raw)
            run_id = payload.get("run_id")
        except Exception:
            return True
        if run_id:
            await self._process_run(run_id)
        return True

    async def process_one(self) -> bool:
        # 1) try stream read (non-blocking first, then block)
        try:
            entries = read_group(self.redis, self.consumer_name, count=1, block_ms=2000)
            if entries:
                for _stream, msgs in entries:
                    for entry_id, fields in msgs:
                        await self._handle_entry(entry_id, fields)
                        return True
            # 2) reclaim pending that exceeded lease (stuck consumer)
            pending = claim_pending(self.redis, self.consumer_name, min_idle_ms=self._lease_ms, count=1)
            if pending:
                for entry_id, fields in pending:
                    await self._handle_entry(entry_id, fields)
                    return True
            # 3) fallback legacy queue
            return await self._fallback_legacy_queue()
        except Exception:
            # on redis error, still try legacy
            try:
                return await self._fallback_legacy_queue()
            except Exception:
                return False

    async def run(self) -> None:
        while True:
            try:
                processed = await self.process_one()
                if not processed:
                    await asyncio.sleep(0.5)
            except Exception:
                await asyncio.sleep(2.0)


def rum_budget(run) -> int:
    try:
        return int(run.token_budget)
    except Exception:
        return settings.RUN_MAX_TOKEN_BUDGET


_BG_TASKS: list = []


@app.on_event("startup")
async def _start():
    global _worker
    _worker = WorkerLoop()
    _BG_TASKS.append(asyncio.create_task(_worker.run()))
