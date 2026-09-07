# LUMI — Run Coordinator (task state machine + budget/limit/kill switch)
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass

from observability.config import settings
from observability.models import RunStatus


@dataclass
class RunBudget:
    max_iterations: int = settings.RUN_MAX_ITERATIONS
    max_tool_calls: int = settings.RUN_MAX_TOOL_CALLS
    max_wall_seconds: int = settings.RUN_MAX_WALL_SECONDS
    max_tokens: int = settings.RUN_MAX_TOKEN_BUDGET
    max_cost: float = settings.RUN_MAX_COST_BUDGET


class CircuitBreaker:
    def __init__(self, threshold: int = 5, cooldown: float = 30.0) -> None:
        self._threshold = threshold
        self._cooldown = cooldown
        self._failures: dict[str, list[float]] = {}

    def record_failure(self, key: str) -> bool:
        now = time.monotonic()
        lst = self._failures.setdefault(key, [])
        lst = [t for t in lst if now - t < self._cooldown]
        lst.append(now)
        self._failures[key] = lst
        return len(lst) >= self._threshold  # True if open

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)


class RunCoordinator:
    """Executes a single run QUEUED -> ... -> COMPLETED/FAILED/CANCELLED/PAUSED."""

    def __init__(
        self, run_id: str | None = None, budget: RunBudget | None = None, *, allowlist_tools: set[str] | None = None
    ) -> None:
        self.run_id = run_id or str(uuid.uuid4())
        self.budget = budget or RunBudget()
        self.status = RunStatus.QUEUED
        self.iteration = 0
        self.tool_calls_count = 0
        self.started_at: float | None = None
        self.tokens_used = 0
        self.cost_used = 0.0
        self._kill = False
        self._pause = False
        self._stop_event = asyncio.Event()
        self._breaker = CircuitBreaker()
        self.allowlist = allowlist_tools or set()
        self._events: list[dict] = []
        # Safe, machine-readable terminal reason. Never contains raw tool input
        # or exception text, so it can be persisted in Run.error and exposed to
        # operators without leaking secrets.
        self.failure_code = ""

    # --- Controls ---
    def request_stop(self) -> None:
        self._kill = True
        self._stop_event.set()

    def request_pause(self) -> None:
        self._pause = True

    def resume(self) -> None:
        self._pause = False

    @property
    def is_killed(self) -> bool:
        return self._kill

    def emit(self, etype: str, payload: dict) -> None:
        self._events.append({"event_type": etype, "payload": payload, "seq": len(self._events)})

    # --- guardrail ---
    def can_continue(self) -> bool:
        if self._kill:
            return False
        if self._pause:
            self.emit("PAUSED", {})
            return False
        if self.started_at and (time.monotonic() - self.started_at) > self.budget.max_wall_seconds:
            self.failure_code = "wall_time_exceeded"
            self.emit("WALL_TIME_EXCEEDED", {})
            return False
        if self.iteration >= self.budget.max_iterations:
            self.failure_code = "iteration_limit_exceeded"
            self.emit("ITERATION_LIMIT", {"limit": self.budget.max_iterations})
            return False
        if self.tool_calls_count >= self.budget.max_tool_calls:
            self.failure_code = "tool_call_limit_exceeded"
            self.emit("TOOL_CALL_LIMIT", {"limit": self.budget.max_tool_calls})
            return False
        if self.tokens_used > self.budget.max_tokens:
            self.failure_code = "token_budget_exceeded"
            self.emit("TOKEN_BUDGET_EXCEEDED", {})
            return False
        if self.cost_used > self.budget.max_cost:
            self.failure_code = "cost_budget_exceeded"
            self.emit("COST_BUDGET_EXCEEDED", {})
            return False
        return True

    # --- main loop ---
    async def _sink(self, event_sink, etype: str, payload: dict) -> None:
        if event_sink is not None:
            await event_sink(self.run_id, etype, payload)

    def _add_usage(self, usage: dict) -> None:
        if not usage:
            return
        try:
            pt = int(usage.get("prompt_tokens") or 0)
            ct = int(usage.get("completion_tokens") or 0)
            tt = int(usage.get("total_tokens") or (pt + ct))
            self.tokens_used += tt
            # cost estimate (low token-based estimate if opencode-go credit is unknown)
            self.cost_used += float(usage.get("cost") or 0.0)
        except Exception:
            pass

    async def run(
        self,
        executor,
        planner,
        assembler,
        policy,
        provider,
        verifier,
        event_sink=None,
        pause_check=None,
        stop_check=None,
    ):
        """Coordinator loop — actions with arguments, LLM context, budget/limit."""
        import json as _json

        self.status = RunStatus.CONTEXT_BUILDING
        self.started_at = time.monotonic()
        self.emit("STARTED", {"run_id": self.run_id})
        await self._sink(event_sink, "STARTED", {"run_id": self.run_id})

        # 1) plan
        self.status = RunStatus.PLANNING
        plan = await planner.make_plan(task=executor.task)
        self.emit("PLAN", {"plan": plan})
        await self._sink(event_sink, "PLAN", {"plan": plan})
        guard = plan.get("_plan_guard") if isinstance(plan, dict) else None
        if isinstance(guard, dict):
            # Plan guards are intentionally non-sensitive reason codes only.
            self.emit("PLAN_GUARD", guard)
            await self._sink(event_sink, "PLAN_GUARD", guard)
        self._add_usage(plan.get("_llm_usage") or {})

        # 2) context (prompt sent to model — for replan/decision)
        _ordered, _prompt = assembler.assemble()
        self.emit("CONTEXT", {"segments": assembler.inspector_metadata()})
        await self._sink(event_sink, "CONTEXT", {"segments": assembler.inspector_metadata()})

        executed: list[dict] = []
        had_error = False
        actions = plan.get("actions", [])
        if not actions:
            # old format tolerance: tools name list -> action without args
            actions = [
                {"action_id": f"action_{i + 1}", "tool": t, "arguments": {}}
                for i, t in enumerate(plan.get("tools", []))
            ]
        if not actions:
            # An explicit unsafe/malformed capability request must not quietly
            # become a successful unrelated health check.
            self.status = RunStatus.FAILED
            self.failure_code = "plan_guard_rejected" if isinstance(guard, dict) else "plan_has_no_actions"
            self.emit("VERIFY", {"passed": False, "reason": self.failure_code})
            await self._sink(event_sink, "VERIFY", {"passed": False, "reason": self.failure_code})
            self.emit("END", {"final_status": self.status.value})
            await self._sink(event_sink, "END", {"final_status": self.status.value})
            return self.status.value, executed, self._events

        for act in actions:
            if not isinstance(act, dict):
                continue
            tool = act.get("tool", "")
            args = act.get("arguments") or {}
            # pause/stop DB check (every iteration)
            if pause_check is not None and await pause_check():
                self.status = RunStatus.PAUSED
                self.emit("PAUSED", {})
                break
            if stop_check is not None and await stop_check():
                self.status = RunStatus.CANCELLED
                self.emit("CANCELLED", {})
                break
            if not self.can_continue():
                if not self._pause and not self._kill:
                    self.status = RunStatus.FAILED
                break
            self.iteration += 1
            self.status = RunStatus.EXECUTING

            decision = policy.decide(tool)
            self.emit("POLICY_CHECK", {"tool": tool, "arguments": args, "decision": decision.decision})
            await self._sink(
                event_sink, "POLICY_CHECK", {"tool": tool, "arguments": args, "decision": decision.decision}
            )

            if decision.decision == "DENY":
                self.status = RunStatus.FAILED
                self.failure_code = f"policy_denied:{tool or 'unknown'}"
                self.emit("DENIED", {"tool": tool})
                break

            if decision.decision == "REQUIRE_APPROVAL":
                self.status = RunStatus.WAITING_APPROVAL
                ap_payload = {
                    "tool": tool,
                    "arguments": args,
                    "action_id": act.get("action_id", ""),
                    "action_class": decision.action_class,
                }
                self.emit("AWAITING_APPROVAL", ap_payload)
                await self._sink(event_sink, "AWAITING_APPROVAL", ap_payload)
                break

            try:
                result = await executor.execute(tool, **args)
                self.tool_calls_count += 1
                executed.append(
                    {"action_id": act.get("action_id"), "tool": tool, "arguments": args, "result": result, "ok": True}
                )
                self.emit("TOOL_CALL", {"tool": tool, "arguments": args, "ok": True})
                await self._sink(
                    event_sink, "TOOL_CALL", {"tool": tool, "arguments": args, "result": result, "ok": True}
                )
                # tool output to context as UNTRUSTED_DATA
                assembler.add("untrusted", _json.dumps(result, default=str)[:4000], title=f"tool:{tool}", relevance=0.5)
                self._breaker.reset(tool)
            except Exception as e:
                had_error = True
                self.failure_code = f"tool_error:{tool or 'unknown'}:{type(e).__name__}"
                self.emit("TOOL_ERROR", {"tool": tool, "error": type(e).__name__, "msg": str(e)[:200]})
                await self._sink(event_sink, "TOOL_ERROR", {"tool": tool, "error": type(e).__name__})
                executed.append({"tool": tool, "arguments": args, "error": type(e).__name__, "ok": False})
                if self._breaker.record_failure(tool):
                    self.status = RunStatus.FAILED
                    self.emit("CIRCUIT_OPEN", {"tool": tool})
                    break

        # finalize
        if self._pause:
            self.status = RunStatus.PAUSED
        elif self._kill:
            self.status = RunStatus.CANCELLED
        elif self.status in (
            RunStatus.QUEUED,
            RunStatus.EXECUTING,
            RunStatus.PLANNING,
            RunStatus.CONTEXT_BUILDING,
            RunStatus.POLICY_CHECK,
        ):
            if had_error:
                self.status = RunStatus.FAILED
                self.emit("VERIFY", {"passed": False, "reason": "tool_error"})
            else:
                self.status = RunStatus.VERIFYING
                try:
                    vres = await verifier.verify({"evidence": executed})
                except Exception:
                    vres = None
                passed = bool(vres and vres.passed)
                if not passed:
                    self.failure_code = self.failure_code or "verification_failed"
                self.emit("VERIFY", {"passed": passed, "evidence_n": len(executed)})
                await self._sink(event_sink, "VERIFY", {"passed": passed, "evidence_n": len(executed)})
                self.status = RunStatus.PERSISTING if passed else RunStatus.FAILED

        if self.status == RunStatus.PERSISTING:
            self.status = RunStatus.COMPLETED
        self.emit("END", {"final_status": self.status.value})
        await self._sink(event_sink, "END", {"final_status": self.status.value})
        return self.status.value, executed, self._events
