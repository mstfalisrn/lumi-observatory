#!/usr/bin/env python3
"""Our own testnet scoreboard, from the ledger we control.

FLOP has not published a per-agent rank yet (the PoUI leaderboard is miner-side and
empty, and the KOL weekly board is not live). Until it does, the honest number is the
one we can derive from receipts: how much compute we paid for, on how many distinct
days, and what that implies under the published 3:1 spend-to-unlock rule.

Nothing here talks to the network; it reads `/var/lib/lumi-earn/flop-compute.jsonl`
and prints a compact table. An empty ledger is reported as "no spend yet", never as
zero credit.

  FLOP_LEDGER  ledger path (default /var/lib/lumi-earn/flop-compute.jsonl)
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path

LEDGER = Path(os.environ.get("FLOP_LEDGER", "/var/lib/lumi-earn/flop-compute.jsonl"))
OK_STATES = {"ok", "spent", "settled", "delivered", "paid", "accepted"}


def _amount(row: dict) -> float:
    for key in ("amount", "flop", "spend", "total"):
        try:
            return float(row.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return 0.0


def _counts_as_spend(row: dict) -> bool:
    for key in ("status", "result", "outcome"):
        value = str(row.get(key, "")).lower()
        if value:
            return value in OK_STATES
    return True if row.get("receipt") or row.get("txHash") else False


def main() -> int:
    if not LEDGER.exists():
        print("FLOP skorboard")
        print("  ledger yok — henüz harcama yapılmadı")
        print("  (testnet açılınca günlük tik buraya yazar)")
        return 0

    rows: list[dict] = []
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)

    ok = [r for r in rows if _counts_as_spend(r)]
    spent = sum(_amount(r) for r in ok)
    receipts = sum(1 for r in ok if r.get("receipt") or r.get("txHash"))
    days = Counter(str(r.get("ts", ""))[:10] for r in ok if r.get("ts"))
    active_days = len([d for d in days if d])

    print("FLOP skorboard — kendi kayıtlarımızdan")
    print(f"  harcama      : {spent:g} FLOP ({len(ok)} başarılı / {len(rows)} deneme)")
    print(f"  receipt      : {receipts}")
    print(f"  aktif gün    : {active_days}")
    print(f"  tahmini likit: {spent / 3:.2f} FLOP (3:1 kuralı, tavan anaparanın ¾'ü)")
    if days:
        print(f"  son gün      : {max(days)} ({days[max(days)]} alım)")
    if len(ok) == 0:
        print("  not: ledger var ama başarılı harcama yok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
