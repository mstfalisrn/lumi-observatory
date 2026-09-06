# LUMI — PHASE 6 queue tests (DLQ, stream publish, group)

from observability.queue import (
    DLQ_STREAM,
    GROUP,
    STREAM,
    ack,
    ensure_stream_group,
    publish_to_dlq,
    publish_to_stream,
)


class _FakeRedis:
    def __init__(self):
        self.streams = {}
        self.groups = {}
        self.xacks = []
        self._counter = 0

    def xgroup_create(self, stream, group, **kw):
        if group in self.groups.get(stream, set()):
            raise Exception("BUSYGROUP Consumer Group name already exists")
        self.groups.setdefault(stream, set()).add(group)
        return True

    def xadd(self, stream, fields, **kw):
        self._counter += 1
        eid = f"{self._counter}-0"
        self.streams.setdefault(stream, []).append((eid, fields))
        return eid

    def xack(self, stream, group, entry_id):
        self.xacks.append((stream, entry_id))
        return 1


class TestPublishToStream:
    def test_adds_data_field(self):
        r = _FakeRedis()
        publish_to_stream(r, {"run_id": "abc"}, idempotency_key="k1")
        assert r.streams[STREAM][0][1]["data"] == '{"run_id": "abc"}'
        assert r.streams[STREAM][0][1]["idempotency_key"] == "k1"

    def test_no_idempotency_key(self):
        r = _FakeRedis()
        publish_to_stream(r, {"run_id": "abc"})
        assert "idempotency_key" not in r.streams[STREAM][0][1]


class TestDLQ:
    def test_publish_to_dlq(self):
        r = _FakeRedis()
        publish_to_dlq(r, {"run_id": "abc"}, reason="max_retries_exceeded")
        entry = r.streams[DLQ_STREAM][0][1]
        assert entry["reason"] == "max_retries_exceeded"
        assert entry["data"] == '{"run_id": "abc"}'


class TestStreamGroup:
    def test_ensure_group_idempotent(self):
        r = _FakeRedis()
        ensure_stream_group(r)  # first call
        ensure_stream_group(r)  # second time BUSYGROUP is caught, does not raise
        assert GROUP in r.groups.get(STREAM, set())


class TestAck:
    def test_ack(self):
        r = _FakeRedis()
        ack(r, "123-0")
        assert ("123-0") in [x[1] for x in r.xacks]
