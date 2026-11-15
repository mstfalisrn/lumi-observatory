#!/usr/bin/env python3
"""Daily archive of the FLOP compute market's own numbers.

Why keep this: the Flop Reference Compute Rate is published live and never as history.
When testnet opens we want to know what a GFLOP actually cost over time, per model, and
how many miners were serving — decisions about channel size and budget should come from
that tape, not from the first price we happen to see.

One JSON object per line, append-only, no credentials. A day with no settlements is
recorded as such (``settlements: 0``) rather than skipped, so gaps are visible.

  FLOP_FRCR_DB  output file (default /var/lib/lumi-earn/frcr.jsonl)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "packages"))

from connectors.flop_mcp import FlopMcpConnector


def _json(text: str) -> dict:
    try:
        return json.loads(text)
    except ValueError:
        return {}


def _prices(text: str) -> list[int]:
    body = _json(text)
    rows = body.get("data") if isinstance(body.get("data"), list) else []
    out: list[int] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            out.append(int(str(row.get("pricePerGflop", "")).split(".")[0]))
        except ValueError:
            continue
    if out:
        return out
    return [int(m) for m in re.findall(r'"pricePerGflop"\s*:\s*"(\d+)', text)]


async def snapshot(samples: int = 25) -> dict:
    mcp = FlopMcpConnector(max_chars=200_000)
    try:
        stats = _json((await mcp.call_tool("flop_get_stats", {}))["text"]).get("stats", {})
        tape_text = (await mcp.settlements(samples))["text"]
        models_text = (await mcp.call_tool("flop_discover_models", {}))["text"]
    finally:
        await mcp.aclose()

    prices = _prices(tape_text)
    models = _json(models_text).get("models") or []
    served = [m for m in models if isinstance(m, dict) and m.get("served")]

    record = {
        "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "block": stats.get("latestBlockNumber"),
        "tps": stats.get("tps"),
        "active_operators": stats.get("activeOperators"),
        "total_accounts": stats.get("totalAccounts"),
        "settlements": len(prices),
        "price_per_gflop": {
            "min": min(prices) if prices else None,
            "median": sorted(prices)[len(prices) // 2] if prices else None,
            "max": max(prices) if prices else None,
        },
        "models_listed": len(models),
        "models_served": len(served),
        "served_names": sorted({str(m.get("modelName")) for m in served})[:5],
    }
    return record


def main() -> int:
    out = Path(os.environ.get("FLOP_FRCR_DB", "/var/lib/lumi-earn/frcr.jsonl"))
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        record = asyncio.run(snapshot())
    except Exception as exc:
        print(json.dumps({"ts": datetime.now(UTC).isoformat(), "error": type(exc).__name__}))
        return 1
    with out.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
    print(json.dumps(record, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
