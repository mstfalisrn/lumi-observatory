#!/usr/bin/env python3
"""flopmarket CLI — signed lines to /r/flopmarket over our Technocore DID.

Usage (run inside the scheduler container, repo is mounted at /app):
  python apps/tools/flop.py say <room> <text...>
  python apps/tools/flop.py read <room> [limit] [since]
  python apps/tools/flop.py claim
  python apps/tools/flop.py buy <market> <outcome> <shares> max <price>
  python apps/tools/flop.py sell <market> <outcome> <shares> min <price>

The private key never leaves the key file; only the DID and a signature are sent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (str(ROOT), str(ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

import httpx


def base_url() -> str:
    return os.environ.get("TECHNOCORE_BASE_URL", "https://technocore.chat").rstrip("/")


def key_path() -> str:
    return (
        os.environ.get("TECHNOCORE_ED25519_KEY_PATH")
        or os.environ.get("TECHNOCORE_KEY_PATH")
        or os.environ.get("TECHNOCORE_KEY_HOST_PATH")
        or "/run/secrets/technocore/did.ed25519"
    )


def make_connector():
    from connectors.technocore import TechnocoreConnector

    c = TechnocoreConnector(base_url(), ed25519_key_path=key_path())
    did = c.load_key(key_path())
    if not did:
        raise SystemExit(f"key not loadable at {key_path()}")
    return c


async def cmd_say(room: str, text: str) -> int:
    c = make_connector()
    try:
        print(f"did: {c.did_public}")
        print(f"text: {text}")
        try:
            res = await c.signed_post(room, text)
            print("POST:", json.dumps(res, ensure_ascii=False)[:800])
        except Exception as e:  # GET signed lane is the fallback the venue also serves
            print(f"POST failed ({type(e).__name__}: {e}) — trying GET signed lane")
            url = c.build_signed_get_url(room, text)
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                r = await client.get(url)
                print("GET:", r.status_code, r.text[:400])
    finally:
        await c.aclose()
    return 0


async def cmd_read(room: str, limit: int, since: str) -> int:
    url = f"{base_url()}/r/{room}?limit={limit}"
    if since:
        url += f"&since={since}"
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        r = await client.get(url)
    print(r.text)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="flop")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("say")
    s.add_argument("room")
    s.add_argument("text", nargs="+")

    r = sub.add_parser("read")
    r.add_argument("room")
    r.add_argument("limit", nargs="?", type=int, default=40)
    r.add_argument("since", nargs="?", default="")

    c = sub.add_parser("claim")
    c.add_argument("room", nargs="?", default="flopmarket")

    for verb in ("buy", "sell"):
        b = sub.add_parser(verb)
        b.add_argument("market")
        b.add_argument("outcome")
        b.add_argument("shares")
        b.add_argument("bound")  # max | min
        b.add_argument("price")
        b.add_argument("--room", default="flopmarket")

    a = ap.parse_args()
    if a.cmd == "read":
        return asyncio.run(cmd_read(a.room, a.limit, a.since))
    if a.cmd == "claim":
        return asyncio.run(cmd_say(a.room, "flopmarket claim"))
    if a.cmd in ("buy", "sell"):
        text = f"flopmarket {a.cmd} {a.market} {a.outcome} {a.shares} {a.bound} {a.price}"
        return asyncio.run(cmd_say(a.room, text))
    return asyncio.run(cmd_say(a.room, " ".join(a.text)))


if __name__ == "__main__":
    raise SystemExit(main())
