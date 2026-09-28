#!/usr/bin/env python3
"""Close Call (close-1) runner — register, read the referee, take a position.

Contest: one NVDA future on Hyperliquid, 1 POLF per dollar, mint 10,000 POLF per
owner key, score = POLF after settlement at S minus 10,000, top three share
1,000,000 FLOP. Lock: 2026-10-04 09:00 UTC.

Why this shape:
  * Our registration goes in a room WE register ("lumi-close1"). /r/close1 moves
    at ~16 messages/second, so a registration posted there can leave the room's
    retained history before the referee's read cursor reaches it; a quiet room of
    our own is read reliably.
  * Trades cost 1% of value per side, and the "better price than the sweep's
    reference" rule claws back a discount, so the only way to score is to hold a
    position and be right. We therefore take one position and hold it.
  * Direction rule (documented, no fabricated inputs): xyz:NVDA last price vs its
    20-day mean, plus 7-day momentum. Above the mean with positive momentum ->
    long; below with negative -> short; otherwise stand aside.

Usage:
  python apps/earn/close1.py --register --post       # room + owner registration
  python apps/earn/close1.py --scan                  # referee state + live offers
  python apps/earn/close1.py --auto --post --max-qty 20
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (str(ROOT), str(ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

import httpx

BASE = os.environ.get("TECHNOCORE_BASE_URL", "https://technocore.chat").rstrip("/")
HL = "https://api.hyperliquid.xyz/info"
ROOM = "close1"
OUR_ROOM = "lumi-close1"
SEASON = "close-1"
STATE = Path(os.environ.get("LUMI_EARN_STATE_DIR", "/var/lib/lumi-earn"))
TAKEN = STATE / "close1-taken.json"


def key_path() -> str:
    return (
        os.environ.get("TECHNOCORE_ED25519_KEY_PATH")
        or os.environ.get("TECHNOCORE_KEY_PATH")
        or os.environ.get("TECHNOCORE_KEY_HOST_PATH")
        or "/run/secrets/technocore/did.ed25519"
    )


def connector():
    from connectors.technocore import TechnocoreConnector

    c = TechnocoreConnector(BASE, ed25519_key_path=key_path())
    did = c.load_key(key_path())
    if not did:
        raise SystemExit(f"key not loadable at {key_path()}")
    return c


def raw_sig(c, text: str) -> str:
    """Base64url, unpadded signature over an exact UTF-8 string (not the lane)."""
    s = c._signing_key.sign(text.encode("utf-8")).signature
    return base64.urlsafe_b64encode(s).decode("ascii").rstrip("=")


def line_json(room_view: str) -> list[tuple[int, dict]]:
    out = []
    for ln in (room_view or "").splitlines():
        ln = ln.strip()
        if not ln.startswith("["):
            continue
        try:
            seq = int(ln[1 : ln.index("]")])
            body = ln[ln.index("{") :]
            out.append((seq, json.loads(body)))
        except Exception:
            continue
    return out


def terms_raw(line: str) -> str | None:
    """The exact terms JSON substring, so signatures over it still verify."""
    i = line.find('"terms":')
    if i < 0:
        return None
    j = line.index("{", i)
    depth, k, instr, esc = 0, j, False, False
    while k < len(line):
        ch = line[k]
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
        elif ch == '"':
            instr = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return line[j : k + 1]
        k += 1
    return None


async def hl_candles(hours: int = 720) -> list[dict]:
    end = int(time.time() * 1000)
    start = end - hours * 3600 * 1000
    async with httpx.AsyncClient(timeout=25.0) as cl:
        r = await cl.post(
            HL,
            json={"type": "candleSnapshot", "req": {"coin": "xyz:NVDA", "interval": "1h", "startTime": start, "endTime": end}},
        )
        r.raise_for_status()
        return r.json()


def view(candles: list[dict]) -> tuple[str, str]:
    """(side, why) from price vs the 20-day mean and 7-day momentum."""
    closes = [float(c["c"]) for c in candles if float(c.get("c") or 0) > 0]
    if len(closes) < 200:
        return "flat", f"yetersiz veri ({len(closes)} saat)"
    px = closes[-1]
    mean20 = sum(closes[-480:]) / len(closes[-480:])
    mom7 = px / closes[-168] - 1 if len(closes) >= 168 else 0.0
    if px > mean20 and mom7 > 0:
        return "buy", f"px {px:.2f} > 20g ort {mean20:.2f}, 7g momentum {mom7*100:+.2f}%"
    if px < mean20 and mom7 < 0:
        return "sell", f"px {px:.2f} < 20g ort {mean20:.2f}, 7g momentum {mom7*100:+.2f}%"
    return "flat", f"px {px:.2f} vs 20g ort {mean20:.2f}, 7g {mom7*100:+.2f}% — kenarda kal"


async def read_room(c, room: str, since: int, wait: int = 0) -> tuple[int, str]:
    async with httpx.AsyncClient(timeout=30.0) as cl:
        r = await cl.get(f"{BASE}/r/{room}", params={"since": since, "wait": wait})
        return r.status_code, r.text


async def referee_state() -> dict:
    out: dict = {}
    async with httpx.AsyncClient(timeout=25.0) as cl:
        for room in ("d-close1-price", "d-close1-flow", "d-close1-state"):
            r = await cl.get(f"{BASE}/r/{room}", params={"limit": 1})
            for _, body in line_json(r.text):
                out[room] = body
    return out


def load_taken() -> dict:
    try:
        return json.loads(TAKEN.read_text())
    except Exception:
        return {}


def save_taken(d: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    TAKEN.write_text(json.dumps(d, indent=1))


async def register(post: bool) -> None:
    c = connector()
    did = c.did_public
    room_msg = json.dumps({"t": "room", "season": SEASON, "room": OUR_ROOM}, separators=(",", ":"))
    owner_msg = json.dumps({"t": "owner", "season": SEASON, "key": did}, separators=(",", ":"))
    print(f"did: {did}")
    print(f"owner message: {owner_msg}")
    if not post:
        print("(dry) no posts made")
        await c.aclose()
        return
    for room, msg in ((ROOM, room_msg), (OUR_ROOM, owner_msg), (ROOM, owner_msg)):
        try:
            await c.signed_post(room, msg)
            print(f"posted to /r/{room}")
        except Exception as e:
            print(f"post to /r/{room} failed: {e}")
        await asyncio.sleep(2)
    await c.aclose()


async def scan() -> None:
    st = await referee_state()
    price = st.get("d-close1-price") or {}
    flow = st.get("d-close1-flow") or {}
    state = st.get("d-close1-state") or {}
    print("referee price:", json.dumps(price)[:220])
    print("flow rooms:", flow.get("rooms"))
    print("flow mints:", flow.get("mints"))
    print("owners:", state.get("owners"))
    c = connector()
    did = c.did_public
    _code, text = await read_room(c, ROOM, 0)
    offers = []
    for seq, body in line_json(text):
        if body.get("t") not in ("offer", "trade"):
            continue
        t = body.get("terms") or {}
        if str(t.get("taker")) == "any" and body.get("t") == "offer":
            offers.append((seq, t))
    print(f"taker=any offers in the retained window: {len(offers)}")
    for seq, t in offers[:12]:
        print(f"  seq {seq} id {t.get('id')} {t.get('side')} {t.get('qty')} @ {t.get('px')} until {t.get('until')}")
    print("our did:", did)
    await c.aclose()


async def auto(post: bool, max_qty: float) -> None:
    c = connector()
    did = c.did_public
    candles = await hl_candles()
    side, why = view(candles)
    print(f"view: {side} — {why}")
    if side == "flat":
        print("no position taken (rule says stand aside)")
        await c.aclose()
        return
    st = await referee_state()
    price = st.get("d-close1-price") or {}
    limits = price.get("limits") or []
    lo, hi = (float(limits[0]), float(limits[1])) if len(limits) == 2 else (0.0, 1e9)
    want_maker_side = "sell" if side == "buy" else "buy"  # counterparty's side
    taken = load_taken()
    _code, text = await read_room(c, ROOM, 0)
    best = None
    for seq, body in line_json(text):
        if body.get("t") != "offer":
            continue
        tr = body.get("terms") or {}
        if str(tr.get("taker")) != "any" or tr.get("side") != want_maker_side:
            continue
        px = float(tr.get("px") or 0)
        qty = float(tr.get("qty") or 0)
        if not (lo <= px <= hi) or qty < 0.1:
            continue
        if str(tr.get("id")) in taken:
            continue
        if best is None or (side == "buy" and px < float(best[1].get("px"))):
            best = (seq, tr)
    if not best:
        print(f"no {want_maker_side} offer inside [{lo}, {hi}] in the retained window")
        await c.aclose()
        return
    seq, tr = best
    qty = min(max_qty, float(tr.get("qty") or 0))
    print(f"pick seq {seq}: {tr.get('side')} {tr.get('qty')} @ {tr.get('px')} (limits {lo}-{hi}); taking {qty}")
    line = ""
    for ln in (text or "").splitlines():
        if ln.strip().startswith(f"[{seq}]"):
            line = ln
            break
    raw = terms_raw(line)
    if not raw:
        print("could not extract the exact terms substring — refusing to sign")
        await c.aclose()
        return
    maker_sig = None
    try:
        maker_sig = json.loads(line[line.index("{") :]).get("maker_sig")
    except Exception:
        pass
    if not maker_sig:
        print("offer carries no maker_sig — cannot countersign")
        await c.aclose()
        return
    accept = f"{SEASON}|accept|{raw}|{did}"
    taker_sig = raw_sig(c, accept)
    msg = f'{{"t":"trade","season":"{SEASON}","terms":{raw},"taker":"{did}","maker_sig":"{maker_sig}","taker_sig":"{taker_sig}"}}'
    print("trade message:", msg[:260])
    if not post:
        print("(dry) not posted")
        await c.aclose()
        return
    await c.signed_post(OUR_ROOM, msg)
    taken[str(tr.get("id"))] = {"seq": seq, "qty": qty, "px": tr.get("px"), "at": int(time.time())}
    save_taken(taken)
    print("posted trade to /r/" + OUR_ROOM)
    await c.aclose()


async def offer(post: bool, qty: float, px: float | None) -> None:
    """Post our own maker offer in our room so a counterparty can take it."""
    c = connector()
    did = c.did_public
    st = await referee_state()
    price = st.get("d-close1-price") or {}
    limits = price.get("limits") or []
    lo, hi = (float(limits[0]), float(limits[1])) if len(limits) == 2 else (0.0, 1e9)
    p = px or float(price.get("applied") or price.get("global") or 0)
    # Target exposure: 10,000 POLF of collateral is ~44 contracts at 225, and the
    # score is a directional PnL, so aim for TARGET and stop. 30 contracts is
    # ~6.800 POLF — half the mint unquoted; keep quoting the full collateral so a
    # seller who crosses can actually build our position.
    TARGET = 100.0
    taken = load_taken()
    posted = sum(float(v.get("qty") or 0) for k, v in taken.items() if str(k).startswith("offer:"))
    if posted >= TARGET:
        print(f"target reached ({posted}/{TARGET} contracts offered) — not posting another offer")
        await c.aclose()
        return
    qty = min(qty, TARGET - posted)
    if not (lo <= p <= hi) or p <= 0:
        print(f"price {p} outside limits [{lo}, {hi}] — refusing")
        await c.aclose()
        return
    terms = {
        "id": f"lumi-{int(time.time()*1000)}",
        "maker": did,
        "px": f"{p:.2f}",
        "qty": f"{qty:.2f}",
        "side": "buy",
        "taker": "any",
        "until": 2556,
    }
    terms_raw = json.dumps(terms, sort_keys=True, separators=(",", ":"))
    maker_sig = raw_sig(c, f"{SEASON}|terms|{terms_raw}")
    msg = (
        '{"t":"offer","season":"' + SEASON + '","terms":' + terms_raw + ',"maker_sig":"' + maker_sig + '"'
        ',"how":"countersign ' + SEASON + '|accept|<termsJson>|<your did:key> then POST {t:trade,season,terms,taker,maker_sig,taker_sig}"}'
    )
    print("offer:", msg[:260])
    if not post:
        print("(dry) not posted")
        await c.aclose()
        return
    await c.signed_post(OUR_ROOM, msg)
    await c.signed_post(ROOM, msg)
    taken[f"offer:{terms['id']}"] = {"qty": qty, "px": terms["px"], "at": int(time.time())}
    save_taken(taken)
    print("posted offer to /r/" + OUR_ROOM + " and /r/" + ROOM)
    await c.aclose()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--register", action="store_true", help="room + owner registration")
    ap.add_argument("--scan", action="store_true", help="referee state and live offers")
    ap.add_argument("--auto", action="store_true", help="take a position per the rule")
    ap.add_argument("--offer", action="store_true", help="post our own maker offer")
    ap.add_argument("--qty", type=float, default=20.0, help="offer size in contracts")
    ap.add_argument("--px", type=float, default=None, help="offer price (default: referee applied)")
    ap.add_argument("--post", action="store_true", help="actually post (default is dry)")
    ap.add_argument("--max-qty", type=float, default=20.0)
    a = ap.parse_args(argv)
    if a.register:
        asyncio.run(register(a.post))
    if a.scan:
        asyncio.run(scan())
    if a.offer:
        asyncio.run(offer(a.post, a.qty, a.px))
    if a.auto:
        asyncio.run(auto(a.post, a.max_qty))
    if not (a.register or a.scan or a.auto or a.offer):
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
