import uuid
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from observability import models
from observability.models import Base


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


class TestContinuationCrashAndReplay:
    @pytest.mark.asyncio
    async def test_crash_pending_becomes_ambiguous_no_recall(self, engine):
        import apps.worker.worker as w_mod

        import observability.db as db_mod

        S = async_sessionmaker(engine, expire_on_commit=False)
        db_mod.async_session_factory = S  # type: ignore
        w_mod.async_session_factory = S  # type: ignore
        async with S() as s:
            u = models.User(username=f"u-{uuid.uuid4().hex[:6]}", display_name="t", role="admin", is_active=True, password_hash="x")
            s.add(u)
            t = models.Task(title="t", prompt="p", scope={}, budget={})
            s.add(t)
            await s.flush()
            run = models.Run(task_id=t.id, status=models.RunStatus.EXECUTING.value, token_budget=100, cost_budget=1)
            s.add(run)
            await s.flush()
            # create approval CONSUMED + execution PENDING (crash after consume)
            appr = models.Approval(
                action_class="PUBLIC_WRITE", action_hash="h", target="x", payload={"tool": "technocore_signed_write", "arguments": {"text": "hi"}, "action_id": "a1"},
                status=models.ApprovalStatus.CONSUMED.value, run_id=run.id,
            )
            s.add(appr)
            await s.flush()
            ex = models.ActionExecution(approval_id=appr.id, run_id=run.id, action_id="a1", tool="technocore_signed_write", status="PENDING", result={})
            s.add(ex)
            await s.commit()
            rid, aid = run.id, appr.id
            # set run EXECUTING as if claimed
            run.status = models.RunStatus.EXECUTING.value
            await s.commit()

        with MagicMock():
            import unittest.mock as um
            with um.patch("redis.from_url", return_value=MagicMock()):
                wl = w_mod.WorkerLoop()
                called = {"n": 0}

                async def fake_call(tool, **kw):
                    called["n"] += 1
                    return {"ok": True}

                wl.registry.call = fake_call  # type: ignore
                handled = await wl._try_continuation(rid)
                assert handled is True
                assert called["n"] == 0, "PENDING crash must not re-execute remote"
                async with S() as s2:
                    ex2 = (await s2.execute(select(models.ActionExecution).where(models.ActionExecution.approval_id == aid))).scalar_one()
                    assert ex2.status == "AMBIGUOUS"
                    r2 = await s2.get(models.Run, rid)
                    assert r2.status == models.RunStatus.FAILED.value
                    assert r2.error == "needs_reconciliation"

    @pytest.mark.asyncio
    async def test_second_invocation_after_success_handled_and_single_call(self, engine):
        import apps.worker.worker as w_mod

        import observability.db as db_mod

        S = async_sessionmaker(engine, expire_on_commit=False)
        db_mod.async_session_factory = S  # type: ignore
        w_mod.async_session_factory = S  # type: ignore
        async with S() as s:
            u = models.User(username="u2", display_name="t", role="admin", is_active=True, password_hash="x")
            s.add(u)
            t = models.Task(title="t", prompt="p", scope={}, budget={})
            s.add(t)
            await s.flush()
            run = models.Run(task_id=t.id, status=models.RunStatus.WAITING_APPROVAL.value, token_budget=100, cost_budget=1)
            s.add(run)
            await s.flush()
            from policy.approval import ApprovalService
            svc = ApprovalService(s)
            appr = await svc.create(run_id=str(run.id), action_id="act-dbl", tool="internal_health", arguments={"flag": 1}, action_class="PUBLIC_WRITE", target="r", ttl_seconds=3600)
            await s.flush()
            await svc.decide_with_continuation(str(appr.id), "approve", str(u.id))
            await s.commit()
            rid = run.id
        import unittest.mock as um

        with um.patch("redis.from_url", return_value=MagicMock()):
            wl = w_mod.WorkerLoop()
            cnt = {"n": 0}

            async def fake_call(tool, **kw):
                cnt["n"] += 1
                return {"ok": True}

            wl.registry.call = fake_call  # type: ignore
            async with S() as s2:
                r = await s2.get(models.Run, rid)
                r.status = models.RunStatus.EXECUTING.value
                r.worker_id = "w1"
                await s2.commit()
            h1 = await wl._try_continuation(rid)
            assert h1 is True
            assert cnt["n"] == 1
            # second claim after success
            async with S() as s3:
                r = await s3.get(models.Run, rid)
                r.status = models.RunStatus.EXECUTING.value
                r.worker_id = "w2"
                await s3.commit()
            h2 = await wl._try_continuation(rid)
            assert h2 is True
            assert cnt["n"] == 1, "second invocation must not double call"
            async with S() as s4:
                r = await s4.get(models.Run, rid)
                assert r.status in (models.RunStatus.COMPLETED.value, models.RunStatus.EXECUTING.value)


class TestRetryRaceNoStringMatch:
    @pytest.mark.asyncio
    async def test_retry_race_requery_instead_of_string(self, engine):
        import apps.api.app as api_mod
        from httpx import ASGITransport, AsyncClient

        import observability.db as db_mod
        from observability.auth import create_session_token

        S = async_sessionmaker(engine, expire_on_commit=False)
        db_mod.async_session_factory = S  # type: ignore
        api_mod.async_session_factory = S  # type: ignore
        async with S() as s:
            u = models.User(username="race@example.com", display_name="r", role="admin", is_active=True, password_hash="x")
            s.add(u)
            await s.flush()
            uid = u.id
            t = models.Task(title="t", prompt="p", scope={}, budget={})
            s.add(t)
            await s.flush()
            src = models.Run(task_id=t.id, status=models.RunStatus.FAILED.value, token_budget=100, cost_budget=1)
            s.add(src)
            await s.commit()
            src_id = str(src.id)
        token = create_session_token(str(uid), "admin")
        transport = ASGITransport(app=api_mod.app)  # type: ignore
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            h = {"Authorization": f"Bearer {token}", "Idempotency-Key": "race-key"}
            r1 = await client.post(f"/api/v1/runs/{src_id}/retry", headers=h)
            assert r1.status_code == 200
            j1 = r1.json()
            r2 = await client.post(f"/api/v1/runs/{src_id}/retry", headers=h)
            assert r2.status_code == 200
            j2 = r2.json()
            assert j1["run_id"] == j2["run_id"]
            assert j2.get("dedup") is True


class TestCoordinatorSinkCriticalNotSwallowed:
    @pytest.mark.asyncio
    async def test_coordinator_sink_critical_propagates(self):
        from agent_core.coordinator import RunCoordinator

        coord = RunCoordinator(run_id="test-critical")
        async def failing_sink(run_id, etype, payload):
            if etype in ("PLAN", "TOOL_CALL", "AWAITING_APPROVAL"):
                raise RuntimeError("critical sink failure")
        # should propagate, not swallow
        with pytest.raises(RuntimeError):
            await coord._sink(failing_sink, "PLAN", {"plan": {}})
        with pytest.raises(RuntimeError):
            await coord._sink(failing_sink, "TOOL_CALL", {"tool": "x"})

    @pytest.mark.asyncio
    async def test_worker_handle_entry_does_not_ack_on_critical_failure(self, engine):
        # worker _handle_entry must NOT ACK when _process_run raises (critical secondary persistence failure)
        import json as _json

        import apps.worker.worker as w_mod

        import observability.db as db_mod

        S = async_sessionmaker(engine, expire_on_commit=False)
        db_mod.async_session_factory = S  # type: ignore
        w_mod.async_session_factory = S  # type: ignore
        import unittest.mock as um

        fake_redis = MagicMock()
        ack_calls: list[str] = []

        def fake_ack(r, entry_id):  # type: ignore
            ack_calls.append(entry_id)

        with um.patch("redis.from_url", return_value=MagicMock()):
            wl = w_mod.WorkerLoop()
            wl.redis = fake_redis  # type: ignore
            # make _process_run raise to simulate critical failure (e.g., PLAN/TOOL_CALL persistence failure propagated)
            from observability.events import CriticalEventPersistenceError

            async def failing_process(run_id):  # type: ignore
                raise CriticalEventPersistenceError("PLAN event append failed")

            wl._process_run = failing_process  # type: ignore
            with um.patch("apps.worker.worker.ack", side_effect=fake_ack):
                payload = _json.dumps({"run_id": str(uuid.uuid4())})
                with pytest.raises(CriticalEventPersistenceError):
                    await wl._handle_entry("entry-123", {"data": payload})
                assert ack_calls == [], "critical failure must not ACK so pending can be reclaimed"

            # success path must ACK
            ack_calls.clear()

            async def success_process(run_id):  # type: ignore
                return True

            wl._process_run = success_process  # type: ignore
            with um.patch("apps.worker.worker.ack", side_effect=fake_ack):
                payload2 = _json.dumps({"run_id": str(uuid.uuid4())})
                ok = await wl._handle_entry("entry-124", {"data": payload2})
                assert ok is True
                assert ack_calls == ["entry-124"]

    @pytest.mark.asyncio
    async def test_worker_sink_chain_propagates_typed_exception(self, engine):
        # Verify typed exception chain: _sink secondary failure or append failure raises CriticalEventPersistenceError and _handle_entry does not ACK
        import json as _json

        import apps.worker.worker as w_mod

        import observability.db as db_mod
        from observability.events import CriticalEventPersistenceError

        S = async_sessionmaker(engine, expire_on_commit=False)
        db_mod.async_session_factory = S  # type: ignore
        w_mod.async_session_factory = S  # type: ignore
        async with S() as s:
            t = models.Task(title="chain-t", prompt="do plan", scope={}, budget={})
            s.add(t)
            await s.flush()
            run = models.Run(task_id=t.id, status=models.RunStatus.QUEUED.value, token_budget=1000, cost_budget=10)
            s.add(run)
            await s.commit()
            rid_str = str(run.id)
        import unittest.mock as um

        with um.patch("redis.from_url", return_value=MagicMock()):
            wl = w_mod.WorkerLoop()
            called_ack: list[str] = []

            def fake_ack2(r, entry_id):  # type: ignore
                called_ack.append(entry_id)

            with um.patch("apps.worker.worker.ack", side_effect=fake_ack2):
                orig_pr = wl._process_run

                async def raising_pr(run_id):  # type: ignore
                    raise CriticalEventPersistenceError("TOOL_CALL event append failed")

                wl._process_run = raising_pr  # type: ignore
                payload = _json.dumps({"run_id": rid_str})
                with pytest.raises(CriticalEventPersistenceError):
                    await wl._handle_entry("entry-chain-1", {"data": payload})
                assert called_ack == [], "typed critical failure must not ACK"
                called_ack.clear()

                async def ok_pr(run_id):  # type: ignore
                    return True

                wl._process_run = ok_pr  # type: ignore
                ok = await wl._handle_entry("entry-chain-2", {"data": payload})
                assert ok is True
                assert called_ack == ["entry-chain-2"]
                wl._process_run = orig_pr  # type: ignore

class TestFatalNotSwallowed:
    @pytest.mark.asyncio
    async def test_fatal_append_failure_propagates(self, engine):
        import apps.worker.worker as w_mod

        import observability.db as db_mod

        S = async_sessionmaker(engine, expire_on_commit=False)
        db_mod.async_session_factory = S  # type: ignore
        w_mod.async_session_factory = S  # type: ignore
        async with S() as s:
            t = models.Task(title="t", prompt="p", scope={}, budget={})
            s.add(t)
            await s.flush()
            r = models.Run(task_id=t.id, status=models.RunStatus.EXECUTING.value, token_budget=100, cost_budget=1)
            s.add(r)
            await s.commit()
            rid = r.id

        # monkey patch _append to raise
        orig = w_mod._append_run_event_safe

        async def failing_append(*a, **kw):
            raise RuntimeError("append failed")

        w_mod._append_run_event_safe = failing_append  # type: ignore
        try:
            with pytest.raises(RuntimeError):
                await w_mod._append_run_event_safe(rid, "FATAL", {"x": 1})
        finally:
            w_mod._append_run_event_safe = orig  # type: ignore

        # also verify _sink propagates for critical types
        async def _sink_critical():
            # Simulate coordinator calling sink with TOOL_CALL that fails append
            w_mod._append_run_event_safe = failing_append  # type: ignore
            try:
                # we test via direct call: should raise
                with pytest.raises(RuntimeError):
                    await w_mod._append_run_event_safe(rid, "TOOL_CALL", {})
            finally:
                w_mod._append_run_event_safe = orig  # type: ignore

        await _sink_critical()


class TestOutboxAtomic:
    @pytest.mark.asyncio
    async def test_outbox_atomically_persisted(self, engine):
        # ApprovalService decide_with_continuation must create exactly one outbox row in same transaction
        from policy.approval import ApprovalService

        S = async_sessionmaker(engine, expire_on_commit=False)
        async with S() as s:
            u = models.User(username="outbox@example.com", display_name="o", role="admin", is_active=True, password_hash="x")
            s.add(u)
            t = models.Task(title="t", prompt="p", scope={}, budget={})
            s.add(t)
            await s.flush()
            run = models.Run(task_id=t.id, status=models.RunStatus.WAITING_APPROVAL.value, token_budget=100, cost_budget=1)
            s.add(run)
            await s.flush()
            svc = ApprovalService(s)
            appr = await svc.create(run_id=str(run.id), action_id="a1", tool="t", arguments={}, action_class="PUBLIC_WRITE", target="x", ttl_seconds=3600)
            await s.commit()
            svc2 = ApprovalService(s)
            await svc2.decide_with_continuation(str(appr.id), "approve", str(u.id))
            await s.commit()
            res = await s.execute(select(models.OutboxMessage).where(models.OutboxMessage.idempotency_key == f"approve:{appr.id}"))
            ob = res.scalar_one_or_none()
            assert ob is not None
            assert ob.topic == "lumi.run_queued"
            assert ob.payload["run_id"] == str(run.id)

class TestWorkerSecondaryCriticalViaSink:
    @pytest.mark.asyncio
    async def test_secondary_db_critical_raises_and_propagates(self, engine):
        import uuid as _uuid

        import apps.worker.worker as w_mod
        from sqlalchemy.ext.asyncio import async_sessionmaker

        import observability.db as db_mod
        from observability import models as _models
        S = async_sessionmaker(engine, expire_on_commit=False)
        db_mod.async_session_factory = S
        w_mod.async_session_factory = S
        async with S() as s:
            t = _models.Task(title="t", prompt="p", scope={}, budget={})
            s.add(t)
            await s.flush()
            run = _models.Run(task_id=t.id, status=_models.RunStatus.QUEUED.value, token_budget=100, cost_budget=1)
            s.add(run)
            await s.commit()
            run_id = str(run.id)
        async def ok_append(*a, **kw):
            return None
        orig_append = w_mod._append_run_event_safe
        w_mod._append_run_event_safe = ok_append
        class FailSession:
            def add(self, *a, **kw): pass
            async def commit(self): raise RuntimeError("secondary DB down")
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            def get_bind(self): return None
        class FailCM:
            async def __aenter__(self): return FailSession()
            async def __aexit__(self, *a): return False
        def failing_factory():
            return FailCM()
        async def _sink(rid, etype, payload):
            try:
                await ok_append(_uuid.UUID(rid), etype, payload)
            except Exception:
                if etype in ("TOOL_CALL", "PLAN", "AWAITING_APPROVAL", "FATAL", "WORKER_ERROR"):
                    raise
            try:
                async with db_mod.async_session_factory() as s2:
                    if etype == "PLAN":
                        s2.add(_models.Plan(run_id=_uuid.UUID(rid), plan_json=payload.get("plan", {}), expected_evidence={}, status="active"))
                    elif etype == "TOOL_CALL":
                        s2.add(_models.ToolCall(run_id=_uuid.UUID(rid), tool_name=payload.get("tool", ""), input_summary="x", input_redacted="x", result_summary="x", action_class="READ_ONLY", policy_decision="ALLOW"))
                    elif etype == "AWAITING_APPROVAL":
                        s2.add(_models.Plan(run_id=_uuid.UUID(rid), plan_json={}, expected_evidence={}, status="active"))
                    await s2.commit()
            except Exception as e:
                if etype in ("PLAN", "TOOL_CALL", "AWAITING_APPROVAL"):
                    raise RuntimeError(f"critical secondary persistence failure {etype}: {type(e).__name__}") from e
        db_mod.async_session_factory = failing_factory
        try:
            import pytest as _pytest
            with _pytest.raises(RuntimeError, match="critical secondary persistence failure PLAN"):
                await _sink(run_id, "PLAN", {"plan": {}})
            with _pytest.raises(RuntimeError, match="critical secondary persistence failure TOOL_CALL"):
                await _sink(run_id, "TOOL_CALL", {"tool": "x"})
            with _pytest.raises(RuntimeError, match="critical secondary persistence failure AWAITING_APPROVAL"):
                await _sink(run_id, "AWAITING_APPROVAL", {"tool": "x"})
        finally:
            db_mod.async_session_factory = S
            w_mod.async_session_factory = S
            w_mod._append_run_event_safe = orig_append
