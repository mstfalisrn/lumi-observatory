"""LLM token ledger — every model call leaves a countable trace.

The dashboard has to answer "how many tokens did the agent burn, and on what",
so the transport layer (agent_core.llm) reports each call here. Writes happen on
a background thread against Postgres: a slow or down database must never delay
or break a model call, and a lost ledger row is always preferable to a lost
delivery. Rows carry service + purpose so the burn can be attributed.

Self-contained by design: the table is created on first write, so the ledger
works in every image (scheduler, earn, logs) without a migration step.
"""

from __future__ import annotations

import atexit
import logging
import os
import queue
import threading
import time

log = logging.getLogger(__name__)

TABLE = "llm_usage"

_DDL = (
    f"create table if not exists {TABLE} ("
    "id bigserial primary key,"
    "ts timestamptz not null default now(),"
    "service text,"
    "purpose text,"
    "provider text,"
    "model text,"
    "prompt_tokens bigint not null default 0,"
    "completion_tokens bigint not null default 0,"
    "total_tokens bigint not null default 0,"
    "latency_ms integer not null default 0)"
)
_INDEX = f"create index if not exists {TABLE}_ts_idx on {TABLE} (ts desc)"

_INSERT = (
    f"insert into {TABLE} "
    "(service, purpose, provider, model, prompt_tokens, completion_tokens,"
    " total_tokens, latency_ms) values (%s, %s, %s, %s, %s, %s, %s, %s)"
)

_QUEUE: queue.Queue = queue.Queue(maxsize=10000)
_LOCK = threading.Lock()
_STARTED = False
_DISABLED = False
_REPORTED_ERROR = False


def dsn() -> str:
    """Postgres DSN for the ledger. Empty string means 'ledger off'."""
    url = os.environ.get("LOGS_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
    user = os.environ.get("POSTGRES_USER", "lumi")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_HOST", "")
    # Host-side services (lumi-earn, lumi-kibble) point POSTGRES_HOST at loopback
    # because the compose network's DNS names don't resolve outside Docker; that
    # explicit override has to win over the container-oriented DATABASE_URL.
    if host and password:
        from urllib.parse import quote

        port = os.environ.get("POSTGRES_PORT", "5432")
        name = os.environ.get("POSTGRES_DB", "lumi")
        return f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}/{name}"
    if url:
        return (
            url.replace("postgresql+asyncpg://", "postgresql://")
            .replace("postgresql+psycopg://", "postgresql://")
        )
    if password:
        name = os.environ.get("POSTGRES_DB", "lumi")
        return f"postgresql://{user}:{password}@lumi-postgres:5432/{name}"
    return ""


def service_name() -> str:
    """Which process spent the tokens (set per unit/container; best effort)."""
    return (os.environ.get("LUMI_SERVICE") or os.environ.get("SERVICE_NAME") or "").strip() or "lumi"


def tokens(usage: dict | None) -> tuple[int, int, int]:
    """(prompt, completion, total) from a provider usage block, 0 when absent."""
    usage = usage or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or 0) or (prompt + completion)
    return prompt, completion, total


def record(
    *,
    provider: str = "",
    model: str = "",
    usage: dict | None = None,
    purpose: str = "",
    latency_ms: int = 0,
    service: str = "",
) -> None:
    """Queue one call for the ledger. Never raises, never blocks the caller."""
    if _DISABLED:
        return
    prompt, completion, total = tokens(usage)
    if not total:
        # No usage block (mock provider, cached answer): nothing to count.
        return
    _ensure_worker()
    row = (
        (service or service_name())[:80],
        (purpose or "")[:80],
        (provider or "")[:40],
        (model or "")[:80],
        prompt,
        completion,
        total,
        int(max(latency_ms, 0)),
    )
    try:
        _QUEUE.put_nowait(row)
    except queue.Full:  # keep serving traffic; drop the count, not the call
        pass


def _ensure_worker() -> None:
    global _STARTED
    if _STARTED:
        return
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True
        threading.Thread(target=_worker, name="llm-usage-writer", daemon=True).start()
        atexit.register(_drain_on_exit)


def _worker() -> None:
    while True:
        try:
            first = _QUEUE.get(timeout=5.0)
        except queue.Empty:
            continue
        batch = [first]
        while len(batch) < 200:
            try:
                batch.append(_QUEUE.get_nowait())
            except queue.Empty:
                break
        _flush(batch)


def _drain_on_exit() -> None:
    batch = []
    while not _QUEUE.empty() and len(batch) < 2000:
        try:
            batch.append(_QUEUE.get_nowait())
        except queue.Empty:
            break
    if batch:
        _flush(batch)


def _flush(rows: list) -> None:
    global _REPORTED_ERROR
    url = dsn()
    if not url:
        return
    try:
        import psycopg
    except ImportError:
        if not _REPORTED_ERROR:
            log.info("llm usage ledger off: psycopg not installed")
            _REPORTED_ERROR = True
        return
    try:
        with psycopg.connect(url, connect_timeout=8, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(_DDL)
                cur.execute(_INDEX)
                cur.executemany(_INSERT, rows)
    except Exception as e:  # noqa: BLE001 — accounting must never break a run
        if not _REPORTED_ERROR:
            log.warning("llm usage ledger write failed: %s", type(e).__name__)
            _REPORTED_ERROR = True


def probe() -> dict:
    """Cheap self-test used by ops: is the ledger writable right now?"""
    url = dsn()
    if not url:
        return {"ok": False, "reason": "no dsn"}
    try:
        import psycopg

        with psycopg.connect(url, connect_timeout=8, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(_DDL)
                cur.execute(_INDEX)
                cur.execute(f"select count(*), coalesce(sum(total_tokens), 0) from {TABLE}")
                count, total = cur.fetchone()
        return {"ok": True, "rows": int(count), "total_tokens": int(total)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": type(e).__name__}


def record_now(**kw) -> None:
    """Synchronous variant (tests, one-off scripts); safe to call directly."""
    prompt, completion, total = tokens(kw.get("usage") or {})
    if not total:
        return
    _flush(
        [
            (
                (kw.get("service") or service_name())[:80],
                (kw.get("purpose") or "")[:80],
                (kw.get("provider") or "")[:40],
                (kw.get("model") or "")[:80],
                prompt,
                completion,
                total,
                int(max(int(kw.get("latency_ms") or 0), 0)),
            )
        ]
    )


__all__ = ["TABLE", "dsn", "probe", "record", "record_now", "service_name", "tokens", "time"]
