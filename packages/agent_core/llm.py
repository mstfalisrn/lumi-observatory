# LUMI — LLM provider interface + OpenAI-compatible + mock
# Provider independent; base_url/model/api_key selected via env. Does not touch host env.
from __future__ import annotations

import abc
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from observability.config import settings

log = logging.getLogger(__name__)

_DEFAULT_LLM_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _llm_headers(api_key: str) -> dict[str, str]:
    """Common headers incl. browser-like UA and optional x-opencode-session.

    Cloudflare-fronted providers (e.g. OpenCode Zen Go) return 403 for the
    default python-httpx UA and 400 MissingSessionID without a session header.
    """
    headers = {"User-Agent": settings.LLM_USER_AGENT or _DEFAULT_LLM_USER_AGENT}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if settings.LLM_SESSION_ID:
        headers["x-opencode-session"] = settings.LLM_SESSION_ID
    return headers


def _report_usage(model: str, usage: dict, purpose: str, elapsed_s: float) -> None:
    """Count one call in the token ledger.

    Accounting is strictly secondary: if the ledger is unavailable the model call
    has still succeeded, so failures here are swallowed on purpose.
    """
    try:
        try:  # container layout: packages/ merged into the image root
            from observability import llm_usage
        except ImportError:  # host layout: repo root on sys.path (earn/kibble)
            from packages.observability import llm_usage

        llm_usage.record(
            provider="openai_compatible",
            model=model,
            usage=usage,
            purpose=purpose,
            latency_ms=int(elapsed_s * 1000),
        )
    except Exception:
        pass


@dataclass
class LLMMessage:
    role: str  # system | user | assistant | tool
    content: str
    name: str | None = None
    tool_call_id: str | None = None


@dataclass
class LLMToolCall:
    name: str
    arguments: dict
    id: str | None = None


@dataclass
class LLMResult:
    text: str = ""
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    finish_reason: str = ""
    usage: dict = field(default_factory=dict)
    # NO unredacted hidden thoughts — returns auditable metadata


class LLMProvider(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    async def chat(self, messages: list[LLMMessage], tools: list[dict] | None = None, **kw) -> LLMResult: ...

    @abc.abstractmethod
    async def check(self) -> bool:
        """Is provider reachable (health)."""


class MockProvider(LLMProvider):
    """Test/dev default provider. Does not make real calls; produces a deterministic plan."""

    name = "mock"

    async def chat(self, messages, tools=None, **kw) -> LLMResult:
        return LLMResult(
            text="[mock] Plan: query, build context, then report.",
            finish_reason="stop",
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        )

    async def check(self) -> bool:
        return True


class OpenAICompatibleProvider(LLMProvider):
    """OpenAI-compatible Chat / Responses style endpoint."""

    name = "openai_compatible"

    def __init__(self, base_url: str, model: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self._client = httpx.AsyncClient(timeout=60.0)

    async def chat(self, messages, tools=None, **kw) -> LLMResult:
        url = f"{self.base_url}/chat/completions"
        # Bookkeeping kwarg for the token ledger: must never reach the API body.
        purpose = str(kw.pop("purpose", "") or "")
        payload: dict = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            **kw,
        }
        # Reasoning depth for reasoning-capable models. Caller-supplied kw wins,
        # so an explicit per-call value can still override the configured default.
        effort = (getattr(settings, "REASONING_EFFORT", "") or "").strip().lower()
        if effort and "reasoning_effort" not in payload:
            payload["reasoning_effort"] = effort
        if tools is not None:
            payload["tools"] = [{"type": "function", "function": t} for t in tools]
        headers = _llm_headers(self.api_key)
        _started = time.perf_counter()
        resp = await self._client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        msg = data["choices"][0]["message"]
        tool_calls = [
            LLMToolCall(
                name=tc["function"]["name"],
                arguments=json.loads(tc["function"].get("arguments") or "{}"),
                id=tc.get("id"),
            )
            for tc in (msg.get("tool_calls") or [])
        ]
        result = LLMResult(
            text=msg.get("content") or "",
            tool_calls=tool_calls,
            finish_reason=data["choices"][0].get("finish_reason", ""),
            usage=data.get("usage", {}),
        )
        _report_usage(self.model, result.usage, purpose, time.perf_counter() - _started)
        return result

    async def check(self) -> bool:
        try:
            await self.chat([LLMMessage("user", "ping")])
            return True
        except Exception:
            return False


def build_provider(provider: str | None = None) -> LLMProvider:
    p = (provider or settings.LLM_PROVIDER or "mock").lower()
    if p == "openai_compatible" or p == "openai":
        base: LLMProvider = OpenAICompatibleProvider(settings.LLM_BASE_URL, settings.LLM_MODEL, settings.LLM_API_KEY)
    else:
        base = MockProvider()
    flop = build_flop_gateway_provider()
    return FallbackProvider(flop, base) if flop is not None else base


# ---------------------------------------------------------------------------
# Embedding (memory semantic retrieval)
# ---------------------------------------------------------------------------
class EmbeddingProvider(abc.ABC):
    @abc.abstractmethod
    async def embed(self, text: str) -> list[float]: ...


class OpenAICompatibleEmbeddingProvider(EmbeddingProvider):
    def __init__(self, base_url: str, model: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key

    async def embed(self, text: str) -> list[float]:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{self.base_url}/embeddings",
                json={"model": self.model, "input": text[:8000]},
                headers=_llm_headers(self.api_key),
            )
            r.raise_for_status()
            data = r.json()
            vec = data["data"][0]["embedding"]
            if not isinstance(vec, list) or not all(isinstance(x, (int, float)) for x in vec):
                raise ValueError("embedding response is not a list")
            return [float(x) for x in vec]


def build_embedding_provider() -> EmbeddingProvider | None:
    """Embedding provider — model embeddings desteklemiyorsa None (graceful)."""
    model = settings.EMBEDDING_MODEL or settings.LLM_MODEL
    if not model or not settings.LLM_BASE_URL:
        return None
    try:
        return OpenAICompatibleEmbeddingProvider(settings.LLM_BASE_URL, model, settings.LLM_API_KEY)
    except Exception:
        return None


class FallbackProvider(LLMProvider):
    """Prefer the first provider, keep working when it refuses.

    A testnet allowance that is exhausted, expired or rate-limited must not take the
    agent's work down with it: the fallback answers and the failure is logged by class
    only, so a broken credential cannot leak into a delivered task.
    """

    name = "fallback"

    def __init__(self, primary: LLMProvider, secondary: LLMProvider) -> None:
        self.primary = primary
        self.secondary = secondary
        self.model = getattr(primary, "model", "")

    async def chat(self, messages, tools=None, **kw) -> LLMResult:
        try:
            result = await self.primary.chat(messages, tools=tools, **kw)
            if (getattr(result, "text", "") or "").strip() or getattr(result, "tool_calls", None):
                return result
            log.warning("primary provider returned no text (%s), falling back", self.primary.name)
        except Exception as exc:
            log.warning("primary provider failed (%s: %s), falling back", self.primary.name, type(exc).__name__)
        return await self.secondary.chat(messages, tools=tools, **kw)

    async def check(self) -> bool:
        return await self.primary.check() or await self.secondary.check()


def _flop_gateway_key() -> str:
    """The gateway bearer, from the environment or the credential file.

    Read here rather than imported from the earning tools: the scheduler image does not
    ship apps/earn, and a missing credential must mean "not enabled", not a crash.
    """
    inline = os.environ.get("FLOP_GATEWAY_KEY", "").strip()
    if inline:
        return inline
    path = Path(os.environ.get("FLOP_GATEWAY_KEY_FILE", "/root/secrets/lumi-observatory/flop-gateway.key"))
    try:
        return path.read_text().strip()
    except OSError:
        return ""


class FlopGatewayProvider(OpenAICompatibleProvider):
    """FLOP's gateway, with the model taken from the credential's own allowlist.

    Guessing a model name is how a working credential looks broken: a model outside the
    allowlist is refused while the allowance is fine. So an unset model is resolved once
    from `/v1/keys/me` and cached, and only then does the call go out.
    """

    name = "flop_gateway"

    async def _resolve_model(self) -> str:
        explicit = (getattr(settings, "FLOP_GATEWAY_LLM_MODEL", "") or os.environ.get("FLOP_GATEWAY_MODEL", "")).strip()
        if explicit:
            return explicit
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(f"{self.base_url}/v1/keys/me", headers=_llm_headers(self.api_key))
            if resp.status_code == 200:
                allow = resp.json().get("model_allowlist") or []
                if allow:
                    return str(allow[0])
        except Exception as exc:
            log.warning("gateway model lookup failed (%s)", type(exc).__name__)
        return "qwen/qwen2.5-0.5b-instruct"

    async def chat(self, messages, tools=None, **kw) -> LLMResult:
        if not self.model:
            self.model = await self._resolve_model()
        return await super().chat(messages, tools=tools, **kw)


def build_flop_gateway_provider() -> LLMProvider | None:
    """FLOP's gateway as a provider, when it is enabled and a credential exists."""
    if not getattr(settings, "FLOP_GATEWAY_LLM_ENABLED", False):
        return None
    key = _flop_gateway_key()
    if not key:
        return None
    base = (
        getattr(settings, "FLOP_GATEWAY_LLM_BASE", "")
        or os.environ.get("FLOP_GATEWAY_BASE", "")
        or "https://api.flop.network"
    ).rstrip("/")
    model = getattr(settings, "FLOP_GATEWAY_LLM_MODEL", "") or os.environ.get("FLOP_GATEWAY_MODEL", "")
    return FlopGatewayProvider(base, model, key)
