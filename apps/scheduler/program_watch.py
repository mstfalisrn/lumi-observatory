#!/usr/bin/env python3
"""Program watchdog — referee verdicts, official score feeds and room archive, one pass.

It runs a single pass and EXITS; it does not loop, because the caller is a
systemd timer (lumi-program-watch.timer, every 15 minutes). The order is
deliberate:

    1) the room archive is refreshed (apps/tools/archive_rooms.py is imported —
       the same code is not written a second time) and room_archives ledger rows
       are written;
    2) verdicts are extracted from the fresh tape and written to tclk_verdicts;
    3) official feeds (passport/points/kibble/board/leaderboard) are snapshotted
       and one row per source is written to program_scores.

Because the watchdog runs every 15 minutes it refreshes only the verdict rooms
(default tclk-deliveries) and kibble; downloading all 8 rooms (~40 MB) on every
pass would be pointless — that job belongs to the 6-hourly
lumi-archive-rooms.timer.

Why it exists: the repo used to keep none of these three facts — the question
"did the referee pass our work, what is our passport score, what did the room
answer" could only be answered by looking at the live room and reconstructing
the lost chain.

No secrets are written: the DSN/password is never printed anywhere, .env is only
read; the `secret:`/`preimage:` fields in room rows are masked before they are
written to the DB (reveal preimage = escrow claim secret, the same rule as
tclk_frames).

Flags:
    --once        single pass (the default behaviour; the timer calls this)
    --dry         write nothing (neither file nor DB), only report
    --print-ours  print whether our own DID appears in every feed

Usage:
    .venv-earn/bin/python apps/scheduler/program_watch.py --once
    .venv-earn/bin/python apps/scheduler/program_watch.py --once --dry --print-ours
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (str(ROOT), str(ROOT / "packages"), str(ROOT / "apps" / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:  # the watchdog runs outside compose (on the host), so it reads .env itself
    from dotenv import load_dotenv

    load_dotenv(os.environ.get("LUMI_ENV_FILE", str(ROOT / ".env")), override=False)
except Exception:  # pragma: no cover - without .env the environment variables suffice
    pass

import httpx
import psycopg

# Room archive logic lives there; it is imported, not copied.
from archive_rooms import (
    DEFAULT_OUT_DIR,
    archive_path,
    archive_room,
    fetch_export,
    format_summary,
    iter_records,
    mask_secret,
)
from psycopg.types.json import Jsonb

from observability.llm_usage import dsn

# Our agent DID. The kibble score is queried with this DID and this is what we
# look for in the feeds.
OUR_DID = os.environ.get(
    "LUMI_AGENT_DID", "did:key:z6MkAUDITPLACEHOLDERDIDnotarealkey00000000000"
)

# Verdict rows are read from these rooms (more can be added, comma-separated).
VERDICT_ROOMS = tuple(
    x.strip() for x in os.environ.get("LUMI_VERDICT_ROOMS", "tclk-deliveries").split(",") if x.strip()
)

LINE_MAX = 2000
FEED_TIMEOUT_S = float(os.environ.get("LUMI_WATCH_TIMEOUT", "60"))
FEED_MAX_BYTES = int(os.environ.get("LUMI_WATCH_MAX_BYTES", str(16 * 1024 * 1024)))
# Published feeds are recomputed on the program's own clock. Past this age the
# snapshot is news about the publisher, not about us — so the age is stored next
# to every score (see feed_age_hours).
FEED_STALE_HOURS = float(os.environ.get("LUMI_FEED_STALE_HOURS", "6"))

# `0x<contract> <status> ...` — the single shared shape of verdict/frame lines.
HASH_RE = re.compile(r"^\s*(0x[0-9a-fA-F]{6,})\s+(.+)$", re.S)
PAYER_RE = re.compile(r"(?i)\bpayer\b\s*[:=]?\s*(did:key:[1-9A-HJ-NP-Za-km-z]{20,})")
PUNCT = " \t\r\n;.,:!?"


class Feed:
    """Official feed definition: where it is read from, which key finds our row."""

    __slots__ = ("kind", "list_key", "name", "score_key", "url")

    def __init__(self, name: str, url: str, kind: str, list_key: str, score_key: str) -> None:
        self.name = name
        self.url = url
        self.kind = kind  # entries | map | single
        self.list_key = list_key
        self.score_key = score_key

    def url_for(self, did: str) -> str:
        return self.url.format(did=did)


FEEDS: tuple[Feed, ...] = (
    Feed("passport", "https://flop-market.pages.dev/blockrewards/passports.json", "entries", "passports", "score"),
    Feed("points", "https://flopmarkets.com/points.json", "map", "points", "total"),
    Feed("kibble", "https://flop-kibble.onrender.com/api/score?did={did}", "single", "", "score"),
    Feed("board", "https://flopmarkets.com/board/workers.json", "entries", "workers", "receipts"),
    Feed("leaderboard", "https://flopmarkets.com/leaderboard.json", "entries", "leaderboard", "profit"),
)

TABLES = ("tclk_verdicts", "program_scores", "room_archives")


# ── verdict row parsing (pure functions: testable without a DB) ──────────────

def parse_ts(raw: object) -> datetime | None:
    """Convert a room record's ts field to datetime; None when unreadable."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def verdict_of(rest: str) -> str:
    """Verdict from the line's first word: PASS / FAIL / <status> / other."""
    word = (rest or "").strip().split(" ", 1)[0].strip(PUNCT)
    if not word:
        return "other"
    upper = word.upper()
    if upper in ("PASS", "FAIL"):
        return upper
    if word.isalpha() and len(word) <= 16:
        return word.lower()
    return "other"


def verdict_row(room: str, rec: dict) -> dict | None:
    """Convert one room record into a verdict row; a record without a hash returns None."""
    seq = rec.get("seq")
    text = rec.get("text")
    if not isinstance(seq, int) or not isinstance(text, str):
        return None
    match = HASH_RE.match(text)
    if not match:
        return None
    payer = PAYER_RE.search(text)
    return {
        "room": room,
        "seq": seq,
        "ts": parse_ts(rec.get("ts")),
        "contract": match.group(1),
        "verdict": verdict_of(match.group(2)),
        "line": mask_secret(text)[:LINE_MAX],  # secrets masked, 2000 chars truncated
        "payer_did": payer.group(1) if payer else "",
    }


def parse_verdicts(room: str, records) -> list[dict]:
    """Verdict rows from a record stream; deduplicated by (room, seq)."""
    out: dict[int, dict] = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        row = verdict_row(room, rec)
        if row is not None:
            out[row["seq"]] = row
    return [out[seq] for seq in sorted(out)]


# ── official feed snapshot ──────────────────────────────────────────────────

def _as_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def feed_age_hours(as_of: str, now: datetime | None = None) -> float | None:
    """How old a published snapshot is, in hours; None when it carries no stamp.

    When the program's own recompute clock stops, every DID reads as absent for
    the same reason — ours included. Storing the age is what separates "we are
    not in the leaderboard" from "the leaderboard is four days old".
    """
    ts = parse_ts(as_of)
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return max(0.0, ((now or datetime.now(UTC)) - ts).total_seconds() / 3600.0)


def parse_feed(feed: Feed, body: object, raw_len: int = 0) -> dict:
    """Pick our row out of a feed body (no network — pure function)."""
    entry: dict | None = None
    detail: dict | None = None
    count = 0
    as_of = ""
    if isinstance(body, dict):
        as_of = str(body.get("as_of") or "")
        if feed.kind == "entries":
            items = body.get(feed.list_key)
            if isinstance(items, list):
                count = len(items)
                for item in items:
                    if isinstance(item, dict) and item.get("did") == OUR_DID:
                        entry = item
                        break
        elif feed.kind == "map":
            items = body.get(feed.list_key)
            if isinstance(items, dict):
                count = len(items)
                hit = items.get(OUR_DID)
                entry = hit if isinstance(hit, dict) else None
        else:  # single — the body belongs directly to us (kibble /api/score?did=)
            count = 1
            # Kibble may return "found: false" (we are not scored yet); in that
            # case the feed does not name us, but what the feed says is recorded.
            detail = {k: v for k, v in body.items() if isinstance(v, (str, int, float, bool))}
            hit = body.get("did")
            if hit == OUR_DID and body.get("found", True):
                entry = body
    score = _as_number(entry.get(feed.score_key)) if isinstance(entry, dict) else None
    age = feed_age_hours(as_of)
    return {
        "source": feed.name,
        "subject_did": OUR_DID if entry is not None else "",
        "score": score,
        "found": entry is not None,
        "payload": {
            "as_of": as_of,
            "as_of_age_hours": None if age is None else round(age, 2),
            "stale": bool(age is not None and age > FEED_STALE_HOURS),
            "count": count,
            "found": entry is not None,
            "bytes": raw_len,
            "detail": detail,
            # When our row is found only that row is stored: the passport feed
            # is ~3.8 MB, and writing all of it to JSONB every 15 minutes would
            # bloat the database; no information is lost, the row stays at source.
            "entry": entry if isinstance(entry, dict) else None,
        },
    }


def snapshot_feed(feed: Feed, client: httpx.Client | None = None) -> dict:
    """Download the feed and return the snapshot; an error payload on failure."""
    url = feed.url_for(OUR_DID)
    own = client is None
    client = client or httpx.Client(timeout=FEED_TIMEOUT_S, follow_redirects=True)
    try:
        res = client.get(url)
        res.raise_for_status()
        if len(res.content) > FEED_MAX_BYTES:
            raise ValueError(f"body too large: {len(res.content)} bytes")
        body = json.loads(res.text)
        return parse_feed(feed, body, len(res.content))
    except Exception as exc:
        return {
            "source": feed.name,
            "subject_did": "",
            "score": None,
            "found": False,
            "payload": {"error": f"{type(exc).__name__}", "url": url},
        }
    finally:
        if own:
            client.close()


# ── database writes (psycopg; all idempotent via ON CONFLICT) ──────────────

VERDICT_SQL = """
INSERT INTO tclk_verdicts (id, room, seq, ts, contract, verdict, line, payer_did, created_at)
VALUES (gen_random_uuid(), %(room)s, %(seq)s, %(ts)s, %(contract)s, %(verdict)s,
        %(line)s, %(payer_did)s, now())
ON CONFLICT ON CONSTRAINT uq_tclk_verdict_room_seq DO NOTHING
"""

SCORE_SQL = """
INSERT INTO program_scores (id, source, subject_did, score, payload, captured_at)
VALUES (gen_random_uuid(), %(source)s, %(subject_did)s, %(score)s, %(payload)s, %(captured_at)s)
ON CONFLICT ON CONSTRAINT uq_program_score_snapshot
DO UPDATE SET score = EXCLUDED.score, payload = EXCLUDED.payload
"""

ARCHIVE_SQL = """
INSERT INTO room_archives (id, room, first_seq, last_seq, records, bytes, archived_at)
VALUES (gen_random_uuid(), %(room)s, %(first_seq)s, %(last_seq)s, %(records)s, %(bytes)s, %(archived_at)s)
ON CONFLICT ON CONSTRAINT uq_room_archive_room
DO UPDATE SET first_seq = EXCLUDED.first_seq, last_seq = EXCLUDED.last_seq,
              records = EXCLUDED.records, bytes = EXCLUDED.bytes,
              archived_at = EXCLUDED.archived_at
"""


def connect():
    """Live DB connection (on the host POSTGRES_HOST=127.0.0.1, port 5433)."""
    url = dsn()
    if not url:
        raise SystemExit("[error] no DSN: POSTGRES_* or DATABASE_URL is required in .env")
    return psycopg.connect(url)


def check_tables(conn) -> list[str]:
    """Are the new tables there; if not, an error telling the operator what to do."""
    missing: list[str] = []
    with conn.cursor() as cur:
        for name in TABLES:
            cur.execute("SELECT to_regclass(%s::text)", (f"public.{name}",))
            if cur.fetchone()[0] is None:
                missing.append(name)
    return missing


def write_rows(conn, sql: str, rows: list[dict]) -> int:
    """Write rows; thanks to ON CONFLICT a second run does not duplicate them."""
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
        return cur.rowcount


# ── pass ────────────────────────────────────────────────────────────────────

def run_once(
    *,
    dry: bool = False,
    print_ours: bool = False,
    out_dir: str | Path = DEFAULT_OUT_DIR,
    base: str | None = None,
    rooms: tuple[str, ...] | None = None,
) -> dict:
    """Single pass: archive → verdicts → feeds. When dry=True it writes nowhere."""
    verdict_rooms = rooms or VERDICT_ROOMS
    now = datetime.now(UTC)
    captured_at = now.replace(second=0, microsecond=0)  # rounded to the minute -> rewrites stay unique
    # NOTE: report keys are Turkish on purpose — external readers depend on them.
    report: dict = {"dry": dry, "archived": [], "verdicts": [], "published": [], "written": {}}

    # 1) Room archive (same code: apps/tools/archive_rooms.archive_room)
    archive_summaries: list[dict] = []
    for room in (*verdict_rooms, "kibble"):
        summary = archive_room(room, out_dir, base=base, dry=dry)
        archive_summaries.append(summary)
        report["archived"].append(summary)
        print(format_summary(summary), flush=True)

    # 2) Verdicts: local archive first, else live export (read-only)
    verdict_rows: list[dict] = []
    sources: list[str] = []
    for room in verdict_rooms:
        path = archive_path(out_dir, room)
        if path.exists():
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                records = list(iter_records(fh.read()))
            sources.append(f"{room}=archive")
        else:
            try:
                records = list(iter_records(fetch_export(room, base)))
            except Exception as exc:
                records = []
                print(f"[verdict] {room:<20} no source: {type(exc).__name__}: {exc}", flush=True)
            sources.append(f"{room}=live")
        verdict_rows.extend(parse_verdicts(room, records))
    by_verdict: dict[str, int] = {}
    for row in verdict_rows:
        by_verdict[row["verdict"]] = by_verdict.get(row["verdict"], 0) + 1
    report["verdicts"] = verdict_rows
    print(
        f"[verdict] source={' '.join(sources)} lines={len(verdict_rows)} "
        f"distribution={json.dumps(by_verdict, ensure_ascii=False, sort_keys=True)}",
        flush=True,
    )

    # 3) Official feeds
    with httpx.Client(timeout=FEED_TIMEOUT_S, follow_redirects=True) as client:
        snapshots = [snapshot_feed(feed, client) for feed in FEEDS]
    report["published"] = snapshots
    for snap in snapshots:
        mark = "ours" if snap["found"] else "absent"
        print(
            f"[score] {snap['source']:<12} state={mark:<6} subject={snap['subject_did'] or '-':<56} "
            f"score={snap['score'] if snap['score'] is not None else '-'}",
            flush=True,
        )
    if print_ours:
        ours = " ".join(f"{s['source']}={'PRESENT' if s['found'] else 'absent'}" for s in snapshots)
        print(f"[our DID] {OUR_DID} → {ours}", flush=True)

    # A frozen feed is the publisher's news, not ours: report it as such so a
    # zero score is never mistaken for a zero score of OUR making.
    stale = [s for s in snapshots if s["payload"].get("stale")]
    if stale:
        print(
            "[stale] published feeds stopped being recomputed: "
            + ", ".join(f"{s['source']}={s['payload'].get('as_of_age_hours')}h" for s in stale)
            + " — scores below are that old",
            flush=True,
        )

    # 4) Writes (in dry mode no connection is made at all)
    if dry:
        print(
            f"[dry] not writing: tclk_verdicts={len(verdict_rows)} program_scores={len(snapshots)} "
            f"room_archives={len(archive_summaries)}",
            flush=True,
        )
        return report

    conn = connect()
    try:
        missing = check_tables(conn)
        if missing:
            raise SystemExit(
                "[error] missing table: "
                + ", ".join(missing)
                + " — migration not applied (alembic upgrade head)"
            )
        score_rows = [
            {
                "source": s["source"],
                "subject_did": s["subject_did"],
                "score": s["score"],
                "payload": Jsonb(s["payload"]),
                "captured_at": captured_at,
            }
            for s in snapshots
        ]
        archive_rows = [
            {
                "room": a["room"],
                # Turkish keys below are produced by archive_rooms.format_summary
                # and are kept as-is for compatibility with that module.
                "first_seq": a["first_seq"],
                "last_seq": a["last_seq"],
                "records": a["rows"],
                "bytes": a["bytes"],
                "archived_at": now,
            }
            for a in archive_summaries
            if not a["error"]
        ]
        wrote = {
            "tclk_verdicts": write_rows(conn, VERDICT_SQL, verdict_rows),
            "program_scores": write_rows(conn, SCORE_SQL, score_rows),
            "room_archives": write_rows(conn, ARCHIVE_SQL, archive_rows),
        }
        conn.commit()
        report["written"] = wrote
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(
        f"[write] tclk_verdicts={len(verdict_rows)} rows processed, "
        f"program_scores={len(score_rows)} (captured_at={captured_at.isoformat()}), "
        f"room_archives={len(archive_rows)}",
        flush=True,
    )
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="program_watch", description="program feed + room verdict watchdog")
    ap.add_argument("--once", action="store_true", help="single pass (default; no loop)")
    ap.add_argument("--dry", action="store_true", help="write nothing: neither file nor DB")
    ap.add_argument("--print-ours", action="store_true", help="is our own DID present in the feeds")
    ap.add_argument("--rooms", default="", help="verdict rooms (comma-separated); tclk-deliveries when empty")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="archive directory")
    args = ap.parse_args(argv)

    rooms = tuple(x.strip() for x in args.rooms.split(",") if x.strip()) or None
    run_once(dry=args.dry, print_ours=args.print_ours, out_dir=args.out_dir, rooms=rooms)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
