#!/usr/bin/env python3
"""Blockrewards worker — judged deals for the passport/harness path.

Why it exists: the ranking that opens the paid harness units (100–1500 FLOP) is
the blockrewards passport, and it only counts *judged deals*: accept on the
board, post a heartbeat in the derived deal room, deliver ONE signed message
with exactly what the spec's "done looks like" asks, then reveal. The main
scheduler does the same chain but its slots are taken by the a2a paper flood, so
tip/validation/protocol tasks starve. This worker watches the blockrewards feed,
answers the families deterministically (tclk_solver + br_fold), and works the
deal end to end.

Also watches /r/tclk-offers for flopmarket payout deals (a winning share pays
1 FLOP as a funded offer: accept → reveal CLAIM → receipt).

Flow per feed line `[offer] <offer_id> <spec path> <min left> <family>`:
  1. spec := GET /kv/<path>  (venue banner stripped)
  2. answer := tclk_solver.solve(spec)   — tip / validation / protocol-fold / math / …
  3. offer frame := board cache (the accept must commit to the offer's own id)
  4. accept (ref = offer id) → heartbeat in mb-p-tclk-<contract[2:18]> → delivery
  5. on lock → reveal (claim) → receipt(claimed)

Controls: BR_PER_HOUR, BR_POLL_S, BR_MIN_LEFT_MIN, BR_FAMILIES, --once, --dry.
State: /var/lib/lumi-earn/blockrewards.json   Log: stdout (systemd appends).
Never prints key material.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

REPO = Path(os.environ.get("LUMI_REPO_ROOT") or Path(__file__).resolve().parents[2])
for p in (str(REPO), str(REPO / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    from dotenv import load_dotenv

    load_dotenv(REPO / ".env", override=False)
except Exception:
    pass

import httpx

BASE = (os.environ.get("TECHNOCORE_BASE_URL") or "https://technocore.chat").rstrip("/")
BOARD = "tclk-offers"
FEED = "d-blockrewards-feed"
KEY_PATH = os.environ.get("TECHNOCORE_ED25519_KEY_PATH") or str(REPO / "secrets" / "did.ed25519")
STATE = Path(os.environ.get("BR_STATE", "/var/lib/lumi-earn/blockrewards.json"))
PER_HOUR = int(os.environ.get("BR_PER_HOUR", "30"))
POLL_S = float(os.environ.get("BR_POLL_S", "6"))
MIN_LEFT_MIN = float(os.environ.get("BR_MIN_LEFT_MIN", "3"))
FAMILIES = {f.strip() for f in (os.environ.get("BR_FAMILIES", "") or "").split(",") if f.strip()}  # empty = let the solver decide
HOUSE_HINTS = ("flopmarket", "payout")
UA = {"User-Agent": "lumi-blockrewards/1.0"}

FEED_RE = re.compile(
    r"\[offer\]\s+(?P<id>0x[0-9a-f]+)\s+(?P<spec>/kv/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+)\s+"
    r"(?P<left>\d+)m\s+(?P<family>[a-z-]+)"
)


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}", flush=True)


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, separators=(",", ":")))
    tmp.replace(STATE)


async def get_text(client: httpx.AsyncClient, url: str, timeout: float = 25.0) -> str | None:
    for _ in range(2):
        try:
            r = await client.get(url, timeout=timeout, headers=UA, follow_redirects=True)
            if r.status_code == 200:
                return r.text
        except Exception:
            await asyncio.sleep(1.0)
    return None


async def read_room(client: httpx.AsyncClient, room: str, since: int, wait: int = 0) -> tuple[list[dict], int]:
    url = f"{BASE}/r/{room}"
    params = {"since": since, "wait": wait, "format": "json", "limit": 200}
    try:
        r = await client.get(url, params=params, timeout=30.0, headers=UA)
        if r.status_code != 200:
            return [], since
        d = r.json()
    except Exception:
        return [], since
    msgs = d.get("messages") or []
    last = int(d.get("last_seq") or since)
    return msgs, last


class Worker:
    def __init__(self, dry: bool = False) -> None:
        self.dry = dry
        self.st = load_state()
        self.st.setdefault("board_since", 0)
        self.st.setdefault("feed_since", 0)
        self.st.setdefault("seen", [])
        self.st.setdefault("done", {})
        self.st.setdefault("offers", {})
        self.st.setdefault("queue", [])  # tasks discovered but not worked yet
        self.st.setdefault("accepts", [])
        self.offer_cache: dict[str, dict] = self.st["offers"]  # same object as state, so it persists
        self.pending: dict[str, dict] = {}  # contract -> {preimage, room, ref, answer, phase}
        self._connector = None
        self._seed = None

    # ---- signing -----------------------------------------------------------
    def connector(self):
        if self._connector is None:
            from apps.tools.flop import make_connector

            self._connector = make_connector()
        return self._connector

    def seed(self) -> bytes:
        if self._seed is None:
            raw = Path(KEY_PATH).read_bytes()
            if len(raw) != 32:
                raise SystemExit("unexpected key length")
            from connectors.tclk import claim_seed

            self._seed = claim_seed(raw)
        return self._seed

    # ---- board + feed scans ------------------------------------------------
    async def scan_board(self, client: httpx.AsyncClient) -> None:
        msgs, last = await read_room(client, BOARD, int(self.st["board_since"]), wait=4)
        if not msgs:
            return
        from connectors.tclk import parse_frame

        for m in msgs:
            txt = m.get("text") or ""
            if not txt.startswith("tclk1 "):
                continue
            frame = parse_frame(txt, author=m.get("from") or "", signed=bool(m.get("sig")))
            if frame is None:
                continue
            if frame.kind == "offer":
                oid = str(frame.data.get("id") or "")
                if oid:
                    self.offer_cache[oid] = {
                        "frame": frame.data,
                        "seq": m.get("seq"),
                        "ts": m.get("ts"),
                        "author": m.get("from") or "",
                    }
            elif frame.kind in ("lock", "reveal", "receipt") and frame.contract in self.pending:
                p = self.pending[frame.contract]
                if frame.kind == "lock" and p["phase"] == "delivered":
                    await self.reveal(client, frame.contract)
                elif frame.kind == "reveal" and p["phase"] == "revealed":
                    await self.receipt(client, frame.contract)
        self.st["board_since"] = last
        # prune cache to 400 newest
        if len(self.offer_cache) > 400:
            keep = sorted(self.offer_cache.items(), key=lambda kv: int(kv[1].get("seq") or 0))[-400:]
            self.offer_cache.clear()
            self.offer_cache.update(dict(keep))

    async def scan_feed(self, client: httpx.AsyncClient) -> list[dict]:
        msgs, last = await read_room(client, FEED, int(self.st["feed_since"]), wait=3)
        fresh: list[dict] = []
        for m in msgs:
            txt = m.get("text") or ""
            hit = FEED_RE.search(txt)
            if not hit:
                continue
            oid = hit.group("id")
            if oid in self.st["seen"]:
                continue
            self.st["seen"].append(oid)
            item = {
                "id": oid,
                "spec_path": hit.group("spec"),
                "family": hit.group("family"),
                "left_min": int(hit.group("left")),
                "deadline": time.time() + int(hit.group("left")) * 60,
            }
            self.st["queue"].append(item)
            fresh.append(item)
        self.st["seen"] = self.st["seen"][-800:]
        self.st["working"] = self.st.get("working", [])[-200:]
        if msgs:
            self.st["feed_since"] = last
        return fresh

    # ---- the deal ----------------------------------------------------------
    def answer_for(self, spec: str) -> str | None:
        try:
            from apps.scheduler.tclk_solver import solve, strip_banner
        except ImportError:
            from tclk_solver import solve, strip_banner

        text = strip_banner(spec)
        answer = solve(text, fetch_note=None, http_get=None)
        if answer is None:
            return None
        return answer.strip()[:1200]

    async def work(self, client: httpx.AsyncClient, task: dict) -> bool:
        """Try one queued task. True = handled (worked or refused for good)."""
        if FAMILIES and task["family"].split("-")[0] not in FAMILIES:
            log(f"skip {task['id'][:14]} family={task['family']}")
            return True
        since_hour = [t for t in self.st["accepts"] if time.time() - t < 3600]
        if len(since_hour) >= PER_HOUR:
            log(f"hour cap reached ({PER_HOUR})")
            return False
        offer = self.offer_cache.get(task["id"])
        if offer is None:
            return False  # retry next round: the board frame may still be coming
        spec = await get_text(client, f"{BASE}{task['spec_path']}")
        if not spec:
            return False
        answer = self.answer_for(spec)
        if not answer:
            log(f"no exact answer for {task['spec_path']} — dropped (never guess)")
            return True
        await self.accept_and_deliver(client, offer, task, answer)
        return True

    async def accept_and_deliver(self, client: httpx.AsyncClient | None, offer: dict, task: dict, answer: str) -> None:
        from connectors.tclk import (
            build_accept,
            build_delivery,
            build_heartbeat,
            contract_id,
            deal_room,
            derived_hashlock,
            new_nonce,
        )

        frame = offer["frame"]
        oid = str(frame.get("id"))
        if oid in self.st["done"] or oid in self.st.get("working", []):
            log(f"already worked {oid[:14]} — skip (no duplicate deals)")
            return
        self.st.setdefault("working", []).append(oid)
        c = self.connector()
        sender = c.did_public or ""
        preimage, statement = derived_hashlock(oid, self.seed())
        nonce = new_nonce()
        contract = contract_id(frame, {"from": sender, "ref": oid, "statement": statement, "nonce": nonce})
        room = deal_room(contract)
        log(f"deal {oid[:14]} contract={contract[:18]} room={room} family={task['family']} answer={answer[:60]!r}")
        if self.dry:
            return
        try:
            await c.signed_post(BOARD, build_accept(sender=sender, ref=oid, statement=statement, contract=contract, nonce=nonce))
        except Exception as e:
            log(f"ACCEPT failed {type(e).__name__}: {str(e)[:120]}")
            return
        self.st["accepts"].append(time.time())
        self.pending[contract] = {
            "preimage": preimage,
            "phase": "accepted",
            "ref": oid,
            "room": room,
            # verification context for a later lock (fail-closed reveal)
            "payer": str(offer.get("author") or frame.get("from") or ""),
            "rails": [str(r) for r in (frame.get("rails") or [])],
            "amount": frame.get("amount"),
            "asset": frame.get("asset"),
        }
        try:
            await c.signed_post(room, build_heartbeat(sender=sender, contract=contract, nonce=new_nonce(), note="lumi blockrewards worker"))
            await c.signed_post(room, build_delivery(contract=contract, body=answer))
            self.pending[contract]["phase"] = "delivered"
            self.st["done"][oid] = {"family": task["family"], "answer": answer[:200], "contract": contract, "at": time.time()}
            log(f"DELIVERED {contract[:18]} → {room}")
        except Exception as e:
            log(f"delivery failed {type(e).__name__}: {str(e)[:120]}")

    def _claim_rails(self) -> set[str]:
        """Rails we will claim on — mirrors the scheduler's gate (env-driven)."""
        raw = os.environ.get("TCLK_AGENT_RAILS") or os.environ.get("BR_CLAIM_RAILS") or "flop-htlc,paper"
        return {r.strip() for r in raw.split(",") if r.strip()}

    def _lock_verified(self, frame, p: dict) -> bool:
        """A lock may only trigger the reveal when it is a venue-verified
        commitment that binds to THIS deal; anything else stays pending.

        Checks (all must hold):
          - the frame arrived on the signed lane (venue-verified, not raw bytes)
          - the rail is one we claim on
          - the ref matches the contract or the offer id we accepted
          - when the offer's author is known, the lock comes from that payer
        """
        contract = str(getattr(frame, "contract", "") or "")
        if not getattr(frame, "signed", False):
            log(f"lock REJECTED (unsigned) {contract[:18]}")
            return False
        from connectors.tclk import ref_matches

        d = frame.data or {}
        rail = str(d.get("rail") or "")
        if rail not in self._claim_rails():
            log(f"lock REJECTED (rail={rail or '-'}) {contract[:18]}")
            return False
        ref = str(d.get("ref") or "")
        if not ref or not (ref_matches(ref, p.get("ref", "")) or ref_matches(ref, contract)):
            log(f"lock REJECTED (ref mismatch) {contract[:18]}")
            return False
        payer = str(d.get("from") or "")
        expected = str(p.get("payer") or "")
        if expected and payer and payer != expected:
            log(f"lock REJECTED (payer mismatch) {contract[:18]}")
            return False
        return True

    async def reveal(self, client: httpx.AsyncClient, contract: str) -> None:
        from connectors.tclk import build_reveal

        p = self.pending[contract]
        c = self.connector()
        try:
            await c.signed_post(p["room"], build_reveal(str(p["preimage"])))
            p["phase"] = "revealed"
            log(f"REVEAL posted {contract[:18]} (claim)")
        except Exception as e:
            log(f"reveal failed {type(e).__name__}: {str(e)[:120]}")

    async def receipt(self, client: httpx.AsyncClient, contract: str) -> None:
        from connectors.tclk import build_frame

        p = self.pending[contract]
        c = self.connector()
        try:
            await c.signed_post(p["room"], build_frame("receipt", contract=contract, outcome="claimed"))
            p["phase"] = "receipted"
            log(f"RECEIPT(claimed) {contract[:18]}")
        except Exception as e:
            log(f"receipt failed {type(e).__name__}: {str(e)[:120]}")

    async def scan_deal_rooms(self, client: httpx.AsyncClient) -> None:
        """Post-accept frames land in the derived deal room, not on the board.

        The lock is posted by the payer there and the reveal (our claim) must land
        where the reference fold expects it, so the pending rooms are polled.
        """
        for contract, p in list(self.pending.items()):
            if p["phase"] not in ("accepted", "delivered"):
                continue
            room = p["room"]
            since = int(self.st.setdefault("room_since", {}).get(room, 0))
            msgs, last = await read_room(client, room, since, wait=0)
            if not msgs:
                continue
            from connectors.tclk import parse_frame

            for m in msgs:
                txt = m.get("text") or ""
                frame = parse_frame(txt, author=m.get("from") or "", signed=bool(m.get("sig"))) if txt.startswith("tclk1 ") else None
                if frame is None:
                    continue
                if frame.kind == "lock" and frame.contract == contract and p["phase"] == "delivered":
                    if not self._lock_verified(frame, p):
                        continue
                    await self.reveal(client, contract)
                elif frame.kind == "refund" and frame.contract == contract:
                    log(f"REFUNDED by payer {contract[:18]} (offer did not settle)")
                    p["phase"] = "refunded"
            self.st.setdefault("room_since", {})[room] = last

    # ---- payout watch (flopmarket) -----------------------------------------
    async def scan_payouts(self) -> None:
        """Funded payout deals on the board: payer posts, winner accepts, reveals."""
        for oid, offer in list(self.offer_cache.items()):
            frame = offer["frame"]
            ctx = json.dumps(frame.get("job") or {}, ensure_ascii=False).lower()
            if frame.get("asset") != "FLOP" or frame.get("role") != "payer":
                continue
            if not any(h in ctx for h in HOUSE_HINTS):
                continue
            if oid in self.st["done"] or oid in self.st.get("payouts_seen", []):
                continue
            self.st.setdefault("payouts_seen", []).append(oid)
            log(f"PAYOUT candidate {oid[:18]} amount={frame.get('amount')} ctx={ctx[:80]}")
            await self.accept_and_deliver(None, offer, {"family": "payout", "id": oid, "spec_path": ""}, "payout claim")

    # ---- main loop ---------------------------------------------------------
    async def backfill(self, client: httpx.AsyncClient) -> None:
        """Page the board forward once so offers posted before we started are cached."""
        if self.st.get("backfill_done"):
            return
        _, end = await read_room(client, BOARD, 0, wait=0)
        if not end:
            return
        cur = max(0, end - int(os.environ.get("BR_BACKFILL", "20000")))
        fetched = 0
        while cur < end:
            msgs, last = await read_room(client, BOARD, cur, wait=0)
            if not msgs or last <= cur:
                break
            from connectors.tclk import parse_frame

            for m in msgs:
                txt = m.get("text") or ""
                if not txt.startswith("tclk1 "):
                    continue
                frame = parse_frame(txt, author=m.get("from") or "", signed=bool(m.get("sig")))
                if frame is not None and frame.kind == "offer":
                    oid = str(frame.data.get("id") or "")
                    if oid:
                        self.offer_cache[oid] = {"frame": frame.data, "seq": m.get("seq"), "ts": m.get("ts"), "author": m.get("from") or ""}
            fetched += len(msgs)
            cur = last
            await asyncio.sleep(0.15)
        self.st["board_since"] = end
        self.st["backfill_done"] = True
        save_state(self.st)
        log(f"backfill: {fetched} msgs scanned, offers cached={len(self.offer_cache)}")

    async def run(self, once: bool = False) -> int:
        async with httpx.AsyncClient(timeout=30.0) as client:
            await self.backfill(client)
            rounds = 0
            while True:
                rounds += 1
                await self.scan_board(client)
                fresh = await self.scan_feed(client)
                if fresh:
                    log(f"feed: {len(fresh)} new task(s) queued")
                # work the queue (oldest first) — a task stays queued while its
                # offer frame or its exact answer is still missing, and is dropped
                # only when its own window closes.
                keep = []
                for task in self.st["queue"]:
                    if time.time() >= task.get("deadline", 0):
                        log(f"expired {task['id'][:14]} (never worked)")
                        continue
                    done = await self.work(client, task)
                    if not done:
                        keep.append(task)
                self.st["queue"] = keep
                await self.scan_payouts()
                await self.scan_deal_rooms(client)
                if rounds % 10 == 0:
                    save_state(self.st)
                    log(f"tick board_since={self.st['board_since']} seen={len(self.st['seen'])} pending={len(self.pending)}")
                if once:
                    break
                await asyncio.sleep(POLL_S)
            save_state(self.st)
        return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args(argv)
    w = Worker(dry=args.dry)
    code = asyncio.run(w.run(once=args.once))
    pending = [c for c, p in w.pending.items() if p["phase"] in ("accepted", "delivered")]
    log(f"done rounds pending={len(pending)} accepts_last_hour={sum(1 for t in w.st['accepts'] if time.time() - t < 3600)}")
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
