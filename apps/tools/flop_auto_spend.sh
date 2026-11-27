#!/usr/bin/env bash
# Daily compute-spend tick for the testnet window.
#
# The agent cohort is credited on compute it actually pays for, and the window is ~90
# days — attention is the scarce resource, not the budget. This runs once a day: if the
# lane is ready it buys a small amount, otherwise it records why and exits 0 so a timer
# does not page anyone. Nothing here invents a spend when the lane is blocked.
#
#   LUMI_PY            interpreter with the app dependencies
#   FLOP_SPEND_AMOUNT  FLOP per purchase (default 5)
#   FLOP_SPEND_TICKS   purchases per run (default 2)
#   FLOP_SPEND_STATE   state/log directory (default /var/lib/lumi-earn)
set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PY="${LUMI_PY:-/root/lumi-docs/venv-lumi/bin/python}"
AMOUNT="${FLOP_SPEND_AMOUNT:-5}"
TICKS="${FLOP_SPEND_TICKS:-2}"
STATE="${FLOP_SPEND_STATE:-/var/lib/lumi-earn}"
LOG="$STATE/spend.log"
mkdir -p "$STATE"

cd "$REPO" || exit 1
stamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)

ready_out=$("$PY" apps/tools/flop_ready.py 2>&1)
ready=$?
blocker=$(printf '%s\n' "$ready_out" | awk '/^blocker/{print $2}')
balance=$(printf '%s\n' "$ready_out" | awk '/^balance/{print $2" "$3}')

if [ $ready -ne 0 ]; then
  printf '%s tick blocked blocker=%s balance=%s\n' "$stamp" "${blocker:-unknown}" "${balance:-?}" >> "$LOG"
  exit 0
fi

printf '%s tick ready balance=%s amount=%s ticks=%s\n' "$stamp" "$balance" "$AMOUNT" "$TICKS" >> "$LOG"
out=$("$PY" apps/earn/flop_compute.py --loop "$TICKS" --amount "$AMOUNT" --interval 20 --json 2>&1)
code=$?
printf '%s exit=%s %s\n' "$stamp" "$code" "$(printf '%s' "$out" | tr '\n' ' ' | cut -c1-400)" >> "$LOG"

# A completed spend is worth one line; a refusal is already in the log.
if [ $code -eq 0 ]; then
  echo "$(date -u +%Y-%m-%d) FLOP harcama turu tamam: $TICKS x $AMOUNT FLOP (bakiye $balance)"
fi
exit 0
