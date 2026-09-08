# LUMI — LLM provider interface + OpenAI-compatible + mock
# Provider independent; base_url/model/api_key selected via env. Does not touch host env.
from __future__ import annotations

import abc
import json
from dataclasses import dataclass, field

import httpx

from observability.config import settings

_DEFAULT_LLM_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
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
    async def chat(self, messages: list[LLMMessage], tools: list[dict] | None = None, **kw) -> LLMResult:
        ...

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
        payload: dict = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            **kw,
        }
        if tools is not None:
            payload["tools"] = [{"type": "function", "function": t} for t in tools]
        headers = _llm_headers(self.api_key)
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
        return LLMResult(
            text=msg.get("content") or "",
            tool_calls=tool_calls,
            finish_reason=data["choices"][0].get("finish_reason", ""),
            usage=data.get("usage", {}),
        )

    async def check(self) -> bool:
        try:
            await self.chat([LLMMessage("user", "ping")])
            return True
        except Exception:
            return False


def build_provider(provider: str | None = None) -> LLMProvider:
    p = (provider or settings.LLM_PROVIDER or "mock").lower()
    if p == "openai_compatible" or p == "openai":
        return OpenAICompatibleProvider(
            settings.LLM_BASE_URL, settings.LLM_MODEL, settings.LLM_API_KEY
        )
    return MockProvider()


# ---------------------------------------------------------------------------
# Embedding (memory semantic retrieval)
# ---------------------------------------------------------------------------
class EmbeddingProvider(abc.ABC):
    @abc.abstractmethod
    async def embed(self, text: str) -> list[float]:
        ...


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