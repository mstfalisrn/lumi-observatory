# LUMI — Jev decision client (TypeSafe System One via Vercel AI Gateway)
#
# Jev is an *evaluation* model, not a chat model: you post a state plus typed
# questions (choice / score / boolean / noul) and get back calibrated answers
# with probabilities — ~200 ms and ~$0.00002 per call. That makes it affordable
# as a decision layer on *every* step, while the expensive chat LLM only runs
# on the uncertain band (confidence between review and auto thresholds).
#
# HTTP API: POST {base}/evaluate  {"model", "state", "questions": {...}}
# Response: {"answers": {name: {choice|probability|score...}}, "usage": {...},
#            "providerMetadata": {"gateway": {"cost": "...", ...}}}
#
# Safety rules encoded here:
# - fail-closed: disabled / no key / cap exceeded / error / timeout -> JevUnavailable,
#   so callers keep their previous (stricter) path; Jev never silently loosens policy.
# - cost guard: per-minute and per-day call caps (spikes cannot burn budget).
# - the state may contain untrusted remote text, so it is NEVER logged; only
#   question names, the decision and the measured cost are.
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from observability.config import settings

log = logging.getLogger(__name__)

# Question types Jev understands.
QTYPES = ("choice", "score", "boolean", "noul")
# Tiers as a risk order (used for conservative merging downstream).
RISK_ORDER = ("SAFE", "WATCH", "RISKY", "DANGEROUS")


class JevUnavailable(Exception):
    """Raised when a Jev decision cannot be trusted (disabled, capped, error)."""


@dataclass
class JevResult:
    answers: dict[str, Any]
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0

    def choice(self, name: str) -> tuple[str, float]:
        return choice_of(self.answers.get(name))

    def prob(self, name: str) -> float:
        return prob_of(self.answers.get(name))

    def score(self, name: str) -> tuple[float, float]:
        return score_of(self.answers.get(name))


@dataclass
class JevStats:
    calls: int = 0
    fallbacks: int = 0
    denied: int = 0
    escalated: int = 0
    cost_usd: float = 0.0
    last_cost_usd: float = 0.0
    last_latency_ms: int = 0
    last_call_at: str = ""
    by_purpose: dict[str, int] = field(default_factory=dict)

    def public(self) -> dict:
        return {
            "calls": self.calls,
            "fallbacks": self.fallbacks,
            "denied": self.denied,
            "escalated": self.escalated,
            "cost_usd": round(self.cost_usd, 6),
            "last_cost_usd": round(self.last_cost_usd, 8),
            "last_latency_ms": self.last_latency_ms,
            "last_call_at": self.last_call_at,
            "by_purpose": dict(self.by_purpose),
        }


STATS = JevStats()


# ---------------------------------------------------------------------------
# Answer helpers — tolerate either the documented shape or a flat float
# ---------------------------------------------------------------------------
def choice_of(answer: object) -> tuple[str, float]:
    """('DANGEROUS', 0.97) from a choice answer; ('', 0.0) when unusable."""
    if isinstance(answer, dict):
        ch = str(answer.get("choice", "") or "").strip()
        conf = answer.get("confidence")
        if conf is None:
            probs = answer.get("probabilities") or {}
            try:
                conf = float(probs.get(ch, 0.0)) if isinstance(probs, dict) else 0.0
            except Exception:
                conf = 0.0
        try:
            return ch, max(0.0, min(1.0, float(conf)))
        except Exception:
            return ch, 0.0
    return "", 0.0


def prob_of(answer: object) -> float:
    """Probability from a boolean/noul answer (or a bare number)."""
    if isinstance(answer, dict):
        for key in ("probability", "noul", "value", "score"):
            if key in answer:
                try:
                    return max(0.0, min(1.0, float(answer[key])))
                except Exception:
                    return 0.0
        return 0.0
    try:
        return max(0.0, min(1.0, float(answer)))  # type: ignore[arg-type]
    except Exception:
        return 0.0


def score_of(answer: object) -> tuple[float, float]:
    """(score, confidence) from a score answer."""
    if isinstance(answer, dict):
        try:
            score = float(answer.get("score", 0.0))
        except Exception:
            score = 0.0
        try:
            conf = max(0.0, min(1.0, float(answer.get("confidence", 0.0))))
        except Exception:
            conf = 0.0
        return score, conf
    return 0.0, 0.0


def route(probability: float, auto: float | None = None, review: float | None = None) -> str:
    """Threshold routing: auto (act) / review (ask a human) / drop (fall back)."""
    a = settings.JEV_AUTO_THRESHOLD if auto is None else auto
    r = settings.JEV_REVIEW_THRESHOLD if review is None else review
    if probability >= a:
        return "auto"
    if probability >= r:
        return "review"
    return "drop"


def conservative_pick(a_tier: str, b_tier: str) -> str:
    """Higher-risk tier wins — the merge rule when two decision layers disagree."""
    ia = RISK_ORDER.index(a_tier) if a_tier in RISK_ORDER else 0
    ib = RISK_ORDER.index(b_tier) if b_tier in RISK_ORDER else 0
    return a_tier if ia >= ib else b_tier


def _tier_score(tier: str) -> int:
    return {"SAFE": 10, "WATCH": 40, "RISKY": 65, "DANGEROUS": 90}.get(tier, 10)


def _host_allowed(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme != "https" or not parsed.netloc:
        return False
    host = (parsed.hostname or "").lower()
    allowed = {
        h.strip().lower()
        for h in (settings.JEV_ALLOWED_HOSTS or "").split(",")
        if h.strip()
    }
    return host in allowed


class JevClient:
    """Minimal async Jev client with a per-minute and per-day cost guard."""

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        timeout: float = 0.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = (base_url or settings.JEV_BASE_URL or "").rstrip("/")
        self.api_key = api_key or settings.JEV_API_KEY or ""
        self.model = model or settings.JEV_MODEL or "typesafe-ai/jev"
        self.timeout = float(timeout or settings.JEV_TIMEOUT_SECONDS or 15)
        self._client = client
        self._call_times: list[float] = []
        self._purpose_times: dict[str, list[float]] = {}
        self._day: str = ""
        self._day_calls: int = 0
        self._open_until: float = 0.0  # circuit breaker (gateway 429 / outages)
        self._lock = asyncio.Lock()

    # --- guards ---
    def enabled(self) -> bool:
        return bool(settings.JEV_ENABLED and self.api_key and self.base_url and _host_allowed(self.base_url))

    async def _cap_check(self, purpose: str = "generic") -> None:
        """Raise JevUnavailable when a cap or the circuit breaker blocks a call."""
        async with self._lock:
            now = time.time()
            if now < self._open_until:
                raise JevUnavailable(f"circuit open for {int(self._open_until - now)}s")
            today = datetime.now(UTC).strftime("%Y-%m-%d")
            if today != self._day:
                self._day = today
                self._day_calls = 0
            if self._day_calls >= int(settings.JEV_DAILY_CALL_CAP or 0):
                raise JevUnavailable("daily call cap reached")
            self._call_times = [t for t in self._call_times if now - t < 60.0]
            if len(self._call_times) >= int(settings.JEV_MAX_CALLS_PER_MINUTE or 0):
                raise JevUnavailable("per-minute call cap reached")
            # per-purpose budget: keeps the evaluator from eating the shared
            # allowance that policy/tclk decisions need.
            cap = self._purpose_cap(purpose)
            if cap > 0:
                times = [t for t in self._purpose_times.get(purpose, []) if now - t < 60.0]
                self._purpose_times[purpose] = times
                if len(times) >= cap:
                    raise JevUnavailable(f"per-minute cap reached for purpose={purpose}")

    @staticmethod
    def _purpose_cap(purpose: str) -> int:
        if purpose == "evaluator":
            return int(settings.JEV_EVALUATOR_MAX_CALLS_PER_MINUTE or 0)
        return 0  # policy/tclk/selfcheck share the global allowance

    async def _record_call(self, purpose: str = "generic") -> None:
        async with self._lock:
            now = time.time()
            self._call_times.append(now)
            self._purpose_times.setdefault(purpose, []).append(now)
            self._day_calls += 1

    def _trip_breaker(self, seconds: float) -> None:
        """Open the circuit so a rate-limited gateway is not hammered again."""
        self._open_until = max(self._open_until, time.time() + max(1.0, seconds))

    def circuit_seconds_left(self) -> int:
        return max(0, int(self._open_until - time.time()))

    # --- api ---
    async def evaluate(
        self,
        state: object,
        questions: dict[str, dict],
        *,
        purpose: str = "generic",
        model: str = "",
    ) -> JevResult:
        """One parallel decision call. Raises JevUnavailable to fail closed."""
        if not settings.JEV_ENABLED:
            raise JevUnavailable("JEV_ENABLED is false")
        if not self.api_key:
            raise JevUnavailable("JEV_API_KEY missing")
        if not self.base_url:
            raise JevUnavailable("JEV_BASE_URL missing")
        if not _host_allowed(self.base_url):
            raise JevUnavailable(f"JEV_BASE_URL host not allowed: {self.base_url}")
        if not questions:
            raise JevUnavailable("no questions supplied")
        for name, q in questions.items():
            if not isinstance(q, dict) or q.get("type") not in QTYPES:
                raise JevUnavailable(f"question '{name}' has an unsupported type")

        await self._cap_check(purpose)

        payload = {"model": model or self.model, "state": state, "questions": questions}
        started = time.monotonic()
        try:
            client = self._client or httpx.AsyncClient(timeout=self.timeout)
            close_after = self._client is None
            try:
                resp = await client.post(
                    f"{self.base_url}/evaluate",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                )
            finally:
                if close_after:
                    await client.aclose()
            if resp.status_code == 429:
                # Gateway rate limit (x-ratelimit-limit-requests is ~30/window):
                # honour Retry-After and stop calling until then instead of
                # hammering the quota.
                retry_after = 0.0
                try:
                    retry_after = float(resp.headers.get("retry-after") or 0)
                except Exception:
                    retry_after = 0.0
                self._trip_breaker(retry_after or settings.JEV_BACKOFF_SECONDS)
                STATS.fallbacks += 1
                raise JevUnavailable(f"gateway 429 (backoff {int(retry_after or settings.JEV_BACKOFF_SECONDS)}s)")
            resp.raise_for_status()
            data = resp.json()
        except JevUnavailable:
            raise
        except Exception as exc:  # network, HTTP, JSON — all fail closed
            STATS.fallbacks += 1
            raise JevUnavailable(f"{type(exc).__name__}: {str(exc)[:120]}") from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        answers = data.get("answers") if isinstance(data, dict) else None
        if not isinstance(answers, dict) or not answers:
            STATS.fallbacks += 1
            raise JevUnavailable("empty answers")

        usage = data.get("usage") or {}
        meta = ((data.get("providerMetadata") or {}).get("gateway") or {}) if isinstance(data, dict) else {}
        cost = 0.0
        for key in ("cost", "marketCost"):
            try:
                cost = float(meta.get(key) or 0.0) or cost
            except Exception:
                continue

        await self._record_call(purpose)
        STATS.calls += 1
        STATS.cost_usd += cost
        STATS.last_cost_usd = cost
        STATS.last_latency_ms = latency_ms
        STATS.last_call_at = datetime.now(UTC).isoformat()
        STATS.by_purpose[purpose] = STATS.by_purpose.get(purpose, 0) + 1
        log.info(
            "jev decision purpose=%s model=%s answers=%d cost=%s ms=%d",
            purpose,
            data.get("model") or payload["model"],
            len(answers),
            cost,
            latency_ms,
        )
        return JevResult(
            answers=answers,
            model=str(data.get("model") or payload["model"]),
            input_tokens=int(usage.get("inputTokens") or 0),
            output_tokens=int(usage.get("outputTokens") or 0),
            cost_usd=cost,
            latency_ms=latency_ms,
        )


_default_client: JevClient | None = None


def get_client() -> JevClient:
    global _default_client
    if _default_client is None:
        _default_client = JevClient()
    return _default_client


async def evaluate(state: object, questions: dict[str, dict], *, purpose: str = "generic") -> JevResult:
    """Module-level convenience used by policy/evaluator/tclk layers."""
    return await get_client().evaluate(state, questions, purpose=purpose)


def stats() -> dict:
    return STATS.public()


def reset_default_client() -> None:
    """Test helper — drop the cached client so new settings apply."""
    global _default_client
    _default_client = None
