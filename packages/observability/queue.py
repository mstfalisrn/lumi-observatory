# LUMI — reliable queue (Redis Streams consumer group + outbox publisher)
from __future__ import annotations

import json

STREAM = "lumi:stream:run_queue"
GROUP = "lumi-workers"
CONSUMER_PREFIX = "worker-"
DLQ_STREAM = "lumi:stream:dlq"

# outbox topic
TOPIC_RUN_QUEUED = "lumi.run_queued"


def ensure_stream_group(redis_client) -> None:
    """Idempotent XGROUP CREATE MKSTREAM."""
    try:
        redis_client.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except Exception as e:
        # BUSYGROUP = already exists
        if "BUSYGROUP" not in str(e):
            raise


def publish_to_stream(redis_client, payload: dict, idempotency_key: str | None = None) -> str:
    """XADD payload to stream; returns entry id."""
    fields = {"data": json.dumps(payload, ensure_ascii=False)}
    if idempotency_key:
        fields["idempotency_key"] = idempotency_key
    # maxlen approx 10000 to avoid unbounded growth
    return redis_client.xadd(STREAM, fields, maxlen=10000, approximate=True)


def publish_to_dlq(redis_client, payload: dict, reason: str) -> str:
    """Write Poison/terminal message to DLQ stream."""
    fields = {"data": json.dumps(payload, ensure_ascii=False), "reason": reason}
    return redis_client.xadd(DLQ_STREAM, fields, maxlen=10000, approximate=True)


def read_group(redis_client, consumer_name: str, count: int = 1, block_ms: int = 5000):
    """XREADGROUP GROUP GROUP consumer_name COUNT count BLOCK block_ms STREAMS STREAM >"""
    return redis_client.xreadgroup(GROUP, consumer_name, {STREAM: ">"}, count=count, block=block_ms)


def ack(redis_client, entry_id: str) -> None:
    redis_client.xack(STREAM, GROUP, entry_id)


def claim_pending(redis_client, consumer_name: str, min_idle_ms: int = 30000, count: int = 10):
    """Reclaim pending entries that exceeded lease idle time (XAUTOCLAIM)."""
    try:
        # redis-py >=5 supports xautoclaim
        result = redis_client.xautoclaim(STREAM, GROUP, consumer_name, min_idle_ms, "0-0", count=count)
        # Redis 6/redis-py4 -> (next_id, entries) len 2
        # Redis 7/redis-py5 -> [next_id, entries, deleted_ids] len 3 — third element is deleted IDs, not entry
        if isinstance(result, (list, tuple)):
            if len(result) == 2:
                return result[1]
            if len(result) == 3:
                # entries is second element, deleted_ids is third — don't mistake it for an entry
                return result[1]
        return result or []
    except Exception:
        # fallback: XPENDING + XCLAIM path
        try:
            pending = redis_client.xpending_range(STREAM, GROUP, "-", "+", count)
            reclaimed = []
            for p in pending:
                # p may be dict or tuple
                if isinstance(p, dict):
                    pid = p.get("message_id") or p.get("entry_id")
                    idle = p.get("time_since_delivered") or p.get("idle") or 0
                else:
                    pid = p[0] if len(p) > 0 else None
                    idle = p[1] if len(p) > 1 else 0
                if pid and int(idle) >= min_idle_ms:
                    claimed = redis_client.xclaim(STREAM, GROUP, consumer_name, min_idle_ms, [pid])
                    reclaimed.extend(claimed or [])
            return reclaimed
        except Exception:
            return []
