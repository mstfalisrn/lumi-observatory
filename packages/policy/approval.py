# LUMI — ApprovalService (approval record creation + atomic decision + consume + replay protection + continuation + idempotent execution)
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from observability import models
from policy.engine import action_hash


class ApprovalService:
    """Shared approval service — Telegram and Web UI use the same path."""

    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def create(
        self,
        *,
        run_id: str,
        action_id: str,
        tool: str,
        arguments: dict,
        action_class: str,
        target: str,
        impact_summary: str = "",
        ttl_seconds: int = 3600,
    ) -> models.Approval:
        payload = {"action_id": action_id, "tool": tool, "arguments": arguments}
        h = action_hash(action_class, target, payload)
        a = models.Approval(
            action_class=action_class,
            action_hash=h,
            target=target,
            impact_summary=impact_summary,
            payload=payload,
            status=models.ApprovalStatus.PENDING.value,
            run_id=uuid.UUID(run_id) if run_id else None,
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
        )
        self.s.add(a)
        await self.s.flush()
        return a

    async def decide(self, approval_id: str, decision: str, user_id: str) -> models.Approval:
        """Atomic decision: SELECT FOR UPDATE + expiry + status + role (caller) check."""
        try:
            uid = uuid.UUID(approval_id)
        except ValueError:
            raise ValueError("invalid approval_id") from None
        res = await self.s.execute(
            select(models.Approval).where(models.Approval.id == uid).with_for_update()
        )
        a = res.scalar_one_or_none()
        if a is None:
            raise ValueError("approval not found")
        if a.status != models.ApprovalStatus.PENDING.value:
            raise ValueError(f"already decided: {a.status}")
        # expiry: for aware/naive comparison, make naive UTC-aware
        expires = a.expires_at
        if expires is not None:
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if expires < datetime.now(UTC):
                a.status = models.ApprovalStatus.EXPIRED.value
                await self.s.flush()
                raise ValueError("approval expired")
        if decision not in ("approve", "reject"):
            raise ValueError("decision must be approve|reject")
        a.status = models.ApprovalStatus.APPROVED.value if decision == "approve" else models.ApprovalStatus.REJECTED.value
        a.decision = decision
        a.decided_by_user_id = uuid.UUID(user_id) if user_id else None
        await self.s.flush()
        return a

    async def decide_with_continuation(
        self, approval_id: str, decision: str, user_id: str
    ) -> models.Approval:
        """Web+Telegram shared atomic continuation: approve+Run transition+outbox in the same transaction.

        - PENDING -> APPROVED/REJECTED
        - atomically transition the run from WAITING_APPROVAL to QUEUED (approve) or FAILED (reject)
        - outbox lumi.run_queued idempotent (approve:{id}) in the same transaction
        """
        a = await self.decide(approval_id, decision, user_id)
        # atomic run transition + outbox — in the same session transaction
        if a.run_id:
            # also lock the run
            run = await self.s.get(models.Run, a.run_id, with_for_update=True)  # type: ignore[call-arg]
            # Fallback to SELECT FOR UPDATE when get(with_for_update=...) is unavailable
            if run is None:
                res = await self.s.execute(
                    select(models.Run).where(models.Run.id == a.run_id).with_for_update()
                )
                run = res.scalar_one_or_none()
            if run is not None:
                if decision == "approve":
                    if run.status == models.RunStatus.WAITING_APPROVAL.value:
                        run.status = models.RunStatus.QUEUED.value
                        run.control_request = None
                    # outbox atomic — same transaction; failure raises in flush, not swallowed
                    self.s.add(
                        models.OutboxMessage(
                            topic="lumi.run_queued",
                            payload={"run_id": str(run.id), "approval_id": str(a.id)},
                            idempotency_key=f"approve:{a.id}",
                            processed=False,
                        )
                    )
                else:  # reject
                    if run.status == models.RunStatus.WAITING_APPROVAL.value:
                        run.status = models.RunStatus.FAILED.value
                        run.error = "approval_rejected"
                        run.finished_at = datetime.now(UTC)
        await self.s.flush()
        return a

    async def get(self, approval_id: str) -> models.Approval | None:
        try:
            uid = uuid.UUID(approval_id)
        except ValueError:
            return None
        return await self.s.get(models.Approval, uid)

    async def consume(self, approval_id: str) -> bool:
        """Mark as single-use after approval (replay protection)."""
        try:
            uid = uuid.UUID(approval_id)
        except ValueError:
            return False
        res = await self.s.execute(
            select(models.Approval).where(models.Approval.id == uid).with_for_update()
        )
        a = res.scalar_one_or_none()
        if a is None:
            return False
        if a.status != models.ApprovalStatus.APPROVED.value:
            return False  # only APPROVED can be consumed; CONSUMED/PENDING are rejected
        a.status = models.ApprovalStatus.CONSUMED.value
        await self.s.flush()
        return True

    async def consume_and_record(
        self, approval_id: str, run_id: str
    ) -> tuple[bool, models.Approval | None, models.ActionExecution | None]:
        """Crash-safe consume + idempotent execution record atomically.

        - transition APPROVED -> CONSUMED exactly once (FOR UPDATE)
        - ActionExecution(approval_id unique) is added as PENDING; a second consume of the same approval
          The unique constraint prevents a duplicate execution record.
        - in consume-then-crash case, approval remains CONSUMED + execution PENDING; recovery
          FAIL-CLOSED: public write is not re-run, marked AMBIGUOUS.
        Returns: (consumed, approval, execution)
        """
        try:
            aid = uuid.UUID(approval_id)
            rid = uuid.UUID(run_id)
        except ValueError:
            return False, None, None
        res = await self.s.execute(
            select(models.Approval).where(models.Approval.id == aid).with_for_update()
        )
        a = res.scalar_one_or_none()
        if a is None:
            return False, None, None
        # expiry aware/naive
        if a.expires_at is not None:
            exp = a.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=UTC)
            if exp < datetime.now(UTC):
                if a.status == models.ApprovalStatus.PENDING.value:
                    a.status = models.ApprovalStatus.EXPIRED.value
                    await self.s.flush()
                return False, a, None
        if a.status == models.ApprovalStatus.CONSUMED.value:
            ex_res = await self.s.execute(
                select(models.ActionExecution).where(models.ActionExecution.approval_id == aid)
            )
            ex = ex_res.scalar_one_or_none()
            return False, a, ex
        if a.status != models.ApprovalStatus.APPROVED.value:
            return False, a, None
        a.status = models.ApprovalStatus.CONSUMED.value
        payload = a.payload or {}
        action_id = payload.get("action_id") or str(a.id)
        tool = payload.get("tool") or ""
        ex = models.ActionExecution(
            approval_id=aid,
            run_id=rid,
            action_id=action_id,
            tool=tool,
            status="PENDING",
            result={},
        )
        self.s.add(ex)
        await self.s.flush()
        return True, a, ex

    async def mark_execution_result(
        self, approval_id: str, status: str, result: dict | None = None
    ) -> None:
        try:
            aid = uuid.UUID(approval_id)
        except ValueError:
            return
        res = await self.s.execute(
            select(models.ActionExecution).where(models.ActionExecution.approval_id == aid)
        )
        ex = res.scalar_one_or_none()
        if ex is not None:
            ex.status = status
            if result is not None:
                ex.result = result
            await self.s.flush()
