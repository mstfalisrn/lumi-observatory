"""Unit tests for packages/connectors/flop_mcp.py — no network."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PACKAGES = Path(__file__).resolve().parents[2] / "packages"
sys.path.insert(0, str(PACKAGES))

from connectors.flop_mcp import FlopMcpConnector, FlopMcpError


class FakeResponse:
    def __init__(self, text: str, status: int = 200) -> None:
        self.text = text
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    def __init__(self, body: str) -> None:
        self.body = body
        self.sent: list[dict] = []

    async def post(self, url, json=None, **kw):
        self.sent.append(json or {})
        return FakeResponse(self.body)

    async def aclose(self) -> None:
        return None


def test_decode_plain_json() -> None:
    assert FlopMcpConnector._decode('{"result": {"ok": true}}')["result"]["ok"] is True


def test_decode_sse_frame() -> None:
    body = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"tools":[]}}'
    assert FlopMcpConnector._decode(body)["result"] == {"tools": []}


def test_decode_garbage_is_empty() -> None:
    assert FlopMcpConnector._decode("not json at all") == {}
    assert FlopMcpConnector._decode("") == {}


def test_allowlist_covers_the_read_surface() -> None:
    for name in ("flop_get_stats", "flop_list_settlements", "flop_get_model_pricing"):
        assert name in FlopMcpConnector().allowed_tools


@pytest.mark.asyncio
async def test_call_tool_refuses_a_tool_outside_the_allowlist() -> None:
    conn = FlopMcpConnector()
    with pytest.raises(FlopMcpError, match="not allowlisted"):
        await conn.call_tool("flop_send_tokens", {})
    await conn.aclose()


@pytest.mark.asyncio
async def test_call_tool_joins_content_and_trims() -> None:
    conn = FlopMcpConnector(max_chars=10)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "x" * 50}]}})
    conn._client = FakeClient(body)  # type: ignore[assignment]
    res = await conn.call_tool("flop_get_stats", {})
    assert res["tool"] == "flop_get_stats"
    assert res["truncated"] is True
    assert len(res["text"]) == 10


@pytest.mark.asyncio
async def test_stats_and_settlements_use_the_right_tools() -> None:
    conn = FlopMcpConnector()
    conn._client = FakeClient(json.dumps({"result": {"content": [{"text": "{}"}]}}))  # type: ignore[assignment]
    await conn.stats()
    await conn.settlements(3)
    names = [call["params"]["name"] for call in conn._client.sent]  # type: ignore[attr-defined]
    assert names == ["flop_get_stats", "flop_list_settlements"]
    assert conn._client.sent[1]["params"]["arguments"] == {"limit": 3}  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_rpc_error_raises_with_the_server_message() -> None:
    conn = FlopMcpConnector()
    conn._client = FakeClient(json.dumps({"error": {"message": "boom"}}))  # type: ignore[assignment]
    with pytest.raises(FlopMcpError, match="boom"):
        await conn.stats()


@pytest.mark.asyncio
async def test_list_tools_ignores_malformed_entries() -> None:
    conn = FlopMcpConnector()
    body = json.dumps({"result": {"tools": [{"name": "a"}, "junk", {"name": "b"}]}})
    conn._client = FakeClient(body)  # type: ignore[assignment]
    tools = await conn.list_tools()
    assert [t["name"] for t in tools] == ["a", "b"]


@pytest.mark.asyncio
async def test_arguments_must_be_an_object() -> None:
    conn = FlopMcpConnector()
    with pytest.raises(FlopMcpError, match="arguments must be an object"):
        await conn.call_tool("flop_get_stats", ["not", "a", "dict"])  # type: ignore[arg-type]
    await conn.aclose()
