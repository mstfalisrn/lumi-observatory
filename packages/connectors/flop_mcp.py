# LUMI — FLOP Network MCP connector (read-only, allowlisted)
"""Client for FLOP's hosted MCP server (https://mcp.flop.network/mcp).

The server exposes the network's own read surface: chain stats, settlements, session
offers, model pricing, miner ranking, the knowledge catalog and the yellowpaper
requirement explainer. For LUMI this is the compute market's ground truth — the same
data the explorer dashboard renders — without a gateway key or a token.

Safety:
  * Only allowlisted tools are callable. A tool the server adds later is refused until
    it is reviewed and added here, so a remote addition cannot widen LUMI's capability.
  * Read-only: nothing here can post, sign or spend.
  * Responses are trimmed to ``max_chars`` so a hostile payload cannot flood the model
    context; the full text stays out of logs.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

DEFAULT_URL = "https://mcp.flop.network/mcp"
PROTOCOL_VERSION = "2024-11-05"

# Reviewed read-only surface of mcp.flop.network (34 tools, 2026-11-03).
READ_ONLY_TOOLS = frozenset(
    {
        "flop_health",
        "flop_get_balance",
        "flop_get_stats",
        "flop_get_block",
        "flop_indexer_query",
        "flop_list_settlements",
        "flop_list_session_offers",
        "flop_list_swaps",
        "flop_get_compute_channel",
        "flop_check_htlc",
        "flop_check_session",
        "flop_get_inference_receipt",
        "flop_get_result",
        "flop_get_poui_leaderboard",
        "flop_trust_root",
        "flop_get_tcb_status",
        "flop_verify_tee_quote",
        "flop_generate_secrets",
        "flop_discover_models",
        "flop_find_best_miner",
        "flop_get_model_info",
        "flop_get_model_pricing",
        "flop_compare_pricing",
        "flop_list_providers",
        "flop_estimate_cost",
        "flop_estimate_inference_cost",
        "flop_get_agent_card",
        "flop_agent_pack_manifest",
        "flop_agent_skill_get",
        "flop_agent_pack_install",
        "flop_knowledge_search",
        "flop_knowledge_get",
        "flop_knowledge_manifest",
        "flop_explain_requirement",
    }
)


class FlopMcpError(RuntimeError):
    """Raised when the MCP server answers with an error."""


class FlopMcpConnector:
    def __init__(
        self,
        url: str = DEFAULT_URL,
        *,
        timeout: float = 30.0,
        max_chars: int = 4000,
        allowed_tools: frozenset[str] | None = None,
    ) -> None:
        self.url = url
        self.max_chars = max_chars
        self.allowed_tools = allowed_tools or READ_ONLY_TOOLS
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "User-Agent": "lumi-agent/1.0",
                "MCP-Protocol-Version": PROTOCOL_VERSION,
            },
        )
        self._next_id = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            payload["params"] = params
        resp = await self._client.post(self.url, json=payload)
        resp.raise_for_status()
        data = self._decode(resp.text)
        if "error" in data:
            err = data["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            raise FlopMcpError(str(message)[:200])
        return data.get("result") or {}

    @staticmethod
    def _decode(body: str) -> dict:
        """The server answers either plain JSON or an SSE frame ("data: {...}")."""
        body = body.strip()
        if not body:
            return {}
        if body.startswith("{"):
            try:
                return json.loads(body)
            except ValueError:
                return {}
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if chunk.startswith("{"):
                    try:
                        return json.loads(chunk)
                    except ValueError:
                        continue
        return {}

    async def initialize(self) -> dict:
        return await self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "lumi", "version": "1.0"},
            },
        )

    async def list_tools(self) -> list[dict]:
        result = await self._rpc("tools/list")
        raw = result.get("tools")
        tools: list = raw if isinstance(raw, list) else []
        return [t for t in tools if isinstance(t, dict)]

    async def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        if name not in self.allowed_tools:
            raise FlopMcpError(f"tool not allowlisted for LUMI: {name}")
        if arguments is not None and not isinstance(arguments, dict):
            raise FlopMcpError("arguments must be an object")
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        text = self._text(result)
        return {
            "tool": name,
            "truncated": len(text) > self.max_chars,
            "text": text[: self.max_chars],
        }

    @staticmethod
    def _text(result: dict) -> str:
        content = result.get("content")
        if isinstance(content, list):
            parts = [str(c.get("text", "")) for c in content if isinstance(c, dict) and c.get("text") is not None]
            return "\n".join(parts).strip()
        if isinstance(result, dict):
            return json.dumps(result, ensure_ascii=False)[:8000]
        return ""

    # Convenience wrappers for the two facts the earning lane needs most.
    async def stats(self) -> dict:
        return await self.call_tool("flop_get_stats", {})

    async def settlements(self, limit: int = 5) -> dict:
        return await self.call_tool("flop_list_settlements", {"limit": int(limit)})

    async def pricing(self) -> dict:
        return await self.call_tool("flop_get_model_pricing", {})
