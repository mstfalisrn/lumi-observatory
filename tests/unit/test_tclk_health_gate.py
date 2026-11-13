"""The accept gate: never promise work the producer cannot deliver."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "packages"))

import packages.agent_core.llm as llm_mod
from apps.scheduler.agent_scorer import AgentScorer


class FakeResult:
    text = "ok"


class GoodProvider:
    name = "openai_compatible"

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, **kw):
        self.calls += 1
        return FakeResult()


class MockProvider:
    name = "mock"

    async def chat(self, messages, **kw):  # pragma: no cover - must not be called
        raise AssertionError("mock provider must not be called")


class DeadProvider:
    name = "openai_compatible"

    async def chat(self, messages, **kw):
        raise RuntimeError("401 api_key_expired")


def fresh() -> AgentScorer:
    scorer = AgentScorer.__new__(AgentScorer)
    scorer._tclk_health = None
    scorer._tclk_health_at = 0.0
    scorer._tclk_health_alert_at = 0.0
    return scorer


@pytest.mark.asyncio
async def test_healthy_producer_passes(monkeypatch) -> None:
    monkeypatch.setattr(llm_mod, "build_provider", lambda *a, **k: GoodProvider())
    assert await fresh()._tclk_producer_healthy() is True


@pytest.mark.asyncio
async def test_expired_key_blocks_the_accept(monkeypatch) -> None:
    monkeypatch.setattr(llm_mod, "build_provider", lambda *a, **k: DeadProvider())
    alerts: list[str] = []

    import connectors.agent_alert as alerts_mod

    async def fake_send(text: str, *a, **k):
        alerts.append(text)
        return True

    monkeypatch.setattr(alerts_mod, "send_telegram_text", fake_send)
    assert await fresh()._tclk_producer_healthy() is False
    assert alerts and "KABUL EDİLMİYOR" in alerts[0]


@pytest.mark.asyncio
async def test_mock_provider_is_not_a_deliverable(monkeypatch) -> None:
    monkeypatch.setattr(llm_mod, "build_provider", lambda *a, **k: MockProvider())
    assert await fresh()._tclk_producer_healthy() is False


@pytest.mark.asyncio
async def test_health_is_cached(monkeypatch) -> None:
    good = GoodProvider()
    monkeypatch.setattr(llm_mod, "build_provider", lambda *a, **k: good)
    scorer = fresh()
    assert await scorer._tclk_producer_healthy() is True
    assert await scorer._tclk_producer_healthy() is True
    assert good.calls == 1


@pytest.mark.asyncio
async def test_cache_expires(monkeypatch) -> None:
    good = GoodProvider()
    monkeypatch.setattr(llm_mod, "build_provider", lambda *a, **k: good)
    scorer = fresh()
    assert await scorer._tclk_producer_healthy() is True
    scorer._tclk_health_at = time.time() - 400
    assert await scorer._tclk_producer_healthy() is True
    assert good.calls == 2


class FlakyConnector:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.posts = 0

    async def signed_post(self, room, frame):
        self.posts += 1
        if self.posts <= self.failures:
            raise RuntimeError("TechnocoreError")
        return True


@pytest.mark.asyncio
async def test_delivery_retries_a_transient_failure(monkeypatch) -> None:
    import apps.scheduler.agent_scorer as mod

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", no_sleep)
    scorer = fresh()
    scorer._connector = FlakyConnector(failures=2)
    assert await scorer._tclk_send("room", "frame") is True
    assert scorer._connector.posts == 3


@pytest.mark.asyncio
async def test_delivery_gives_up_after_three_attempts(monkeypatch) -> None:
    import apps.scheduler.agent_scorer as mod

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", no_sleep)
    scorer = fresh()
    scorer._connector = FlakyConnector(failures=5)
    assert await scorer._tclk_send("room", "frame") is False
    assert scorer._connector.posts == 3
