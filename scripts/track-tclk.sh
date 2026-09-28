#!/usr/bin/env bash
# tclk kazanç takibi — tek komut, tahmin yok.
#
# Soru: "LUMI görev yapıyor mu, token kazanıyor mu?"
# Cevap üç katmandan okunur:
#   1) karar hunisi   — gelen teklife ne karar verildi (denetim tablosu)
#   2) teslimat       — kabul edilen iş gerçekten yapıldı mı (outcome)
#   3) ödeme          — kilit/ödeme frame'i geldi mi (kazanç)
set -euo pipefail
cd "$(dirname "$0")/.."
PSQL=(docker compose exec -T lumi-postgres psql -U lumi -d lumi -tA)
q() { "${PSQL[@]}" -c "$1"; }

echo "════ tclk KAZANÇ TAKİBİ · $(date -u '+%Y-%m-%d %H:%M UTC') ════"

echo
echo "── 1) KARAR HUNİSİ (son 24 saat) ──"
q "SELECT decision || '  →  ' || count(*) FROM tclk_offer_audits
   WHERE created_at > now() - interval '24 hours' GROUP BY decision ORDER BY count(*) DESC;"

echo
echo "── 2) NEDEN REDDEDİLDİ (son 24 saat, ilk 6) ──"
q "SELECT count(*) || '  ×  ' || left(reason, 60) FROM tclk_offer_audits
   WHERE created_at > now() - interval '24 hours' AND decision <> 'accept'
   GROUP BY left(reason, 60) ORDER BY count(*) DESC LIMIT 6;"

echo
echo "── 2b) AYNI TABLO, SON 2 SAAT (güncel kapı davranışı) ──"
q "SELECT count(*) || '  ×  ' || left(reason, 60) FROM tclk_offer_audits
   WHERE created_at > now() - interval '2 hours' AND decision <> 'accept'
   GROUP BY left(reason, 60) ORDER BY count(*) DESC LIMIT 6;"

echo
echo "── 3) KABUL EDİLEN İŞİN SONUCU (son 24 saat) ──"
q "SELECT outcome || '  →  ' || c FROM (
     SELECT COALESCE(NULLIF(outcome, ''), '(kayıt yok — eski build)') AS outcome, count(*) AS c
     FROM tclk_offer_audits
     WHERE decision = 'accept' AND created_at > now() - interval '24 hours'
     GROUP BY 1
   ) t ORDER BY c DESC;"

echo
echo "── 4) TESLİM EDİLENLER (son 24 saat) ──"
q "SELECT to_char(delivered_at, 'HH24:MI') || '  ' || amount || ' ' || asset
        || '  ' || left(contract, 20) || '…  cevap: ' || left(answer, 60)
   FROM tclk_offer_audits WHERE outcome = 'delivered' AND created_at > now() - interval '24 hours'
   ORDER BY delivered_at DESC LIMIT 10;"

echo
echo "── 5) KAZANÇ: bizim contract'lara gelen kilit/ödeme ──"
q "SELECT kind || '  ' || COALESCE(NULLIF(asset, ''), '?') || ' ' || COALESCE(NULLIF(amount, ''), '?')
        || '  ' || left(contract, 20) || '…  ' || to_char(created_at, 'MM-DD HH24:MI')
   FROM tclk_frames
   WHERE kind IN ('lock', 'receipt', 'reveal', 'refund')
     AND contract <> '' AND contract IN (
       SELECT contract FROM tclk_offer_audits WHERE contract <> ''
     )
   ORDER BY created_at DESC LIMIT 10;"
echo -n "   TOPLAM KİLİTLİ: "
q "SELECT COALESCE(sum(NULLIF(regexp_replace(amount, '[^0-9]', '', 'g'), '')::numeric), 0)
   FROM tclk_frames WHERE kind = 'lock' AND contract <> ''
     AND contract IN (SELECT contract FROM tclk_offer_audits WHERE contract <> '');"

echo
echo "── 6) CANLI AĞDA BİZ (kendi DID'imizin frame'leri) ──"
DID="did:key:z6MkAUDITPLACEHOLDERDIDnotarealkey00000000000"
q "SELECT kind || '  →  ' || count(*) FROM tclk_frames
   WHERE author LIKE '%' || '$DID' || '%' OR author LIKE 'z6MkEXAMPLE%'
   GROUP BY kind ORDER BY count(*) DESC;"
echo -n "   pazar geneli kilit sayısı: "
q "SELECT count(*) FROM tclk_frames WHERE kind = 'lock';"
