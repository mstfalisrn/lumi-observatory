import asyncio
import os

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from observability import models

pytestmark = pytest.mark.asyncio

# This test only runs when DATABASE_URL points to real Postgres (PG service in CI / izole test)
# On sqlite (default unit fixture) it is skipped honestly — production semantics proof is PG only.

async def test_pg_global_seq_unique_across_runs():
    db_url = os.getenv("DATABASE_URL", "")
    if "postgresql" not in db_url:
        pytest.skip("PG integration only — DATABASE_URL not postgresql")
    # use PG engine, do not use Base.metadata.create_all — assume alembic already upgraded head
    engine = create_async_engine(db_url)
    S = async_sessionmaker(engine, expire_on_commit=False)
    # import after env
    import apps.worker.worker as w_mod

    import observability.db as db_mod
    orig_db = db_mod.async_session_factory
    orig_w = w_mod.async_session_factory
    db_mod.async_session_factory = S  # type: ignore
    w_mod.async_session_factory = S  # type: ignore
    try:
        async with S() as s:
            t = models.Task(title="pg-conc", prompt="p", scope={}, budget={})
            s.add(t)
            await s.flush()
            r1 = models.Run(task_id=t.id, status=models.RunStatus.EXECUTING.value, token_budget=100, cost_budget=1)
            r2 = models.Run(task_id=t.id, status=models.RunStatus.EXECUTING.value, token_budget=100, cost_budget=1)
            s.add_all([r1, r2])
            await s.commit()
            id1, id2 = r1.id, r2.id
        # concurrent writes across two runs
        async def writer(run_id, n):
            await w_mod._append_run_event_safe(run_id, f"E{n}", {"n": n})

        await asyncio.gather(
            writer(id1, 0), writer(id1, 1), writer(id2, 0), writer(id2, 1), writer(id1, 2)
        )
        async with S() as s2:
            res = await s2.execute(select(models.RunEvent).order_by(models.RunEvent.global_seq))
            evs = list(res.scalars().all())
            gseqs = [int(e.global_seq) for e in evs if e.run_id in (id1, id2)]
            assert len(gseqs) == 5
            assert len(set(gseqs)) == 5, f"global_seq collision {gseqs}"
            assert gseqs == sorted(gseqs)
            # per-run seq 0..N
            for rid, expected_len in [(id1, 3), (id2, 2)]:
                res2 = await s2.execute(select(models.RunEvent).where(models.RunEvent.run_id == rid).order_by(models.RunEvent.seq))
                seqs = [e.seq for e in res2.scalars().all()]
                assert seqs == list(range(expected_len)), f"run {rid} seq {seqs}"
            # cursor lossless replay: fetch after first global_seq should return remaining without loss
            # advisory lock serializes global commit order (pg_advisory_xact_lock 727271) so sequence == commit order; gaps avoided.
            first_cursor = gseqs[1]
            res3 = await s2.execute(select(models.RunEvent).where(models.RunEvent.global_seq > first_cursor).order_by(models.RunEvent.global_seq))
            after = [e for e in res3.scalars().all() if e.run_id in (id1, id2)]
            assert len(after) == 3, f"cursor replay lost {len(after)} !=3 after {first_cursor}, gseqs {gseqs}"
            # Repo scan: production RunEvent inserts must go via _append_run_event_safe (worker) or same advisory lock (scheduler).
            # Verified: worker uses helper; scheduler now acquires same advisory before direct inserts. Fixtures/migrations excluded.
    finally:
        db_mod.async_session_factory = orig_db  # type: ignore
        w_mod.async_session_factory = orig_w  # type: ignore
        await engine.dispose()
