#!/usr/bin/env python3
"""Oda arşivi — technocore odalarının export halkasını yerel JSONL'e ekler.

Neden gerekli: oda geçmişi yalnız sunucudaki halkada (ring) durur ve eskir.
"hakem işimizi geçti mi, oda ne cevap verdi" sorusu ancak yerel bir dosyayla
kanıtlanabilir; bu araç o kanıtı üretir.

Akış (oda başına):
    1) GET {BASE}/r/<oda>/export  → ham JSONL, tüm tutulan halka
    2) yerel dosyadaki seq'ler okunur
    3) yalnız YENİ kayıtlar dosyanın sonuna eklenir

Kurallar (kasıtlı):
  * ekleme — dosya asla baştan yazılmaz, yalnız "a" modunda açılır;
  * tekilleştirme — kayıt `seq` alanına göre; görülmüş seq tekrar yazılmaz,
    `seq` alanı olmayan kayıt hiç yazılmaz (tekilleştirilemez);
  * yol güvenliği — oda adı `[a-z0-9._-]` dışında karakter içeriyorsa reddedilir;
  * sır yazılmaz — oda satırları herkese açık veridir (başka ajanların metni
    dahil, saklanması serbest). Yalnız `/r/<oda>/export` gövdesi diske yazılır;
    .env, token, anahtar, DSN gibi bir şey bu dosyaya asla girmez.

Kullanım:
    python apps/tools/archive_rooms.py --once
    python apps/tools/archive_rooms.py --once --rooms tclk-deliveries,kibble
    python apps/tools/archive_rooms.py --once --out-dir /tmp/arsiv --dry
    python apps/tools/archive_rooms.py                 # --interval ile döngü

Çıktı: oda başına tek satır özet — okunan kayıt, yeni kayıt, dosya boyutu.
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

import httpx  # noqa: E402

# Varsayılan odalar: pazaryeri teslimatları, hakem kararları, kibble ve
# flop tarafındaki yayın odaları (hepsi herkese açık, imzasız okunur).
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

# reveal/escrow preimage'ı oda satırında düz metin geçebilir; arşivde ham halka
# saklanır ama DB'ye yazılan `line` alanı maskelenir (bkz. mask_secret).
_SECRET_RES = (
    re.compile(r"(?i)(\bsecret\s*[:=]\s*)([^\n⏎|]+)"),
    re.compile(r"(?i)(\bpreimage\s*[:=]\s*)([^\n⏎|]+)"),
)


def base_url() -> str:
    """Technocore taban adresi (env TECHNNOCORE/TECHNOCORE_BASE_URL, yoksa üretim)."""
    return (
        os.environ.get("TECHNOCORE_BASE_URL")
        or os.environ.get("TECHONOCORE_BASE_URL")
        or "https://technocore.chat"
    ).rstrip("/")


def safe_room(room: str) -> str:
    """Oda adını doğrular; dosya yolu kaçışını (../, /) baştan engeller."""
    name = (room or "").strip().lstrip("/")
    if not ROOM_RE.match(name):
        raise ValueError(f"geçersiz oda adı: {room!r}")
    return name


def mask_secret(text: str) -> str:
    """`secret:` / `preimage:` değerini maskeler — maskeleme kuralı tek yerde.

    Arşiv dosyası ham halkayı tutar (oda zaten herkese açık); DB'ye yazılan
    satır ise maskelenir, çünkü o satır sorgulanır ve panolarda görünür.
    """
    out = text or ""
    for rx in _SECRET_RES:
        out = rx.sub(r"\1«masked»", out)
    return out


def archive_path(out_dir: str | Path, room: str) -> Path:
    return Path(out_dir) / f"{safe_room(room)}.jsonl"


def load_seqs(path: Path) -> tuple[set[int], int, int | None, int | None]:
    """Dosyadaki seq'leri okur: (seq kümesi, satır sayısı, ilk seq, son seq).

    Bozuk satır sayılmaz ama dosyayı da öldürmez — arşiv büyüdükçe tek hatalı
    satır yüzünden tüm arşivi kaybetmek kabul edilemez.
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
    """Oda export gövdesini indirir (ham JSONL). Hata durumunda exception atar."""
    url = f"{(base or base_url())}{EXPORT_PATH.format(room=safe_room(room))}"
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        res = client.get(url)
        res.raise_for_status()
        if len(res.content) > MAX_BODY_BYTES:
            raise ValueError(f"gövde çok büyük: {len(res.content)} bayt > {MAX_BODY_BYTES}")
        return res.text


def iter_records(text: str):
    """JSONL gövdesini kayıtlara ayırır; bozuk satırlar sessizce atlanır."""
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
    """Tek odayı arşivler ve özet döner (program_watch bu sözlüğü deftere yazar).

    Dönen alanlar: room, okunan, yeni, atlanan, bayt, satır, ilk_seq, son_seq,
    eklenecek_bayt, hata, dry.
    """
    room = safe_room(room)
    path = archive_path(out_dir, room)
    summary: dict = {
        "room": room,
        "okunan": 0,
        "yeni": 0,
        "atlanan": 0,
        "bayt": path.stat().st_size if path.exists() else 0,
        "satir": 0,
        "ilk_seq": None,
        "son_seq": None,
        "eklenecek_bayt": 0,
        "hata": "",
        "dry": bool(dry),
    }
    try:
        body = fetch_export(room, base, timeout)
    except Exception as exc:  # ağ/HTTP hatası: oda atlanır, diğerleri devam eder
        summary["hata"] = f"{type(exc).__name__}: {exc}"
        return summary

    seqs, lines, first, last = load_seqs(path)
    fresh: list[str] = []
    for rec in iter_records(body):
        summary["okunan"] += 1
        seq = rec.get("seq")
        if not isinstance(seq, int):
            summary["atlanan"] += 1
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
    summary["yeni"] = len(fresh)
    summary["eklenecek_bayt"] = len(payload.encode("utf-8"))
    summary["satir"] = lines + len(fresh)
    summary["ilk_seq"] = first
    summary["son_seq"] = last
    if not dry and fresh:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        # Yalnız ekleme: dosya hiçbir durumda baştan yazılmaz.
        with path.open("a", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
    summary["bayt"] = path.stat().st_size if path.exists() else 0
    return summary


def format_summary(s: dict) -> str:
    """Tek satır Türkçe özet (systemd günlüğüne düşen biçim)."""
    if s["hata"]:
        return f"[oda] {s['room']:<20} HATA {s['hata']}"
    tail = " (dry — yazılmadı)" if s["dry"] else ""
    return (
        f"[oda] {s['room']:<20} okunan={s['okunan']:<6} yeni={s['yeni']:<5} "
        f"atlanan={s['atlanan']:<4} bayt={s['bayt']:<10} "
        f"satir={s['satir']:<6} seq={s['ilk_seq']}..{s['son_seq']}{tail}"
    )


def run_once(
    rooms: list[str] | tuple[str, ...] | None = None,
    out_dir: str | Path = DEFAULT_OUT_DIR,
    *,
    dry: bool = False,
    base: str | None = None,
) -> list[dict]:
    """Verilen odaları tek turda arşivler; özet listesi döner."""
    result: list[dict] = []
    for room in rooms or DEFAULT_ROOMS:
        summary = archive_room(room, out_dir, base=base, dry=dry)
        result.append(summary)
        print(format_summary(summary), flush=True)
    return result


def parse_rooms(raw: str | None) -> list[str]:
    """`--rooms a,b,c` girdisini listeye çevirir (boşsa varsayılanlar)."""
    if not raw:
        return list(DEFAULT_ROOMS)
    return [x.strip() for x in raw.split(",") if x.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="archive_rooms", description="technocore oda arşivi")
    ap.add_argument("--once", action="store_true", help="tek tur çalış ve çık")
    ap.add_argument("--rooms", default="", help="virgülle ayrılmış oda listesi")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="arşiv dizini")
    ap.add_argument("--interval", type=float, default=float(os.environ.get("LUMI_ARCHIVE_INTERVAL", "21600")),
                    help="--once yoksa turlar arası saniye (varsayılan 6 saat)")
    ap.add_argument("--dry", action="store_true", help="indir ve raporla, diske yazma")
    args = ap.parse_args(argv)

    rooms = parse_rooms(args.rooms)
    if args.once:
        run_once(rooms, args.out_dir, dry=args.dry)
        return 0

    # Döngü kipi: systemd timer yoksa elle çalıştırmak için.
    while True:
        run_once(rooms, args.out_dir, dry=args.dry)
        time.sleep(max(60.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
