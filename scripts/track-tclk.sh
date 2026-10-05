#!/usr/bin/env bash
# tclk earnings tracking — one command, no guesswork.
#
# Question: "Is LUMI doing the jobs, is it earning tokens?"
# The answer is read from three layers:
#   1) decision funnel  — what was decided for the incoming offer (audit table)
#   2) delivery         — was the accepted job actually done (outcome)
#   3) payment          — did a lock/payment frame arrive (earnings)
set -euo pipefail
cd "$(dirname "$0")/.."
PSQL=(docker compose exec -T lumi-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA "$@"' psql)
q() { "${PSQL[@]}" -c "$1"; }

echo "════ tclk EARNINGS TRACKING · $(date -u '+%Y-%m-%d %H:%M UTC') ════"

echo
echo "── 1) DECISION FUNNEL (last 24 hours) ──"
q "SELECT decision || '  →  ' || count(*) FROM tclk_offer_audits
   WHERE created_at > now() - interval '24 hours' GROUP BY decision ORDER BY count(*) DESC;"

echo
echo "── 2) WHY REJECTED (last 24 hours, first 6) ──"
q "SELECT count(*) || '  ×  ' || left(reason, 60) FROM tclk_offer_audits
   WHERE created_at > now() - interval '24 hours' AND decision <> 'accept'
   GROUP BY left(reason, 60) ORDER BY count(*) DESC LIMIT 6;"

echo
echo "── 2b) SAME TABLE, LAST 2 HOURS (current gate behaviour) ──"
q "SELECT count(*) || '  ×  ' || left(reason, 60) FROM tclk_offer_audits
   WHERE created_at > now() - interval '2 hours' AND decision <> 'accept'
   GROUP BY left(reason, 60) ORDER BY count(*) DESC LIMIT 6;"

echo
echo "── 3) OUTCOME OF ACCEPTED JOBS (last 24 hours) ──"
q "SELECT outcome || '  →  ' || c FROM (
     SELECT COALESCE(NULLIF(outcome, ''), '(no record — old build)') AS outcome, count(*) AS c
     FROM tclk_offer_audits
     WHERE decision = 'accept' AND created_at > now() - interval '24 hours'
     GROUP BY 1
   ) t ORDER BY c DESC;"

echo
echo "── 4) DELIVERED (last 24 hours) ──"
q "SELECT to_char(delivered_at, 'HH24:MI') || '  ' || amount || ' ' || asset
        || '  ' || left(contract, 20) || '…  answer: ' || left(answer, 60)
   FROM tclk_offer_audits WHERE outcome = 'delivered' AND created_at > now() - interval '24 hours'
   ORDER BY delivered_at DESC LIMIT 10;"

echo
echo "── 5) EARNINGS: locks/payments arriving on our contracts ──"
q "SELECT kind || '  ' || COALESCE(NULLIF(asset, ''), '?') || ' ' || COALESCE(NULLIF(amount, ''), '?')
        || '  ' || left(contract, 20) || '…  ' || to_char(created_at, 'MM-DD HH24:MI')
   FROM tclk_frames
   WHERE kind IN ('lock', 'receipt', 'reveal', 'refund')
     AND contract <> '' AND contract IN (
       SELECT contract FROM tclk_offer_audits WHERE contract <> ''
     )
   ORDER BY created_at DESC LIMIT 10;"
echo -n "   TOTAL LOCKED: "
q "SELECT COALESCE(sum(NULLIF(regexp_replace(amount, '[^0-9]', '', 'g'), '')::numeric), 0)
   FROM tclk_frames WHERE kind = 'lock' AND contract <> ''
     AND contract IN (SELECT contract FROM tclk_offer_audits WHERE contract <> '');"

echo
echo "── 6) US ON THE LIVE NETWORK (our own DID's frames) ──"
DID="${LUMI_AGENT_DID:-}"
if [ -z "$DID" ]; then
  echo "   (LUMI_AGENT_DID not set — export it, or run scripts/setup.sh to register)"
else
  q "SELECT kind || '  →  ' || count(*) FROM tclk_frames
     WHERE author LIKE '%' || '$DID' || '%'
     GROUP BY kind ORDER BY count(*) DESC;"
fi
echo -n "   market-wide lock count: "
q "SELECT count(*) FROM tclk_frames WHERE kind = 'lock';"
