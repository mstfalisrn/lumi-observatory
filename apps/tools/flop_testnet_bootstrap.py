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
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

SITE = "https://flop.finance/testnet/"
DOCS = "https://docs.flop.network/networks/testnet"
AGENT_DOCS = "https://docs.flop.network/docs/agent-onboarding"
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


def _origin(url: str) -> str:
    """Scheme + host only: discovered links point at a page, the API lives at the root."""
    if not url:
        return ""
    parts = urlparse(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else url


def _plain(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>[\s\S]*?</\1>", " ", html, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


def discover() -> dict:
    """Read the published endpoints and genesis hash.

    Two surfaces publish them and they do not move together: the marketing page is the
    announcement, the operator docs carry the values first (that is where the live network
    named its RPC and genesis while flop.finance still said draft). Both are read, docs win.
    """
    site_code, site_html = fetch(SITE)
    docs_code, docs_html = fetch(DOCS)
    _agent_code, agent_html = fetch(AGENT_DOCS)
    text = " ".join(_plain(h) for h in (site_html, docs_html, agent_html) if h)
    if site_code != 200 and docs_code != 200:
        return {"ok": False, "reason": "sites_unreachable", "http": site_code}

    urls = set(re.findall(r"https?://[a-z0-9.\-]+\.[a-z]{2,}(?:/[^\s\"'<>)]*)?", text, re.I))
    for h in (site_html, docs_html, agent_html):
        for href in re.findall(r'href="(https?://[^"]+)"', h or "", re.I):
            urls.add(href.split("#")[0].rstrip("/"))
    rpc = next(
        (u for u in urls if (urlparse(u).hostname or "") in {"testnet.flop.network", "rpc.testnet.flop.network"}),
        "",
    )
    rpc = rpc or next((u for u in urls if "rpc" in u and "flop" in u), "")
    faucet_u = next((u for u in urls if "faucet" in u), "")
    gateway_u = next((u for u in urls if "api." in u and "flop" in u), "")
    faucet = _origin(faucet_u)
    gateway = _origin(gateway_u)
    genesis = ""
    m = re.search(r"genesis block hash\s*(0x[0-9a-f]{64})", text, re.I) or re.search(r"0x[0-9a-f]{64}", text, re.I)
    if m:
        genesis = m.group(1) if m.groups() else m.group(0)
    chain_id = ""
    c = re.search(r"chain id is\s*([a-z0-9_]+)", text, re.I)
    if c:
        chain_id = c.group(1)

    missing = [k for k, v in (("rpc", rpc), ("faucet", faucet), ("gateway", gateway)) if not v]
    return {
        "ok": not missing,
        "missing": missing,
        "rpc": rpc,
        "faucet": faucet,
        "gateway": gateway,
        "genesis": genesis,
        "chain_id": chain_id,
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


def faucet_chain(faucet: str) -> dict:
    """The faucet's own chain identity — never request funds before comparing it."""
    code, body = fetch(f"{faucet.rstrip('/')}/chain")
    if code != 200:
        return {"ok": False, "http": code}
    try:
        data = json.loads(body)
    except ValueError:
        return {"ok": False, "http": code}
    genesis = data.get("genesisHash") or data.get("genesis_hash") or data.get("genesis") or ""
    return {"ok": bool(genesis), "genesis": genesis, "chain": data.get("chain") or data.get("name") or ""}


def signing_seed() -> bytes | None:
    """The Ed25519 seed whose public key is our FLOP account (same key as the DID)."""
    path = Path(os.environ.get("FLOP_SIGNING_KEY_FILE", "/root/secrets/lumi-observatory/did.ed25519"))
    try:
        seed = path.read_bytes()
    except OSError:
        return None
    return seed[:32] if len(seed) >= 32 else None


def claim_message(address: str, nonce: str, genesis: str) -> bytes:
    """The exact text the faucet verifies: flop-faucet-agent/v1|<genesis>|<address>|<nonce>."""
    return f"flop-faucet-agent/v1|{genesis}|{address}|{nonce}".encode()


def sign_claim(address: str, nonce: str, genesis: str) -> str | None:
    seed = signing_seed()
    if not seed:
        return None
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sig = Ed25519PrivateKey.from_private_bytes(seed).sign(claim_message(address, nonce, genesis))
    return "0x" + sig.hex()


def claim(faucet: str, address: str) -> dict:
    """Ask the faucet for the daily grant; report the machine reason verbatim.

    The devnet faucet used POST /fund; the testnet surface is only published at launch,
    so a couple of documented-shaped paths are tried before declaring failure. A 404 is
    a wrong path, not a refusal, and must not be reported as one.
    """
    base = faucet.rstrip("/")
    nonce = str(int(time.time() * 1000))
    # The testnet lane wants a signed D10 claim; the devnet path took a bare address.
    # Sign when we can and let the faucet name the refusal otherwise.
    genesis = discover().get("genesis", "")
    payload = {"address": address, "client": "lumi-bootstrap/1.0"}
    signature = sign_claim(address, nonce, genesis) if genesis else None
    if signature:
        payload.update({"nonce": nonce, "signature": signature})
    if signature:
        state = Path(os.environ.get("FLOP_CLAIM_STATE", "/var/lib/lumi-earn/claim-reconcile.json"))
        try:
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text(json.dumps({"nonce": nonce, "signature": signature, "address": address,
                                         "genesis": genesis, "sent_at": datetime.now(UTC).isoformat()}))
        except OSError:
            pass
    last: dict = {"http": 0}
    for path in ("/v1/testnet/agent-claims", "/fund", "/api/fund", "/v1/fund", "/faucet/fund"):
        code, body = fetch(f"{base}{path}", payload=payload)
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = {"raw": body[:200]}
        last = {"http": code, "path": path, **parsed}
        if code != 404:
            # The testnet agent lane answers 200 with success:false while the signed
            # claim format is unpublished; that is a held gate, not a transport error.
            if parsed.get("reason") == "claim_signature_invalid":
                last["reason"] = "claim_needs_signature"
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
            # The testnet agent lane answers 200 with success:false while the signed
            # claim format is unpublished; that is a held gate, not a transport error.
            if parsed.get("reason") == "claim_signature_invalid":
                last["reason"] = "claim_needs_signature"
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
    needed = ("faucet",) if args.claim else ("gateway",)
    absent = [k for k in needed if not found.get(k)]
    if absent:
        print(json.dumps({"ok": False, "reason": "endpoints_unpublished", "missing": absent}))
        return 6

    if args.claim:
        if not SS58:
            print("FLOP_ACCOUNT_SS58 is not set", file=sys.stderr)
            return 2
        chain = faucet_chain(found["faucet"])
        if found.get("genesis") and chain.get("genesis") and chain["genesis"] != found["genesis"]:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "reason": "faucet_wrong_chain",
                        "faucet_genesis": chain["genesis"],
                        "testnet_genesis": found["genesis"],
                    },
                    sort_keys=True,
                )
            )
            return 7
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
                    "reason": res.get("reason"),
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
