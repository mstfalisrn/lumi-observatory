#!/usr/bin/env python3
"""Program bekçisi — hakem kararı, resmî puan yayını ve oda arşivi, tek turda.

Tek tur çalışır ve ÇIKAR; döngü kurmaz, çünkü çağıran taraf systemd timer'dır
(lumi-program-watch.timer, 15 dakikada bir). Sıra kasıtlı:

    1) oda arşivi tazelenir  (apps/tools/archive_rooms.py içe aktarılır —
       aynı kod ikinci kez yazılmaz) ve room_archives defter satırları yazılır;
    2) kararlar taze tape'ten ayıklanır ve tclk_verdicts'e yazılır;
    3) resmî yayınlar (passport/points/kibble/board/leaderboard) anlık
       görüntülenir ve program_scores'a kaynak başına bir satır yazılır.

Bekçi 15 dakikada bir çalıştığı için yalnız karar odalarını (varsayılan
tclk-deliveries) ve kibble'ı tazeler; 8 odanın tamamını (~40 MB) her turda
indirmek gereksiz olurdu — o iş 6 saatlik lumi-archive-rooms.timer'a aittir.

Neden: repo bu üç gerçeği hiç saklamıyordu — "hakem işimizi geçti mi, passport
puanımız ne, oda ne cevap verdi" sorusu yalnız canlı odaya bakılarak ve
kaybolan halka üzerinden cevaplanabiliyordu.

Sır yazılmaz: DSN/parola hiçbir yere basılmaz, .env yalnız okunur; oda
satırlarındaki `secret:`/`preimage:` alanları DB'ye yazılmadan önce maskelenir
(reveal preimage = escrow claim sırrı, tclk_frames ile aynı kural).

Bayraklar:
    --once        tek tur (varsayılan davranış; zamanlayıcı bunu çağırır)
    --dry         hiçbir şey yazma (ne dosya ne DB), yalnız raporla
    --print-ours  kendi DID'imizin her yayında geçip geçmediğini yazdır

Kullanım:
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

try:  # bekçi compose dışında (host) çalışır, bu yüzden .env'i kendisi okur
    from dotenv import load_dotenv

    load_dotenv(os.environ.get("LUMI_ENV_FILE", str(ROOT / ".env")), override=False)
except Exception:  # pragma: no cover - .env yoksa ortam değişkenleri yeter
    pass

import httpx  # noqa: E402
import psycopg  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402

# Oda arşivi mantığı burada; kopyalanmaz, içe aktarılır.
from archive_rooms import (  # noqa: E402
    DEFAULT_OUT_DIR,
    archive_path,
    archive_room,
    fetch_export,
    format_summary,
    iter_records,
    mask_secret,
)

from observability.llm_usage import dsn  # noqa: E402

# Bizim ajan DID'imiz. Kibble puanı bu DID ile sorulur, yayınlarda bu aranır.
OUR_DID = os.environ.get(
    "LUMI_AGENT_DID", "did:key:z6MkAUDITPLACEHOLDERDIDnotarealkey00000000000"
)

# Karar satırları bu odalardan okunur (virgülle çoğaltılabilir).
VERDICT_ROOMS = tuple(
    x.strip() for x in os.environ.get("LUMI_VERDICT_ROOMS", "tclk-deliveries").split(",") if x.strip()
)

LINE_MAX = 2000
FEED_TIMEOUT_S = float(os.environ.get("LUMI_WATCH_TIMEOUT", "60"))
FEED_MAX_BYTES = int(os.environ.get("LUMI_WATCH_MAX_BYTES", str(16 * 1024 * 1024)))

# `0x<kontrat> <durum> ...` — karar/çerçeve satırlarının tek ortak biçimi.
HASH_RE = re.compile(r"^\s*(0x[0-9a-fA-F]{6,})\s+(.+)$", re.S)
PAYER_RE = re.compile(r"(?i)\bpayer\b\s*[:=]?\s*(did:key:[1-9A-HJ-NP-Za-km-z]{20,})")
PUNCT = " \t\r\n;.,:!?"


class Feed:
    """Resmî yayın tanımı: nereden okunur, bizim satır hangi anahtarla bulunur."""

    __slots__ = ("name", "url", "kind", "list_key", "score_key")

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


# ── karar satırı ayrıştırma (saf fonksiyonlar: DB'siz test edilebilir) ───────

def parse_ts(raw: object) -> datetime | None:
    """Oda kaydının ts alanını datetime'a çevirir; okunamazsa None."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def verdict_of(rest: str) -> str:
    """Satırın ilk kelimesinden karar: PASS / FAIL / <durum> / other."""
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
    """Tek oda kaydını karar satırına çevirir; hash'i olmayan kayıt None döner."""
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
        "line": mask_secret(text)[:LINE_MAX],  # sır maskeli, 2000 karakter kırpık
        "payer_did": payer.group(1) if payer else "",
    }


def parse_verdicts(room: str, records) -> list[dict]:
    """Kayıt akışından karar satırları; (room, seq) içinde tekilleştirilir."""
    out: dict[int, dict] = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        row = verdict_row(room, rec)
        if row is not None:
            out[row["seq"]] = row
    return [out[seq] for seq in sorted(out)]


# ── resmî yayın anlık görüntüsü ─────────────────────────────────────────────

def _as_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_feed(feed: Feed, body: object, raw_len: int = 0) -> dict:
    """Yayın gövdesinden bizim satırı seçer (ağ yok — saf fonksiyon)."""
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
        else:  # single — gövde doğrudan bize ait (kibble /api/score?did=)
            count = 1
            # Kibble "found: false" dönebilir (henüz puanlanmadık); o hâlde
            # yayın bizi anmıyor demektir, ama yayının ne dediği kayda geçsin.
            detail = {k: v for k, v in body.items() if isinstance(v, (str, int, float, bool))}
            hit = body.get("did")
            if hit == OUR_DID and body.get("found", True):
                entry = body
    score = _as_number(entry.get(feed.score_key)) if isinstance(entry, dict) else None
    return {
        "source": feed.name,
        "subject_did": OUR_DID if entry is not None else "",
        "score": score,
        "found": entry is not None,
        "payload": {
            "as_of": as_of,
            "count": count,
            "found": entry is not None,
            "bytes": raw_len,
            "detail": detail,
            # Bizim satır bulunduysa yalnız o satır saklanır: passport yayını
            # ~3.8 MB, tamamını 15 dakikada bir JSONB'ye yazmak veri tabanını
            # şişirirdi; kaybolan bilgi yok, satır kaynakta duruyor.
            "entry": entry if isinstance(entry, dict) else None,
        },
    }


def snapshot_feed(feed: Feed, client: httpx.Client | None = None) -> dict:
    """Yayını indirir ve anlık görüntüyü döner; hata hâlinde hata payload'ı."""
    url = feed.url_for(OUR_DID)
    own = client is None
    client = client or httpx.Client(timeout=FEED_TIMEOUT_S, follow_redirects=True)
    try:
        res = client.get(url)
        res.raise_for_status()
        if len(res.content) > FEED_MAX_BYTES:
            raise ValueError(f"gövde çok büyük: {len(res.content)} bayt")
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


# ── veri tabanı yazımı (psycopg; hepsi ON CONFLICT ile idempotent) ──────────

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
    """Canlı DB bağlantısı (host'ta POSTGRES_HOST=127.0.0.1, port 5433)."""
    url = dsn()
    if not url:
        raise SystemExit("[hata] DSN yok: .env içinde POSTGRES_* veya DATABASE_URL gerekli")
    return psycopg.connect(url)


def check_tables(conn) -> list[str]:
    """Yeni tablolar var mı; yoksa operatöre ne yapacağını söyleyen hata."""
    missing: list[str] = []
    with conn.cursor() as cur:
        for name in TABLES:
            cur.execute("SELECT to_regclass(%s::text)", (f"public.{name}",))
            if cur.fetchone()[0] is None:
                missing.append(name)
    return missing


def write_rows(conn, sql: str, rows: list[dict]) -> int:
    """Satırları yazar; ON CONFLICT sayesinde tekrar çalıştırma çoğaltmaz."""
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
        return cur.rowcount


# ── tur ─────────────────────────────────────────────────────────────────────

def run_once(
    *,
    dry: bool = False,
    print_ours: bool = False,
    out_dir: str | Path = DEFAULT_OUT_DIR,
    base: str | None = None,
    rooms: tuple[str, ...] | None = None,
) -> dict:
    """Tek tur: arşiv → kararlar → yayınlar. dry=True ise hiçbir yere yazmaz."""
    verdict_rooms = rooms or VERDICT_ROOMS
    now = datetime.now(UTC)
    captured_at = now.replace(second=0, microsecond=0)  # dakikaya yuvarlanır → tekrar yazım tekil
    report: dict = {"dry": dry, "arşiv": [], "karar": [], "yayın": [], "yazilan": {}}

    # 1) Oda arşivi (aynı kod: apps/tools/archive_rooms.archive_room)
    archive_summaries: list[dict] = []
    for room in (*verdict_rooms, "kibble"):
        summary = archive_room(room, out_dir, base=base, dry=dry)
        archive_summaries.append(summary)
        report["arşiv"].append(summary)
        print(format_summary(summary), flush=True)

    # 2) Kararlar: önce yerel arşiv, yoksa canlı export (yalnız okuma)
    verdict_rows: list[dict] = []
    sources: list[str] = []
    for room in verdict_rooms:
        path = archive_path(out_dir, room)
        if path.exists():
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                records = list(iter_records(fh.read()))
            sources.append(f"{room}=arsiv")
        else:
            try:
                records = list(iter_records(fetch_export(room, base)))
            except Exception as exc:
                records = []
                print(f"[karar] {room:<20} kaynak yok: {type(exc).__name__}: {exc}", flush=True)
            sources.append(f"{room}=canli")
        verdict_rows.extend(parse_verdicts(room, records))
    by_verdict: dict[str, int] = {}
    for row in verdict_rows:
        by_verdict[row["verdict"]] = by_verdict.get(row["verdict"], 0) + 1
    report["karar"] = verdict_rows
    print(
        f"[karar] kaynak={' '.join(sources)} satir={len(verdict_rows)} "
        f"dagilim={json.dumps(by_verdict, ensure_ascii=False, sort_keys=True)}",
        flush=True,
    )

    # 3) Resmî yayınlar
    with httpx.Client(timeout=FEED_TIMEOUT_S, follow_redirects=True) as client:
        snapshots = [snapshot_feed(feed, client) for feed in FEEDS]
    report["yayın"] = snapshots
    for snap in snapshots:
        mark = "bizde" if snap["found"] else "yok"
        print(
            f"[puan] {snap['source']:<12} durum={mark:<6} subject={snap['subject_did'] or '-':<56} "
            f"puan={snap['score'] if snap['score'] is not None else '-'}",
            flush=True,
        )
    if print_ours:
        ours = " ".join(f"{s['source']}={'VAR' if s['found'] else 'yok'}" for s in snapshots)
        print(f"[kendi DID] {OUR_DID} → {ours}", flush=True)

    # 4) Yazım (dry'da hiç bağlanılmaz)
    if dry:
        print(
            f"[dry] yazılmayacak: tclk_verdicts={len(verdict_rows)} program_scores={len(snapshots)} "
            f"room_archives={len(archive_summaries)}",
            flush=True,
        )
        return report

    conn = connect()
    try:
        missing = check_tables(conn)
        if missing:
            raise SystemExit(
                "[hata] eksik tablo: "
                + ", ".join(missing)
                + " — migration uygulanmamış (alembic upgrade head)"
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
                "first_seq": a["ilk_seq"],
                "last_seq": a["son_seq"],
                "records": a["satir"],
                "bytes": a["bayt"],
                "archived_at": now,
            }
            for a in archive_summaries
            if not a["hata"]
        ]
        wrote = {
            "tclk_verdicts": write_rows(conn, VERDICT_SQL, verdict_rows),
            "program_scores": write_rows(conn, SCORE_SQL, score_rows),
            "room_archives": write_rows(conn, ARCHIVE_SQL, archive_rows),
        }
        conn.commit()
        report["yazilan"] = wrote
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(
        f"[yaz] tclk_verdicts={len(verdict_rows)} satır işlendi, "
        f"program_scores={len(score_rows)} (captured_at={captured_at.isoformat()}), "
        f"room_archives={len(archive_rows)}",
        flush=True,
    )
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="program_watch", description="program yayını + oda kararı bekçisi")
    ap.add_argument("--once", action="store_true", help="tek tur (varsayılan; döngü yok)")
    ap.add_argument("--dry", action="store_true", help="hiç yazma: ne dosya ne DB")
    ap.add_argument("--print-ours", action="store_true", help="kendi DID'imiz yayınlarda var mı")
    ap.add_argument("--rooms", default="", help="karar odaları (virgüllü); boşsa tclk-deliveries")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="arşiv dizini")
    args = ap.parse_args(argv)

    rooms = tuple(x.strip() for x in args.rooms.split(",") if x.strip()) or None
    run_once(dry=args.dry, print_ours=args.print_ours, out_dir=args.out_dir, rooms=rooms)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
