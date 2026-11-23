#!/usr/bin/env python3
"""Testnet day-0 bootstrap for the agent lane.

The launch email settles the agent path: no approval, no waitlist. Claim testnet FLOP
from the faucet every day (API or a Technocore chat message) and spend it on inference —
one testnet FLOP spent is one agent voucher, and only vouchers decide the airdrop share.
Unspent faucet tokens earn nothing.

Endpoints and the first block hash are published on flop.finance at launch, so this
tool discovers them instead of hard-coding a guess:

    python apps/tools/flop_testnet_bootstrap.py --discover
    python apps/tools/flop_testnet_bootstrap.py --claim
    python apps/tools/flop_testnet_bootstrap.py --spend 5

Discovery fails loudly with the missing field rather than inventing a URL. Claiming and
spending write to the same ledger the scoreboard reads.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

SITE = "https://flop.finance/testnet/"
LEDGER = Path(os.environ.get("FLOP_LEDGER", "/var/lib/lumi-earn/flop-compute.jsonl"))
SS58 = os.environ.get("FLOP_ACCOUNT_SS58", "")
UA = "lumi-bootstrap/1.0"


def fetch(url: str, *, payload: dict | None = None, timeout: int = 30) -> tuple[int, str]:
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(  # noqa: S310 - https scheme enforced below
        url, data=body, headers={"User-Agent": UA, "Content-Type": "application/json"}
    )
    if not url.startswith(("https://", "http://")):
        raise ValueError("url must be http(s)")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")
    except (urllib.error.URLError, TimeoutError) as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def discover() -> dict:
    """Read the published endpoints and genesis hash off the testnet page."""
    code, html = fetch(SITE)
    if code != 200:
        return {"ok": False, "reason": "site_unreachable", "http": code}
    text = re.sub(r"<(script|style)[^>]*>[\s\S]*?</\1>", " ", html, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)

    urls = set(re.findall(r"https?://[a-z0-9.\-]+\.[a-z]{2,}(?:/[^\s\"'<>)]*)?", text, re.I))
    rpc = next((u for u in urls if "rpc" in u and "flop" in u), "")
    faucet = next((u for u in urls if "faucet" in u), "")
    gateway = next((u for u in urls if "api." in u and "flop" in u), "")
    genesis = ""
    m = re.search(r"0x[0-9a-f]{64}", text, re.I)
    if m:
        genesis = m.group(0)
    block = ""
    b = re.search(r"first block[^0-9]{0,40}(\d{1,12})", text, re.I)
    if b:
        block = b.group(1)

    missing = [k for k, v in (("rpc", rpc), ("faucet", faucet), ("gateway", gateway)) if not v]
    return {
        "ok": not missing,
        "missing": missing,
        "rpc": rpc,
        "faucet": faucet,
        "gateway": gateway,
        "genesis": genesis,
        "first_block": block,
        "published_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def chain_ready(rpc: str) -> dict:
    """Cheap liveness check against the discovered RPC, over HTTP JSON-RPC."""
    http_rpc = rpc.replace("wss://", "https://").replace("ws://", "http://")
    code, body = fetch(
        http_rpc,
        payload={"jsonrpc": "2.0", "id": 1, "method": "chain_getBlockHash", "params": [0]},
    )
    if code != 200:
        return {"ok": False, "reason": "rpc_unreachable", "http": code}
    try:
        genesis = json.loads(body).get("result", "")
    except ValueError:
        return {"ok": False, "reason": "rpc_unparsable"}
    return {"ok": bool(genesis), "genesis": genesis}


def claim(faucet: str, address: str) -> dict:
    """Ask the faucet for the daily grant; report the machine reason verbatim.

    The devnet faucet used POST /fund; the testnet surface is only published at launch,
    so a couple of documented-shaped paths are tried before declaring failure. A 404 is
    a wrong path, not a refusal, and must not be reported as one.
    """
    base = faucet.rstrip("/")
    last: dict = {"http": 0}
    for path in ("/fund", "/api/fund", "/v1/fund", "/faucet/fund"):
        code, body = fetch(f"{base}{path}", payload={"address": address})
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = {"raw": body[:200]}
        last = {"http": code, "path": path, **parsed}
        if code != 404:
            return last
    return last


def spend(gateway: str, amount: float) -> dict:
    """One small inference purchase so the voucher count moves.

    The launch email fixes testnet compute at a network price, so the amount is the fee
    we are willing to pay; the gateway is the documented agent path for inference.
    """
    payload = {
        "model": os.environ.get("FLOP_GATEWAY_MODEL", "Qwen/Qwen3-8B"),
        "messages": [{"role": "user", "content": "Reply with the single word: voucher"}],
        "max_tokens": 8,
        "max_cost_flop": amount,
    }
    base = gateway.rstrip("/")
    last: dict = {"http": 0}
    for path in ("/v1/chat/completions", "/chat/completions"):
        code, body = fetch(f"{base}{path}", payload=payload)
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = {"raw": body[:300]}
        last = {"http": code, "path": path, **parsed}
        if code != 404:
            return last
    return last


def append_ledger(row: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="FLOP testnet agent bootstrap")
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--claim", action="store_true")
    ap.add_argument("--spend", type=float, default=0.0)
    args = ap.parse_args()

    if not (args.discover or args.claim or args.spend):
        ap.print_help()
        return 1

    found = discover()
    if args.discover:
        print(json.dumps(found, indent=2, sort_keys=True))
        if not found.get("ok"):
            print(f"blocked: endpoints not published yet (missing: {', '.join(found.get('missing', []))})")
            return 6
        print(json.dumps(chain_ready(found["rpc"]), indent=2))
        return 0
    if not found.get("ok"):
        print(json.dumps({"ok": False, "reason": "endpoints_unpublished", "missing": found.get("missing", [])}))
        return 6

    if args.claim:
        if not SS58:
            print("FLOP_ACCOUNT_SS58 is not set", file=sys.stderr)
            return 2
        res = claim(found["faucet"], SS58)
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        amount = res.get("amount") or 0
        ok = res.get("http") == 200 and bool(res.get("success"))
        append_ledger({"ts": stamp, "kind": "claim", "http": res.get("http"), "amount": amount, "result": res})
        print(
            json.dumps(
                {
                    "ok": ok,
                    "http": res.get("http"),
                    "amount": amount,
                    "tx": res.get("txHash") or res.get("tx"),
                    "error": res.get("error"),
                },
                sort_keys=True,
            )
        )
        return 0 if ok else 1

    if args.spend:
        res = spend(found["gateway"], args.spend)
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        ok = res.get("http") == 200
        append_ledger(
            {
                "ts": stamp,
                "kind": "spend",
                "amount": args.spend,
                "http": res.get("http"),
                "status": "ok" if ok else "failed",
                "request_id": res.get("id") or res.get("request_id", ""),
                "usage": res.get("usage", {}),
            }
        )
        print(
            json.dumps(
                {
                    "ok": ok,
                    "http": res.get("http"),
                    "amount": args.spend,
                    "error": res.get("error"),
                    "usage": res.get("usage", {}),
                },
                sort_keys=True,
            )
        )
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
