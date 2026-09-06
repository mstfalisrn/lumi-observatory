# LUMI — PHASE 12 queue stream function tests (fake sync Redis)
import json

import pytest

from observability.queue import (
    DLQ_STREAM,
    STREAM,
    ack,
    claim_pending,
    ensure_stream_group,
    publish_to_dlq,
    publish_to_stream,
    read_group,
)


class FakeRedis:
    def __init__(self):
        self.calls = []
        self.xadd_result = "1-0"
        self.xautoclaim_result = ("0-0", [("1-1", {"data": "{}"})])
        self.xautoclaim_raises = False
        self.xpending_result: list | None = [{"message_id": "1-1", "time_since_delivered": 40000}]
        self.xclaim_result = [("1-1", {})]
        self.xgroup_error: Exception | None = None

    def xgroup_create(self, *a, **k):
        if self.xgroup_error:
            raise self.xgroup_error
        self.calls.append(("xgroup_create", a))

    def xadd(self, *a, **k):
        self.calls.append(("xadd", a, k))
        return self.xadd_result

    def xreadgroup(self, *a, **k):
        self.calls.append(("xreadgroup", a, k))
        return [("stream", [("1-1", {"data": "{}"})])]

    def xack(self, *a, **k):
        self.calls.append(("xack", a))

    def xautoclaim(self, *a, **k):
        self.calls.append(("xautoclaim", a, k))
        if self.xautoclaim_raises:
            raise RuntimeError("no xautoclaim")
        return self.xautoclaim_result

    def xpending_range(self, *a, **k):
        self.calls.append(("xpending_range", a, k))
        return self.xpending_result

    def xclaim(self, *a, **k):
        self.calls.append(("xclaim", a))
        return self.xclaim_result


def test_publish_to_stream():
    r = FakeRedis()
    eid = publish_to_stream(r, {"run_id": "abc"})
    assert eid == "1-0"
    assert r.calls[0][0] == "xadd"
    assert r.calls[0][1][0] == STREAM
    assert json.loads(r.calls[0][1][1]["data"]) == {"run_id": "abc"}


def test_publish_to_stream_idempotency():
    r = FakeRedis()
    publish_to_stream(r, {"x": 1}, idempotency_key="ik-1")
    assert r.calls[0][1][1]["idempotency_key"] == "ik-1"


def test_publish_to_dlq():
    r = FakeRedis()
    publish_to_dlq(r, {"run_id": "x"}, "max retry")
    assert r.calls[0][1][0] == DLQ_STREAM
    assert r.calls[0][1][1]["reason"] == "max retry"


def test_ensure_stream_group_busygroup():
    r = FakeRedis()
    r.xgroup_error = RuntimeError("BUSYGROUP Consumer Group name already exists")
    ensure_stream_group(r)  # raise etmemeli


def test_ensure_stream_group_real_error():
    r = FakeRedis()
    r.xgroup_error = RuntimeError("connection refused")
    with pytest.raises(RuntimeError):
        ensure_stream_group(r)


def test_read_group_and_ack():
    r = FakeRedis()
    res = read_group(r, "worker-1")
    assert res[0][0] == "stream"
    ack(r, "1-1")
    assert r.calls[-1][0] == "xack"


def test_claim_pending_xautoclaim():
    r = FakeRedis()
    entries = claim_pending(r, "worker-1")
    assert entries == [("1-1", {"data": "{}"})]


def test_claim_pending_fallback():
    r = FakeRedis()
    r.xautoclaim_raises = True
    entries = claim_pending(r, "worker-1")
    assert entries == [("1-1", {})]
    assert any(c[0] == "xpending_range" for c in r.calls)
    assert any(c[0] == "xclaim" for c in r.calls)


def test_claim_pending_total_failure():
    r = FakeRedis()
    r.xautoclaim_raises = True
    r.xpending_result = None  # make fallback fail too
    assert claim_pending(r, "worker-1") == []
