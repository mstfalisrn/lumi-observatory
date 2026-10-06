"""Regression tests for worker and scheduler FastAPI lifespans."""

import asyncio

import apps.api.app as api
import apps.scheduler.scheduler as scheduler
import apps.worker.worker as worker
import pytest


class _WaitingWorker:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def run(self) -> None:
        self.started.set()
        await asyncio.Event().wait()


class _WaitingScheduler:
    def __init__(self, *args, **kwargs) -> None:
        self.started = asyncio.Event()

    async def run(self) -> None:
        self.started.set()
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_worker_lifespan_cancels_tasks_disposes_engine_and_restarts(monkeypatch):
    workers: list[_WaitingWorker] = []
    dispose_calls: list[None] = []

    def build_worker() -> _WaitingWorker:
        instance = _WaitingWorker()
        workers.append(instance)
        return instance

    async def dispose() -> None:
        dispose_calls.append(None)

    monkeypatch.setattr(worker, "WorkerLoop", build_worker)
    monkeypatch.setattr(worker, "dispose_engine", dispose)

    for _ in range(2):
        async with worker.app.router.lifespan_context(worker.app):
            task = worker._BG_TASKS[0]
            await asyncio.sleep(0)
            assert workers[-1].started.is_set()
            assert not task.done()
        assert task.cancelled()
        assert worker._BG_TASKS == []
        assert worker._worker is None

    assert len(workers) == 2
    assert len(dispose_calls) == 2


@pytest.mark.asyncio
async def test_scheduler_lifespan_cancels_tasks_disposes_engine_and_restarts(monkeypatch):
    schedulers: list[_WaitingScheduler] = []
    dispose_calls: list[None] = []

    def build_scheduler(*args, **kwargs) -> _WaitingScheduler:
        instance = _WaitingScheduler()
        schedulers.append(instance)
        return instance

    async def dispose() -> None:
        dispose_calls.append(None)

    monkeypatch.setattr(scheduler, "SchedulerLoop", build_scheduler)
    monkeypatch.setattr(scheduler, "AgentScorer", None)
    monkeypatch.setattr(scheduler, "dispose_engine", dispose)

    for _ in range(2):
        async with scheduler.app.router.lifespan_context(scheduler.app):
            task = scheduler._BG_TASKS[0]
            await asyncio.sleep(0)
            assert schedulers[-1].started.is_set()
            assert not task.done()
        assert task.cancelled()
        assert scheduler._BG_TASKS == []
        assert scheduler._AGENT_SCORER_TASK == []

    assert len(schedulers) == 2
    assert len(dispose_calls) == 2


@pytest.mark.asyncio
async def test_api_lifespan_disposes_engine(monkeypatch):
    dispose_calls: list[None] = []

    async def dispose() -> None:
        dispose_calls.append(None)

    monkeypatch.setattr(type(api.settings), "validate_production", lambda *_a, **_k: None)
    monkeypatch.setattr(api.settings, "ADMIN_PASSWORD_HASH", "")
    monkeypatch.setattr(api.settings, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(api, "dispose_engine", dispose)

    async with api.app.router.lifespan_context(api.app):
        pass

    assert dispose_calls == [None]
