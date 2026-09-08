# LUMI — PHASE 12 LLM provider tests (mock + openai-compatible + embedding)
import pytest

from agent_core.llm import (
    LLMMessage,
    LLMResult,
    MockProvider,
    OpenAICompatibleEmbeddingProvider,
    OpenAICompatibleProvider,
    build_embedding_provider,
    build_provider,
)


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeClient:
    def __init__(self, *a, **k):
        self._data = k.get("_data") if "_data" in k else a[0] if a else None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def post(self, url, json=None, headers=None):
        return _FakeResp(self._data)


@pytest.mark.asyncio
async def test_mock_provider_chat_and_check():
    p = MockProvider()
    r = await p.chat([LLMMessage("user", "hello")])
    assert isinstance(r, LLMResult)
    assert r.finish_reason == "stop"
    assert await p.check() is True


@pytest.mark.asyncio
async def test_openai_compatible_chat_text():
    p = OpenAICompatibleProvider("https://x.ai/v1", "m", "k")
    p._client = _FakeClient(
        {
            "choices": [{"message": {"content": "hello", "tool_calls": []}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 5},
        }
    )
    r = await p.chat([LLMMessage("user", "hello")])
    assert r.text == "hello"
    assert r.finish_reason == "stop"
    assert r.usage.get("total_tokens") == 5


@pytest.mark.asyncio
async def test_openai_compatible_chat_tool_calls():
    p = OpenAICompatibleProvider("https://x.ai/v1", "m", "k")
    p._client = _FakeClient(
        {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "t1",
                                "function": {"name": "github_repo_read", "arguments": '{"owner": "a", "repo": "b"}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
    )
    r = await p.chat([LLMMessage("user", "hi")], tools=[{"name": "github_repo_read"}])
    assert len(r.tool_calls) == 1
    assert r.tool_calls[0].name == "github_repo_read"
    assert r.tool_calls[0].arguments == {"owner": "a", "repo": "b"}


@pytest.mark.asyncio
async def test_openai_compatible_check_true():
    p = OpenAICompatibleProvider("https://x.ai/v1", "m", "k")
    p._client = _FakeClient(
        {
            "choices": [{"message": {"content": "pong", "tool_calls": []}, "finish_reason": "stop"}],
        }
    )
    assert await p.check() is True


@pytest.mark.asyncio
async def test_openai_compatible_check_false():
    class _Boom:
        async def post(self, url, json=None, headers=None):
            raise RuntimeError("closed")

    p = OpenAICompatibleProvider("https://x.ai/v1", "m", "k")
    p._client = _Boom()
    assert await p.check() is False


def test_build_provider_mock(monkeypatch):
    monkeypatch.setattr("agent_core.llm.settings.LLM_PROVIDER", "mock")
    assert isinstance(build_provider(), MockProvider)


def test_build_provider_openai(monkeypatch):
    monkeypatch.setattr("agent_core.llm.settings.LLM_PROVIDER", "openai_compatible")
    monkeypatch.setattr("agent_core.llm.settings.LLM_BASE_URL", "https://x.ai/v1")
    monkeypatch.setattr("agent_core.llm.settings.LLM_MODEL", "m")
    monkeypatch.setattr("agent_core.llm.settings.LLM_API_KEY", "k")
    p = build_provider()
    assert isinstance(p, OpenAICompatibleProvider)


def test_build_embedding_provider_none(monkeypatch):
    monkeypatch.setattr("agent_core.llm.settings.EMBEDDING_MODEL", "")
    monkeypatch.setattr("agent_core.llm.settings.LLM_MODEL", "")
    assert build_embedding_provider() is None


@pytest.mark.asyncio
async def test_embedding_provider_embed(monkeypatch):
    monkeypatch.setattr(
        "agent_core.llm.httpx.AsyncClient",
        lambda *a, **k: _FakeClient({"data": [{"embedding": [0.1, 0.2, 0.3]}]}),
    )
    p = OpenAICompatibleEmbeddingProvider("https://x.ai/v1", "m", "k")
    v = await p.embed("test")
    assert v == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_embedding_provider_bad_shape(monkeypatch):
    monkeypatch.setattr(
        "agent_core.llm.httpx.AsyncClient",
        lambda *a, **k: _FakeClient({"data": [{"embedding": "not-a-list"}]}),
    )
    p = OpenAICompatibleEmbeddingProvider("https://x.ai/v1", "m", "k")
    with pytest.raises(ValueError):
        await p.embed("test")


@pytest.mark.asyncio
async def test_openai_provider_sends_session_and_ua_headers(monkeypatch):
    monkeypatch.setattr("agent_core.llm.settings.LLM_SESSION_ID", "lumi-test")
    monkeypatch.setattr("agent_core.llm.settings.LLM_USER_AGENT", "TestUA")
    captured: dict = {}

    class _CapClient:
        async def post(self, url, json=None, headers=None):
            captured["headers"] = headers
            return _FakeResp(
                {"choices": [{"message": {"content": "hi", "tool_calls": []}, "finish_reason": "stop"}], "usage": {}}
            )

    p = OpenAICompatibleProvider("https://x.ai/v1", "m", "k")
    p._client = _CapClient()
    await p.chat([LLMMessage("user", "hi")])
    assert captured["headers"]["x-opencode-session"] == "lumi-test"
    assert captured["headers"]["User-Agent"] == "TestUA"
    assert captured["headers"]["Authorization"] == "Bearer k"
