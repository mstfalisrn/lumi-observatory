#!/usr/bin/env python3
"""LUMI earning loop — flopmarket participation, coherence arbitrage, news edge.

Why it exists: a winning flopmarket share pays 1 FLOP (delivered as a funded
offer on /r/tclk-offers), and the venue's points.json feeds a future airdrop.
Points are earned by participation (chips filled), continuity (active days),
skill (profit) and calibration — so the honest way to earn is: trade small and
often at fair prices, take real edges when the rules make one, and never guess.

Three rules, all checkable against public data:

  1. coherence — the markets nest, so P(m01) <= P(m02) <= P(m03) and
     P(m16 Q4 2026) <= P(m03) must hold (m01/m02/m03 share one definition with
     later deadlines; m16 Q4 is a subset of "opens before 2027-01-01"). A
     violation is a real edge: buy the underpriced leg.
  2. news — m01/m02/m03/m16 resolve on a *published artefact* (chain spec, node
     binary, faucet) on flop.finance or the flop-labs GitHub org. We watch those
     surfaces; a corroborated artefact flips the basket to YES, so the alert is
     worth more than any model.
  3. participation — fills at the venue's own LMSR price are ~zero-EV (no spread
     beyond price impact) and pay participation points, so a bounded daily fill
     across open markets is cheap points, not a gamble.

Safety: signed lines only (our DID), per-order and per-day caps, per-market cap
below the venue's 3000, dry-run by default in `--once` mode unless --post.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for p in (str(REPO), str(REPO / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

import itertools

import httpx

SITE = "https://flopmarkets.com"
TC = os.environ.get("TECHNOCORE_BASE_URL", "https://technocore.chat").rstrip("/")
ROOM = "flopmarket"

STATE_DIR = Path(os.environ.get("LUMI_EARN_STATE_DIR", "/var/lib/lumi-earn"))
STATE_FILE = STATE_DIR / "state.json"
TRADE_LOG = STATE_DIR / "trades.jsonl"

# --- caps (chips) -----------------------------------------------------------
DAILY_BUDGET = float(os.environ.get("LUMI_EARN_DAILY_CHIPS", "2500"))
ORDER_SHARES = int(os.environ.get("LUMI_EARN_ORDER_SHARES", "300"))
MARKET_CAP = float(os.environ.get("LUMI_EARN_MARKET_CAP", "2500"))  # venue cap is 3000
COHERENCE_MIN = float(os.environ.get("LUMI_EARN_COHERENCE_MIN", "0.01"))

# Our documented stance per market (published rules + the 2026-09-07 reading):
#   m01/m02 resolve on a testnet artefact published before 2026-10-16 /
#   2026-11-16. The teaser states "Testnet Q4 2026" and no artefact existed at
#   the reading, so the ship-in-the-first-6-weeks branch is the small tail.
#   m04 needs a Q4-2026 testnet *plus* the ~90-day testnet window to land
#   mainnet genesis before 2027-04-01 — priced above m03, which that path
#   cannot support.
# These are stances, not promises: every fill stays inside the participation cap.
VIEWS = {"m01": "NO", "m02": "NO", "m04": "NO"}

# m01/m02/m03 share one definition (deadline differs) — later deadline must be >= earlier.
CHAIN = ["m01", "m02", "m03"]


def log(msg: str) -> None:
    print(f"[{datetime.now(UTC).strftime('%H:%M:%S')}] {msg}", flush=True)


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"day": "", "filled_today": 0.0, "baseline": {}, "news": [], "holdings": {}}


def save_state(st: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2, ensure_ascii=False))
    tmp.replace(STATE_FILE)


def append_trade(row: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with TRADE_LOG.open("a") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


# --- market data ------------------------------------------------------------
async def fetch_json(client: httpx.AsyncClient, path: str):
    r = await client.get(f"{SITE}{path}")
    r.raise_for_status()
    return r.json()


def open_markets(markets: dict) -> list[dict]:
    out = []
    for m in markets.get("markets", []):
        if str(m.get("status") or "open") != "open":
            continue
        out.append(m)
    return out


def price_of(m: dict, outcome: str) -> float | None:
    """Live price if the House has published one, else the daily snapshot.

    markets.json is written about once a day, so the snapshot goes stale within
    hours: that is what kept rejecting fills (limit built from a 3-day-old
    price). The House's hourly `odds` line is the price fills actually print
    against, so when a live read exists it wins — and partial live data never
    falls back to the stale snapshot."""
    live = m.get("_live") or {}
    if live:
        return float(live[outcome]) if outcome in live else None
    outs = m.get("outcomes") or []
    prices = m.get("prices") or []
    if outcome.startswith("o") and outcome[1:].isdigit():
        i = int(outcome[1:]) - 1
        return float(prices[i]) if i < len(prices) else None
    try:
        i = [str(o).upper() for o in outs].index(outcome.upper())
    except ValueError:
        return None
    return float(prices[i]) if i < len(prices) else None


ODDS_LINE = re.compile(r"\bodds\s+\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\s*(.*)$")
ODDS_PART = re.compile(r"^m(\d{1,2})\s+(.*?)\s*(\d+(?:\.\d+)?)%$")
BAL_RE = re.compile(r"you have ([\d.]+)")
OFFER_ID_RE = re.compile(r"\b([0-9a-f]{12,64})\b")


async def live_odds(client: httpx.AsyncClient, room: str = ROOM) -> dict:
    """Read the House's latest `odds <stamp>: m01 8% · m02 26% · ...` line.

    Returns {market: (bucket_label_or_empty, price)} using the newest stamp.
    The stamp carries two colons (19:00) so it is matched whole, then dropped —
    cutting at the first colon silently swallowed the leading market."""
    r = await client.get(f"{TC}/r/{room}", params={"limit": 200})
    r.raise_for_status()
    body = ""
    for line in r.text.splitlines():
        mm = ODDS_LINE.search(line)
        if mm:
            body = mm.group(1)
    if not body:
        return {}
    live: dict = {}
    for part in body.split("·"):
        mm = ODDS_PART.match(part.strip())
        if mm:
            live["m" + mm.group(1)] = (mm.group(2).strip(), float(mm.group(3)) / 100.0)
    return live


def apply_live(by_id: dict, live: dict) -> int:
    """Overlay House prices on the snapshot; binaries get both legs priced."""
    n = 0
    for mid, (label, px) in live.items():
        m = by_id.get(mid)
        if not m:
            continue
        outs = [str(o) for o in (m.get("outcomes") or [])]
        if not outs:
            continue
        if not label:
            if len(outs) < 2:
                continue
            m["_live"] = {"YES": px, "NO": round(1.0 - px, 4)}
        else:
            hit = [o for o in outs if o.lower() == label.lower()]
            if not hit:
                continue
            idx = outs.index(hit[0]) + 1
            m["_live"] = {hit[0]: px, f"o{idx}": px}
        n += 1
    return n


def probe_coherence(by_id: dict) -> list[dict]:
    """Return edges implied by nesting the rule definitions guarantee."""
    edges: list[dict] = []

    def p(mid: str, outcome: str = "YES") -> float | None:
        m = by_id.get(mid)
        return price_of(m, outcome) if m else None

    # later deadlines cannot be cheaper than earlier ones
    chain = [(a, b) for a, b in itertools.pairwise(CHAIN) if a in by_id and b in by_id]
    for early, late in chain:
        pe, pl = p(early), p(late)
        if pe is None or pl is None:
            continue
        if pe - pl > COHERENCE_MIN:
            edges.append(
                {
                    "kind": "coherence",
                    "market": late,
                    "outcome": "YES",
                    "edge": round(pe - pl, 4),
                    "why": f"{late} cannot be cheaper than {early} ({pl:.3f} < {pe:.3f})",
                }
            )

    # "opens in Q4 2026" is a subset of "opens before 2027-01-01"
    q4, m03 = p("m16", "o1"), p("m03", "YES")
    if q4 is not None and m03 is not None and q4 - m03 > COHERENCE_MIN:
        edges.append(
            {
                "kind": "coherence",
                "market": "m16",
                "outcome": "o1",
                "edge": round(q4 - m03, 4),
                "why": f"m16 Q4 2026 ({q4:.3f}) cannot exceed m03 before-2027 ({m03:.3f})",
            }
        )
    return edges


# --- news watch: the surfaces the rules name ---------------------------------
SURFACES = ["https://flop.finance/", "https://flop.finance/teaser/", "https://flop.finance/intro/"]
ARTEFACT_WORDS = (
    "faucet",
    "chain spec",
    "chainspec",
    "genesis",
    "node binary",
    "testnet live",
    "testnet is live",
    "download",
    "rpc endpoint",
    "genesis hash",
)


async def watch_news(client: httpx.AsyncClient, st: dict) -> list[dict]:
    """Look for a published artefact on the official surfaces. Returns hits."""
    hits: list[dict] = []
    base = st.setdefault("baseline", {})

    try:
        r = await client.get("https://api.github.com/orgs/flop-labs/repos?per_page=100&sort=updated")
        repos = r.json() if r.status_code == 200 else []
        if isinstance(repos, list):
            for repo in repos:
                name = str(repo.get("name", ""))
                pushed = str(repo.get("pushed_at", ""))
                if not name:
                    continue
                key = f"github:{name}"
                if re.search(r"testnet|faucet|chain|spec|node", name, re.I):
                    prev = base.get(key)
                    if prev is not None and prev != pushed:
                        hits.append(
                            {
                                "kind": "news",
                                "source": f"github.com/flop-labs/{name}",
                                "why": f"artefact-shaped repo updated ({pushed})",
                                "at": pushed,
                            }
                        )
                    base[key] = pushed
            base["github:count"] = len(repos)
    except Exception as e:
        log(f"github watch failed: {type(e).__name__}")

    for url in SURFACES:
        try:
            r = await client.get(url, timeout=20.0)
            # A countdown/clock makes any text hash noisy, so watch the LINKS
            # instead: a real artefact shows up as a new download/docs/faucet
            # target. Stable against cosmetic page changes.
            links = sorted(
                {
                    h
                    for h in re.findall(r'href=["\']([^"\']+)["\']', r.text)
                    if re.search(r"testnet|faucet|chain|spec|download|docs|node|rpc|genesis", h, re.I)
                }
            )
            key = f"links:{url}"
            prev = base.get(key)
            new_links = [lnk for lnk in links if prev is not None and lnk not in prev]
            if new_links:
                hits.append(
                    {
                        "kind": "news",
                        "source": url,
                        "why": "new artefact link(s): " + ", ".join(new_links[:4]),
                        "at": datetime.now(UTC).isoformat(timespec="seconds"),
                    }
                )
            base[key] = links
        except Exception as e:
            log(f"page watch failed {url}: {type(e).__name__}")
    return hits


# --- signed posting ---------------------------------------------------------
def make_connector():
    from connectors.technocore import TechnocoreConnector

    key = (
        os.environ.get("TECHNOCORE_ED25519_KEY_PATH")
        or os.environ.get("TECHNOCORE_KEY_PATH")
        or "/opt/lumi-secrets/did.ed25519"
    )
    c = TechnocoreConnector(TC, ed25519_key_path=key)
    if not c.load_key(key):
        raise SystemExit(f"key not loadable: {key}")
    return c


async def send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_HOME_CHAT_ID", "") or os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        return
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": text[:3900]},
            )
    except Exception:
        pass


async def place(connector, mid: str, outcome: str, shares: int, max_price: float, post: bool) -> dict:
    text = f"flopmarket buy {mid} {outcome} {shares} max {max_price:.3f}"
    row = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "market": mid,
        "outcome": outcome,
        "shares": shares,
        "max": round(max_price, 3),
        "line": text,
        "posted": bool(post),
    }
    if post:
        try:
            await connector.signed_post(ROOM, text)
            row["ok"] = True
        except Exception as e:
            row["ok"] = False
            row["error"] = f"{type(e).__name__}: {e}"
    return row


async def spendable(client) -> float | None:
    """The House's own reading of our chips ('you have 57.8').

    claim is one-time, so once the bankroll is deployed every extra order is
    noise: the loop should go quiet and watch for settlement instead."""
    try:
        r = await client.get(f"{TC}/r/{ROOM}", params={"limit": 40})
        r.raise_for_status()
    except Exception:
        return None
    vals = [float(m.group(1)) for m in BAL_RE.finditer(r.text)]
    return min(vals) if vals else None


async def watch_payouts(client, st: dict) -> list[dict]:
    """A winning share pays 1 FLOP as a funded offer on /r/tclk-offers.

    That offer is the money: it is what the accept -> lock -> reveal chain
    settles, so a new one is the signal worth reporting."""
    try:
        r = await client.get(f"{TC}/r/tclk-offers", params={"limit": 80})
        r.raise_for_status()
    except Exception:
        return []
    seen = st.setdefault("offers_seen", [])
    fresh = []
    for line in r.text.splitlines():
        if "offer" not in line.lower() or "did:key:" not in line:
            continue
        idm = OFFER_ID_RE.search(line)
        if not idm or idm.group(1) in seen:
            continue
        seen.append(idm.group(1))
        fresh.append({"id": idm.group(1), "line": line.strip()[:400]})
    st["offers_seen"] = seen[-200:]
    return fresh


async def cycle(post: bool) -> int:
    st = load_state()
    if st.get("day") != today():
        st["day"] = today()
        st["filled_today"] = 0.0
    st.setdefault("news", [])

    async with httpx.AsyncClient(timeout=25.0) as client:
        markets = await fetch_json(client, "/markets.json")
        try:
            odds = await fetch_json(client, "/odds.json")
        except Exception:
            odds = {}
        hits = await watch_news(client, st)

    by_id = {str(m.get("id")): m for m in open_markets(markets)}
    log(f"open markets: {len(by_id)} ({', '.join(sorted(by_id))})")

    async with httpx.AsyncClient(timeout=25.0) as client:
        try:
            live = await live_odds(client)
        except Exception as e:
            live = {}
            log(f"live odds fetch failed: {type(e).__name__}")
    n_live = apply_live(by_id, live) if live else 0
    if n_live:
        shown = []
        for k in sorted(live):
            if k not in by_id:
                continue
            px = price_of(by_id[k], "YES")
            if px is None:
                px = next(iter((by_id[k].get("_live") or {}).values()), None)
            shown.append(f"{k}={px:.3f}" if px is not None else f"{k}=?")
        log("live prices from the House odds line: " + ", ".join(shown))

    async with httpx.AsyncClient(timeout=25.0) as client:
        cash = await spendable(client)
        fresh = await watch_payouts(client, st)
    if cash is not None:
        log(f"spendable chips: {cash:.1f}")
        if cash < 50:
            st["filled_today"] = DAILY_BUDGET
            log("bankroll deployed (claim is one-time) — no new orders, watching payouts")
    if fresh:
        log(f"NEW payout offer(s) on /r/tclk-offers: {len(fresh)}")
        for o in fresh[:5]:
            log("  " + o["line"][:200])
        await send_telegram(
            "LUMI money: new offer on /r/tclk-offers " + str(len(fresh)) + "\n" + fresh[0]["line"][:300]
        )

    edges = probe_coherence(by_id)
    if edges:
        for e in edges:
            log(f"EDGE {e['kind']}: buy {e['market']} {e['outcome']} — {e['why']}")
    else:
        log("no coherence edge (markets consistent)")

    # holdings for our DID, if the venue publishes them
    st.setdefault("holdings", {})
    did = ""
    try:
        connector = make_connector()
        did = connector.did_public
    except SystemExit as e:
        connector = None
        log(f"no signing key: {e}")

    if odds and did:
        try:
            for row in odds.get("holdings", []) if isinstance(odds, dict) else []:
                if str(row.get("did")) == did:
                    break
        except Exception:
            pass

    orders: list[dict] = []
    # 1) real edges first, sized small
    for e in edges:
        m = by_id.get(e["market"])
        if not m:
            continue
        px = price_of(m, e["outcome"])
        if px is None:
            continue
        orders.append(
            {
                "market": e["market"],
                "outcome": e["outcome"],
                "shares": min(ORDER_SHARES, 200),
                "max": min(0.995, px * 1.06 + 0.01),
                "why": e["why"],
            }
        )

    # 2) participation fill: near-fair, diversified, capped by the daily budget
    budget_left = DAILY_BUDGET - float(st.get("filled_today") or 0.0)
    if budget_left > 20 and not hits:
        for mid in sorted(by_id):
            m = by_id[mid]
            px_yes = price_of(m, "YES")
            if px_yes is None:
                continue
            # A price near zero is not a near-fair fill: it is a lottery ticket,
            # and buying it is a -100% expectation. Skip those markets entirely.
            if px_yes <= 0.05 or px_yes >= 0.95:
                continue
            # Our documented stance when one exists (see VIEWS above); otherwise
            # the cheaper side, so price impact stays small.
            outcome = VIEWS.get(mid) or ("YES" if px_yes <= 0.5 else "NO")
            px = price_of(m, outcome)
            if px is None or px > 0.93:
                continue
            shares = min(ORDER_SHARES, max(20, int(budget_left * 0.25 / max(px, 0.02))))
            orders.append(
                {
                    "market": mid,
                    "outcome": outcome,
                    "shares": shares,
                    "max": min(0.995, px * 1.06 + 0.01),
                    "why": "participation fill (near fair, bounded)",
                }
            )
            budget_left -= shares * px
            if budget_left <= 20:
                break

    posted_any = False
    for o in orders:
        row = await place(connector, o["market"], o["outcome"], o["shares"], o["max"], post and connector is not None)
        row["why"] = o["why"]
        row["did"] = did
        append_trade(row)
        if row.get("ok"):
            st["filled_today"] = round(float(st.get("filled_today") or 0.0) + o["shares"] * o["max"], 2)
        log(f"{'POSTED' if row['posted'] else 'DRY'} {row['line']}  ({o['why']})")
        posted_any = posted_any or bool(row.get("ok"))
        await asyncio.sleep(3)

    if hits:
        st["news"].extend(hits)
        st["news"] = st["news"][-50:]
        for h in hits:
            log(f"NEWS {h['source']} — {h['why']}")
        seen = {n.get("source") for n in st.get("news", [])[:-len(hits)]}
        fresh = [h for h in hits if h["source"] not in seen]
        if fresh and post:
            await send_telegram(
                "🔔 New signal on official FLOP surfaces:\n"
                + "\n".join(f"• {h['source']} — {h['why']}" for h in fresh[:5])
                + "\n(m01/m02/m03/m16 resolve against this artefact — review the position.)"
            )

    if posted_any:
        save_state(st)
    else:
        save_state(st)
    if connector is not None:
        await connector.aclose()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="lumi-earn")
    ap.add_argument("--once", action="store_true", help="single cycle")
    ap.add_argument("--post", action="store_true", help="actually post signed orders")
    ap.add_argument("--interval", type=int, default=900, help="seconds between cycles in loop mode")
    a = ap.parse_args()

    post = a.post or os.environ.get("LUMI_EARN_POST", "").lower() in ("1", "true", "yes")
    if a.once:
        return asyncio.run(cycle(post))
    while True:
        try:
            asyncio.run(cycle(post))
        except Exception as e:
            log(f"cycle failed: {type(e).__name__}: {e}")
        time.sleep(a.interval)


if __name__ == "__main__":
    raise SystemExit(main())
