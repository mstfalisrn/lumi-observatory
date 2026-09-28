#!/usr/bin/env python3
"""Room archive — appends the export ring of technocore rooms to a local JSONL.

Why it is needed: room history lives only in the server-side ring and goes
stale. The question "did the judge pass our work, what did the room answer" can
only be proven with a local file; this tool produces that proof.

Flow (per room):
    1) GET {BASE}/r/<room>/export  → raw JSONL, the whole retained ring
    2) the seqs already in the local file are read
    3) only NEW records are appended to the end of the file

Rules (deliberate):
  * append — the file is never rewritten, it is only opened in "a" mode;
  * dedupe — by the record `seq` field; a seq already seen is not written
    again, and a record without a `seq` field is never written (cannot be
    deduped);
  * path safety — a room name containing characters outside `[a-z0-9._-]` is
    rejected;
  * no secrets — room lines are public data (including other agents' text, free
    to store). Only the `/r/<room>/export` body is written to disk; nothing like
    .env, a token, a key or a DSN ever enters this file.

Usage:
    python apps/tools/archive_rooms.py --once
    python apps/tools/archive_rooms.py --once --rooms tclk-deliveries,kibble
    python apps/tools/archive_rooms.py --once --out-dir /tmp/archive --dry
    python apps/tools/archive_rooms.py                 # loop with --interval

Output: one summary line per room — records read, records new, file size.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (str(ROOT), str(ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

import httpx

# Default rooms: marketplace deliveries, judge verdicts, kibble and the
# publication rooms on the flop side (all public, read unsigned).
DEFAULT_ROOMS: tuple[str, ...] = (
    "tclk-offers",
    "tclk-deliveries",
    "kibble",
    "blockrewards",
    "d-blockrewards-feed",
    "d-flop-harness",
    "flop-harness",
    "d-fleet-feeds",
)

DEFAULT_OUT_DIR = os.environ.get("LUMI_ARCHIVE_DIR", "/var/lib/lumi-earn/archives")
EXPORT_PATH = "/r/{room}/export"
TIMEOUT_S = float(os.environ.get("LUMI_ARCHIVE_TIMEOUT", "120"))
MAX_BODY_BYTES = int(os.environ.get("LUMI_ARCHIVE_MAX_BYTES", str(64 * 1024 * 1024)))
ROOM_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")

# A reveal/escrow preimage can appear as plain text in a room line; the archive
# keeps the raw ring but the `line` field written to the DB is masked
# (see mask_secret).
_SECRET_RES = (
    re.compile(r"(?i)(\bsecret\s*[:=]\s*)([^\n⏎|]+)"),
    re.compile(r"(?i)(\bpreimage\s*[:=]\s*)([^\n⏎|]+)"),
)


def base_url() -> str:
    """Technocore base URL (env TECHNNOCORE/TECHNOCORE_BASE_URL, else production)."""
    return (
        os.environ.get("TECHNOCORE_BASE_URL")
        or os.environ.get("TECHONOCORE_BASE_URL")
        or "https://technocore.chat"
    ).rstrip("/")


def safe_room(room: str) -> str:
    """Validate the room name; blocks path traversal (../, /) up front."""
    name = (room or "").strip().lstrip("/")
    if not ROOM_RE.match(name):
        raise ValueError(f"invalid room name: {room!r}")
    return name


def mask_secret(text: str) -> str:
    """Mask the `secret:` / `preimage:` value — the masking rule lives in one place.

    The archive file keeps the raw ring (the room is already public); the line
    written to the DB is masked, because that line is queried and shown on
    dashboards.
    """
    out = text or ""
    for rx in _SECRET_RES:
        out = rx.sub(r"\1«masked»", out)
    return out


def archive_path(out_dir: str | Path, room: str) -> Path:
    return Path(out_dir) / f"{safe_room(room)}.jsonl"


def load_seqs(path: Path) -> tuple[set[int], int, int | None, int | None]:
    """Read the seqs in the file: (set of seqs, line count, first seq, last seq).

    A corrupt line is not counted but does not kill the file either — losing the
    whole archive to a single bad line as it grows is unacceptable.
    """
    seqs: set[int] = set()
    lines = 0
    first: int | None = None
    last: int | None = None
    if not path.exists():
        return seqs, lines, first, last
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            if not raw.strip():
                continue
            lines += 1
            try:
                rec = json.loads(raw)
            except Exception:
                continue
            seq = rec.get("seq")
            if isinstance(seq, int):
                seqs.add(seq)
                first = seq if first is None or seq < first else first
                last = seq if last is None or seq > last else last
    return seqs, lines, first, last


def fetch_export(room: str, base: str | None = None, timeout: float = TIMEOUT_S) -> str:
    """Download the room export body (raw JSONL). Raises on error."""
    url = f"{(base or base_url())}{EXPORT_PATH.format(room=safe_room(room))}"
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        res = client.get(url)
        res.raise_for_status()
        if len(res.content) > MAX_BODY_BYTES:
            raise ValueError(f"body too large: {len(res.content)} bytes > {MAX_BODY_BYTES}")
        return res.text


def iter_records(text: str):
    """Split the JSONL body into records; corrupt lines are skipped silently."""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if isinstance(rec, dict):
            yield rec


def archive_room(
    room: str,
    out_dir: str | Path = DEFAULT_OUT_DIR,
    *,
    base: str | None = None,
    dry: bool = False,
    timeout: float = TIMEOUT_S,
) -> dict:
    """Archive a single room and return a summary (program_watch writes this dict to the ledger).

    The returned keys are read as-is by apps/scheduler/program_watch.py:
    room, read, new, skipped, bytes, rows, first_seq, last_seq,
    bytes_to_append, error, dry.
    """
    room = safe_room(room)
    path = archive_path(out_dir, room)
    # NOTE: program_watch.py reads these dict keys by these exact names.
    summary: dict = {
        "room": room,
        "read": 0,
        "new": 0,
        "skipped": 0,
        "bytes": path.stat().st_size if path.exists() else 0,
        "rows": 0,
        "first_seq": None,
        "last_seq": None,
        "bytes_to_append": 0,
        "error": "",
        "dry": bool(dry),
    }
    try:
        body = fetch_export(room, base, timeout)
    except Exception as exc:  # network/HTTP error: skip this room, others continue
        summary["error"] = f"{type(exc).__name__}: {exc}"
        return summary

    seqs, lines, first, last = load_seqs(path)
    fresh: list[str] = []
    for rec in iter_records(body):
        summary["read"] += 1
        seq = rec.get("seq")
        if not isinstance(seq, int):
            summary["skipped"] += 1
            continue
        if seq in seqs:
            continue
        seqs.add(seq)
        fresh.append(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
        if first is None or seq < first:
            first = seq
        if last is None or seq > last:
            last = seq

    payload = "".join(line + "\n" for line in fresh)
    summary["new"] = len(fresh)
    summary["bytes_to_append"] = len(payload.encode("utf-8"))
    summary["rows"] = lines + len(fresh)
    summary["first_seq"] = first
    summary["last_seq"] = last
    if not dry and fresh:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        # Append only: the file is never rewritten under any circumstance.
        with path.open("a", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
    summary["bytes"] = path.stat().st_size if path.exists() else 0
    return summary


def format_summary(s: dict) -> str:
    """Single-line summary (the format that lands in the systemd journal)."""
    if s["error"]:
        return f"[room] {s['room']:<20} ERROR {s['error']}"
    tail = " (dry — not written)" if s["dry"] else ""
    return (
        f"[room] {s['room']:<20} read={s['read']:<6} new={s['new']:<5} "
        f"skipped={s['skipped']:<4} bytes={s['bytes']:<10} "
        f"rows={s['rows']:<6} seq={s['first_seq']}..{s['last_seq']}{tail}"
    )


def run_once(
    rooms: list[str] | tuple[str, ...] | None = None,
    out_dir: str | Path = DEFAULT_OUT_DIR,
    *,
    dry: bool = False,
    base: str | None = None,
) -> list[dict]:
    """Archive the given rooms in one round; returns the list of summaries."""
    result: list[dict] = []
    for room in rooms or DEFAULT_ROOMS:
        summary = archive_room(room, out_dir, base=base, dry=dry)
        result.append(summary)
        print(format_summary(summary), flush=True)
    return result


def parse_rooms(raw: str | None) -> list[str]:
    """Turn the `--rooms a,b,c` input into a list (defaults when empty)."""
    if not raw:
        return list(DEFAULT_ROOMS)
    return [x.strip() for x in raw.split(",") if x.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="archive_rooms", description="technocore room archive")
    ap.add_argument("--once", action="store_true", help="run one round and exit")
    ap.add_argument("--rooms", default="", help="comma-separated room list")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="archive directory")
    ap.add_argument("--interval", type=float, default=float(os.environ.get("LUMI_ARCHIVE_INTERVAL", "21600")),
                    help="seconds between rounds when --once is absent (default 6 hours)")
    ap.add_argument("--dry", action="store_true", help="download and report, do not write to disk")
    args = ap.parse_args(argv)

    rooms = parse_rooms(args.rooms)
    if args.once:
        run_once(rooms, args.out_dir, dry=args.dry)
        return 0

    # Loop mode: for manual runs when there is no systemd timer.
    while True:
        run_once(rooms, args.out_dir, dry=args.dry)
        time.sleep(max(60.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
