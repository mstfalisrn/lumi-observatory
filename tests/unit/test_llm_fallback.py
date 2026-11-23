"""The FLOP gateway spend path must never take the agent's work down with it."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "packages"))

from agent_core import llm as llm_mod
from agent_core.llm import FallbackProvider, LLMMessage, LLMResult


class Ok:
    name = "ok"
    model = "m"

    def __init__(self, text: str = "fine") -> None:
        self.text = text
        self.calls = 0

    async def chat(self, messages, tools=None, **kw):
        self.calls += 1
        return LLMResult(text=self.text, finish_reason="stop", usage={})

    async def check(self) -> bool:
        return True


class Dead:
    name = "dead"
    model = "m"

    def __init__(self, exc: Exception | None = None, text: str = "") -> None:
        self.exc = exc or RuntimeError("gateway_quota_exhausted")
        self.text = text
        self.calls = 0

    async def chat(self, messages, tools=None, **kw):
        self.calls += 1
        if self.exc:
            raise self.exc
        return LLMResult(text=self.text, finish_reason="stop", usage={})

    async def check(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_gateway_failure_falls_back() -> None:
    primary, secondary = Dead(), Ok()
    result = await FallbackProvider(primary, secondary).chat([LLMMessage("user", "hi")])
    assert result.text == "fine"
    assert primary.calls == 1 and secondary.calls == 1


@pytest.mark.asyncio
async def test_working_gateway_is_not_bypassed() -> None:
    primary, secondary = Ok("from gateway"), Ok("from fallback")
    result = await FallbackProvider(primary, secondary).chat([LLMMessage("user", "hi")])
    assert result.text == "from gateway"
    assert secondary.calls == 0


@pytest.mark.asyncio
async def test_empty_answer_falls_back() -> None:
    primary, secondary = Dead(exc=None, text="  "), Ok("from fallback")
    result = await FallbackProvider(primary, secondary).chat([LLMMessage("user", "hi")])
    assert result.text == "from fallback"
    assert secondary.calls == 1


def test_gateway_provider_is_not_built_without_a_credential(monkeypatch) -> None:
    monkeypatch.setattr(llm_mod.settings, "FLOP_GATEWAY_LLM_ENABLED", True, raising=False)
    monkeypatch.delenv("FLOP_GATEWAY_KEY", raising=False)
    monkeypatch.setenv("FLOP_GATEWAY_KEY_FILE", "/nonexistent/key")
    assert llm_mod.build_flop_gateway_provider() is None


def test_gateway_provider_is_built_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(llm_mod.settings, "FLOP_GATEWAY_LLM_ENABLED", True, raising=False)
    monkeypatch.setenv("FLOP_GATEWAY_KEY", "test-bearer")
    provider = llm_mod.build_flop_gateway_provider()
    assert provider is not None
    assert provider.name == "flop_gateway"
    assert provider.base_url == "https://api.flop.network"


@pytest.mark.asyncio
async def test_a_failed_primary_is_skipped_while_cooling_down() -> None:
    primary, secondary = Dead(), Ok()
    provider = FallbackProvider(primary, secondary, cooldown=300)
    await provider.chat([LLMMessage("user", "one")])
    await provider.chat([LLMMessage("user", "two")])
    assert primary.calls == 1
    assert secondary.calls == 2
