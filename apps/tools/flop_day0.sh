#!/usr/bin/env bash
# FLOP testnet day-0 runbook — one command, no decisions.
#
#   apps/tools/flop_day0.sh            # check and stop at the first blocker
#   apps/tools/flop_day0.sh --spend    # also buy 10 FLOP of compute, 3 times
#
# Preconditions it verifies instead of trusting: gateway key, DID funding, chain
# pin, gateway enrollment, live completion, receipt. The chain legs stay off
# unless FLOP_SDK_ENABLED=true and FLOP_CHAIN_RPC are set on purpose.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PY="${LUMI_PY:-/root/lumi-docs/venv-lumi/bin/python}"
SPEND=0
[ "${1:-}" = "--spend" ] && SPEND=1

cd "$REPO" || exit 1
echo "== 1/4 gateway key =="
"$PY" apps/tools/flop_enroll.py --if-needed || echo "  (key not refreshed — continuing to the readiness report)"

echo
echo "== 2/4 chain pin + balance =="
"$PY" apps/tools/flop_rail_check.py
rail=$?
case $rail in
  0) echo "  chain: ready" ;;
  2) echo "  chain: genesis mismatch — do not spend"; exit 2 ;;
  3) echo "  chain: account missing or unfunded — faucet first: https://faucet.flop.network"; exit 3 ;;
  *) echo "  chain: unreachable ($rail)"; exit 8 ;;
esac

echo
echo "== 3/4 gateway + live completion =="
"$PY" apps/tools/flop_ready.py
ready=$?
if [ $ready -ne 0 ]; then
  echo "  blocked (exit $ready) — the report above names the first fix"
  exit $ready
fi

echo
echo "== 4/4 observed price =="
"$PY" apps/earn/flop_compute.py --live-price --json

if [ $SPEND -eq 1 ]; then
  echo
  echo "== spend: 3 x 10 FLOP through the gateway =="
  FLOP_GATEWAY_ENABLED=true "$PY" apps/earn/flop_compute.py --loop 3 --amount 10 --interval 30 --json
fi

echo
echo "DAY0_OK"
