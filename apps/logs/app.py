#!/usr/bin/env python3
"""LUMI · canlı günlük — LLM ve Jev ne yaptı, tek okunabilir sayfa.

Neden ayrı servis: mevcut panel karar/denetim odaklı SPA. Bu servis tek soruyu
yanıtlar — "gerçekten çalışıyor mu, ne yapıyor" — ve iki günlüğü ayrı gösterir:
LLM kararları (agent_evaluations) ve Jev kararları (tclk_offer_audits).

Yalnızca SELECT çalıştırır; hiçbir tabloya yazmaz.
"""

from __future__ import annotations

import html
import json
import os
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg

PORT = int(os.environ.get("LOGS_PORT", "8000"))
REFRESH_S = int(os.environ.get("LOGS_REFRESH_SECONDS", "20"))
LIMIT = int(os.environ.get("LOGS_ROWS", "25"))
FRESH_S = int(os.environ.get("LOGS_FRESH_SECONDS", "900"))


def dsn() -> str:
    url = os.environ.get("LOGS_DATABASE_URL")
    if url:
        return url
    return (
        "postgresql://{u}:{p}@{h}:5432/{d}".format(
            u=os.environ.get("POSTGRES_USER", "lumi"),
            p=os.environ.get("POSTGRES_PASSWORD", ""),
            h=os.environ.get("POSTGRES_HOST", "lumi-postgres"),
            d=os.environ.get("POSTGRES_DB", "lumi"),
        )
    )


# ── sorgular ────────────────────────────────────────────────────────────────

SUMMARY_SQL = """
SELECT
  (SELECT count(*) FROM agent_evaluations
     WHERE evaluated_at > now() - interval '5 minutes'),
  (SELECT count(*) FROM agent_evaluations
     WHERE evaluated_at > now() - interval '1 hour'),
  (SELECT count(*) FROM agent_evaluations
     WHERE evaluated_at > now() - interval '24 hours'),
  (SELECT count(*) FROM tclk_offer_audits
     WHERE jev_ran AND created_at > now() - interval '5 minutes'),
  (SELECT count(*) FROM tclk_offer_audits
     WHERE jev_ran AND created_at > now() - interval '1 hour'),
  (SELECT count(*) FROM tclk_offer_audits
     WHERE jev_ran AND created_at > now() - interval '24 hours'),
  (SELECT count(*) FROM tclk_offer_audits
     WHERE decision = 'accept' AND created_at > now() - interval '24 hours'),
  (SELECT count(*) FROM tclk_offer_audits
     WHERE outcome = 'delivered' AND created_at > now() - interval '24 hours'),
  (SELECT count(*) FROM tclk_offer_audits
     WHERE outcome = 'no_answer' AND created_at > now() - interval '24 hours'),
  (SELECT count(*) FROM tclk_frames f
     JOIN tclk_offer_audits a ON a.contract = f.contract
     WHERE f.contract <> '' AND a.contract <> ''
       AND f.kind IN ('lock', 'settle', 'payment', 'receipt')),
  (SELECT max(evaluated_at) FROM agent_evaluations),
  (SELECT max(created_at) FROM tclk_offer_audits WHERE jev_ran),
  (SELECT max(created_at) FROM tclk_offer_audits)
"""

LLM_SQL = """
SELECT evaluated_at, model, score, tier, reason, nick, text
FROM agent_evaluations
ORDER BY evaluated_at DESC NULLS LAST
LIMIT %s
"""

LLM_MODELS_SQL = """
SELECT model, count(*) AS n, max(evaluated_at) AS son
FROM agent_evaluations
WHERE evaluated_at > now() - interval '24 hours'
GROUP BY model ORDER BY n DESC
LIMIT 8
"""

JEV_SQL = """
SELECT created_at, jev_tier, jev_confidence, jev_reason, jev_model,
       decision, reason, spec, spec_missing
FROM tclk_offer_audits
WHERE jev_ran
ORDER BY created_at DESC
LIMIT %s
"""

FLOW_SQL = """
SELECT created_at, decision, risk, reason, rail, amount, spec, spec_missing,
       outcome, answer
FROM tclk_offer_audits
ORDER BY created_at DESC
LIMIT %s
"""

EARN_SQL = """
SELECT f.created_at, f.kind, f.rail, f.asset, f.amount, f.contract, a.answer
FROM tclk_frames f
JOIN tclk_offer_audits a ON a.contract = f.contract
WHERE f.contract <> '' AND a.contract <> ''
  AND f.kind IN ('lock', 'settle', 'payment', 'receipt', 'reveal', 'refund',
                 'payout', 'pay')
ORDER BY f.created_at DESC
LIMIT %s
"""

# Kazanç yoksa hangi frame tipleri geliyor — "kilit yok" iddiasını kanıtlar
CONTRACT_FRAMES_SQL = """
SELECT f.kind, count(*) AS n
FROM tclk_frames f
JOIN tclk_offer_audits a ON a.contract = f.contract
WHERE f.contract <> '' AND a.contract <> ''
GROUP BY f.kind ORDER BY n DESC
LIMIT 8
"""


# Görev yapma durumu: kabul ettiğimiz HER iş için — üretildi mi, teslim edildi mi,
# kilidi geldi mi, claim ettik mi. Karar günlüğünden ayrı soru: "işi yaptı mı?"
JOB_SQL = """
WITH mine AS (
  SELECT created_at, contract, spec, amount, asset, outcome, answer, delivered_at
  FROM tclk_offer_audits
  WHERE decision = 'accept' AND contract <> ''
  ORDER BY created_at DESC
  LIMIT %s
), ev AS (
  SELECT f.contract,
         count(*) FILTER (WHERE f.kind = 'lock') AS kilit,
         count(*) FILTER (WHERE f.kind = 'reveal') AS reveal
  FROM tclk_frames f
  WHERE f.contract IN (SELECT contract FROM mine)
  GROUP BY f.contract
)
SELECT m.created_at, m.contract, m.spec, m.amount, m.asset, m.outcome, m.answer,
       coalesce(ev.kilit, 0), coalesce(ev.reveal, 0), m.delivered_at,
       CASE WHEN m.outcome = 'claimed' OR coalesce(ev.reveal, 0) > 0 THEN 1 ELSE 0 END AS claim
FROM mine m LEFT JOIN ev ON ev.contract = m.contract
ORDER BY m.created_at DESC
"""


# ── yardımcılar ─────────────────────────────────────────────────────────────


def esc(v: object) -> str:
    return "—" if v is None or v == "" else html.escape(str(v))


def cut(v: object, n: int) -> str:
    s = " ".join(str(v or "").split())
    return html.escape(s[:n] + ("…" if len(s) > n else "")) or "—"


def num(v: object) -> str:
    """Binlik ayracli sayı (metrik okunurluğu için)."""
    try:
        return f"{int(v):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "—"


def ago(ts: datetime | None) -> str:
    if not ts:
        return "hiç"
    secs = (datetime.now(UTC) - ts).total_seconds()
    if secs < 90:
        return f"{int(secs)} sn önce"
    if secs < 5400:
        return f"{int(secs // 60)} dk önce"
    if secs < 172800:
        return f"{int(secs // 3600)} saat önce"
    return f"{int(secs // 86400)} gün önce"


def is_fresh(ts: datetime | None) -> bool:
    return bool(ts) and (datetime.now(UTC) - ts).total_seconds() < FRESH_S


def stamp(ts: datetime | None) -> str:
    return ts.astimezone(UTC).strftime("%m-%d %H:%M:%S") if ts else "—"


def table(heads: list[str], rows: list[list[str]], empty: str) -> str:
    if not rows:
        return f'<p class="empty">{html.escape(empty)}</p>'
    th = "".join(f"<th>{html.escape(h)}</th>" for h in heads)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>"
                   for r in rows)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>"


def tag(text: object, kind: str) -> str:
    return f'<span class="tag {kind}">{esc(text)}</span>'


CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin:0; padding:24px; background:#0d1117; color:#e6edf3;
 font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
h1 { font-size:20px; margin:0 0 4px; }
h2 { font-size:15px; margin:30px 0 10px; padding-bottom:6px;
 border-bottom:1px solid #21262d; color:#79c0ff; }
.sub { color:#8b949e; font-size:12px; margin:0 0 12px; }
.banner { border-radius:8px; padding:10px 14px; margin:14px 0 4px; font-weight:600;
 border:1px solid #21262d; background:#161b22; }
.banner.good { border-color:#238636; background:#0f2417; color:#3fb950; }
.banner.bad { border-color:#8b2c2c; background:#2a1214; color:#f85149; }
.cards { display:flex; flex-wrap:wrap; gap:10px; margin:16px 0 6px; }
.card { background:#161b22; border:1px solid #21262d; border-radius:8px;
 padding:10px 14px; min-width:158px; }
.card .k { font-size:11px; color:#8b949e; text-transform:uppercase; letter-spacing:.5px; }
.card .v { font-size:21px; font-weight:600; margin-top:3px; }
.card .n { font-size:11px; color:#6e7681; margin-top:2px; }
.good-t { color:#3fb950; } .bad-t { color:#f85149; } .wrap { overflow-x:auto; }
table { width:100%; border-collapse:collapse; background:#161b22;
 border:1px solid #21262d; border-radius:8px; }
th,td { text-align:left; padding:7px 10px; border-bottom:1px solid #21262d;
 vertical-align:top; font-size:12.5px; }
th { background:#1c2128; color:#8b949e; font-weight:600; font-size:11px;
 text-transform:uppercase; letter-spacing:.4px; white-space:nowrap; }
tr:last-child td { border-bottom:none; }
.mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }
.empty { color:#6e7681; font-style:italic; padding:10px 2px; }
.tag { display:inline-block; padding:1px 7px; border-radius:10px; font-size:11px;
 font-weight:600; border:1px solid #30363d; color:#8b949e; white-space:nowrap; }
.t-acc { background:#132f1a; border-color:#238636; color:#3fb950; }
.t-skip { background:#2d1618; border-color:#8b2c2c; color:#f85149; }
.t-safe { background:#132f1a; border-color:#238636; color:#3fb950; }
.t-risky { background:#2b2412; border-color:#9e6a03; color:#d29922; }
.t-dang { background:#2d1618; border-color:#8b2c2c; color:#f85149; }
.t-dim { background:#1c2128; }
footer { margin-top:30px; color:#6e7681; font-size:11.5px; }
"""


MARKET_ROWS = int(os.environ.get("LOGS_MARKET_ROWS", "20"))

MARKET_SQL = """
SELECT created_at, asset, amount, rail, decision, risk, reason, spec, room, seq
FROM tclk_offer_audits
ORDER BY created_at DESC
LIMIT %s
"""

SCORE_SQL = """
SELECT
  count(*) FILTER (WHERE created_at > now() - interval '24 hours'),
  count(*) FILTER (WHERE created_at > now() - interval '24 hours'
                     AND decision = 'accept'),
  count(*) FILTER (WHERE decision = 'accept'),
  count(*) FILTER (WHERE outcome IN ('delivered','claimed')),
  count(*) FILTER (WHERE outcome = 'claimed'),
  count(*) FILTER (WHERE outcome = 'no_answer'),
  count(*) FILTER (WHERE upper(coalesce(asset,'')) = 'FLOP'),
  count(*) FILTER (WHERE upper(coalesce(asset,'')) = 'FLOP'
                     AND decision = 'accept')
FROM tclk_offer_audits
"""

# ── LLM token tüketimi (harcama) ────────────────────────────────────────────
# Kaynak: llm_usage — model çağrılarının kendi muhasebesi (agent_core.llm yazar).
# Tablo ilk LLM çağrısında kendiliğinden oluşur; yoksa bölüm "veri yok" der.
USAGE_ROWS = int(os.environ.get("LOGS_USAGE_ROWS", "15"))

USAGE_SQL = """
SELECT
  count(*) FILTER (WHERE ts > now() - interval '1 hour'),
  coalesce(sum(total_tokens) FILTER (WHERE ts > now() - interval '1 hour'), 0),
  count(*) FILTER (WHERE ts > now() - interval '24 hours'),
  coalesce(sum(total_tokens) FILTER (WHERE ts > now() - interval '24 hours'), 0),
  coalesce(sum(prompt_tokens) FILTER (WHERE ts > now() - interval '24 hours'), 0),
  coalesce(sum(completion_tokens) FILTER (WHERE ts > now() - interval '24 hours'), 0),
  count(*) FILTER (WHERE ts > now() - interval '7 days'),
  coalesce(sum(total_tokens) FILTER (WHERE ts > now() - interval '7 days'), 0),
  (SELECT count(*) FROM llm_usage),
  (SELECT coalesce(sum(total_tokens), 0) FROM llm_usage),
  max(ts)
FROM llm_usage
"""

USAGE_PURPOSE_SQL = """
SELECT coalesce(nullif(purpose, ''), '(amaç belirtilmemiş)'), service,
       count(*), coalesce(sum(total_tokens), 0),
       coalesce(sum(completion_tokens), 0),
       coalesce(round(avg(latency_ms)), 0), max(ts)
FROM llm_usage
WHERE ts > now() - interval '24 hours'
GROUP BY 1, 2
ORDER BY 4 DESC
LIMIT 10
"""

USAGE_RECENT_SQL = """
SELECT ts, service, purpose, model, prompt_tokens, completion_tokens,
       total_tokens, latency_ms
FROM llm_usage
ORDER BY ts DESC
LIMIT %s
"""""


def collect() -> dict:
    """Tek bağlantıda tüm sorgular — sayfa gecikmesi için."""
    with psycopg.connect(dsn(), connect_timeout=8) as conn:
        cur = conn.cursor()

        def run(sql: str, args: tuple = ()) -> list[tuple]:
            cur.execute(sql, args)
            return cur.fetchall()

        def run_safe(sql: str, args: tuple = ()) -> list[tuple]:
            """Sorgu patlarsa (ör. tablo henüz yok) sayfayı düşürme."""
            try:
                return run(sql, args)
            except Exception:
                conn.rollback()
                return []

        summary = (run(SUMMARY_SQL) or [()])[0]
        return {
            "summary": summary,
            "llm": run(LLM_SQL, (LIMIT,)),
            "models": run(LLM_MODELS_SQL),
            "jev": run(JEV_SQL, (LIMIT,)),
            "flow": run(FLOW_SQL, (LIMIT,)),
            "earn": run(EARN_SQL, (10,)),
            "myframes": run(CONTRACT_FRAMES_SQL),
            "jobs": run(JOB_SQL, (LIMIT,)),
            "market": run(MARKET_SQL, (MARKET_ROWS,)),
            "score": (run(SCORE_SQL) or [()])[0],
            "usage": (run_safe(USAGE_SQL) or [()])[0],
            "usage_purpose": run_safe(USAGE_PURPOSE_SQL),
            "usage_recent": run_safe(USAGE_RECENT_SQL, (USAGE_ROWS,)),
        }


def render(d: dict) -> str:
    (llm5, llm60, llm24, jv5, jv60, jv24, acc24, del24, noa24, locked,
     last_llm, last_jev, last_audit) = d["summary"]

    # ── görev yapma durumu: karar günlüğünden AYRI soru — "işi yaptı mı?" ──
    jobs = d["jobs"]
    n_jobs = len(jobs)
    n_done = sum(1 for r in jobs if (r[5] or "") in ("delivered", "claimed"))
    n_locked = sum(1 for r in jobs if int(r[7] or 0) > 0)
    n_claimed = sum(1 for r in jobs if int(r[10] or 0) > 0)

    # ── pazar + skor: pazarda ne var, biz ne aldık, ne kadarını yaptık ──
    (seen24, sc_acc24, sc_acc, sc_ok, sc_claim, sc_noa,
     sc_flop_seen, sc_flop_acc) = d["score"]
    acc_all, ok_all = int(sc_acc or 0), int(sc_ok or 0)
    rate = (100.0 * ok_all / acc_all) if acc_all else 0.0
    share = (100.0 * int(sc_acc24 or 0) / int(seen24 or 1)) if seen24 else 0.0

    # ── LLM token harcaması: modeli kaç kez ve kaç token için çağırdık ──
    (u_c1, u_t1, u_c24, u_t24, u_in24, u_out24, u_c7, u_t7,
     u_c_all, u_t_all, u_last) = (d["usage"] or (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, None))
    tok_per_call = (float(u_t24) / float(u_c24)) if u_c24 else 0.0

    llm_ok, jev_ok = is_fresh(last_llm), is_fresh(last_jev)
    banner = (
        f'<div class="banner {"good" if llm_ok else "bad"}">'
        f'LLM: {"ÇALIŞIYOR" if llm_ok else "SESSİZ"} — son karar {ago(last_llm)}'
        f' &nbsp;·&nbsp; Jev: {"ÇALIŞIYOR" if jev_ok else "SESSİZ"} — son çağrı '
        f' &nbsp;·&nbsp; tclk kapısı: son teklif {ago(last_audit)}'
        f' &nbsp;·&nbsp; görev: yapıldı {n_done}/{n_jobs}'
        f' · kilit {n_locked} · claim {n_claimed}'
        f'</div>'
    )

    cards = f"""
    <div class="cards">
      <div class="card"><div class="k">LLM · son 5 dk</div>
        <div class="v {"good-t" if llm_ok else "bad-t"}">{llm5}</div>
        <div class="n">1 saat: {llm60} · 24 saat: {llm24}</div></div>
      <div class="card"><div class="k">Jev · son 5 dk</div>
        <div class="v {"good-t" if jev_ok else "bad-t"}">{jv5}</div>
        <div class="n">1 saat: {jv60} · 24 saat: {jv24}</div></div>
      <div class="card"><div class="k">Kabul · 24 saat</div><div class="v">{acc24}</div>
        <div class="n">teslim {del24} · çözülemedi {noa24}</div></div>
      <div class="card"><div class="k">Görev · işi gerçekten yaptı mı</div>
        <div class="v {"good-t" if n_done else "bad-t"}">{n_done}/{n_jobs}</div>
        <div class="n">teslim edilen / kabul edilen (son {n_jobs})</div></div>
      <div class="card"><div class="k">Escrow · kilit &amp; claim</div>
        <div class="v {"good-t" if n_locked else "bad-t"}">{n_locked}</div>
        <div class="n">kilit gelen · reveal {n_claimed}</div></div>
      <div class="card"><div class="k">Kazanç · kilitli</div>
        <div class="v {"" if locked else "bad-t"}">{locked}</div>
        <div class="n">kendi contract'larımıza gelen</div></div>
    </div>"""

    job_tbl = table(
        ["zaman", "contract", "iş (brief)", "tutar", "İŞİ YAPTI MI?",
         "üretilen cevap", "kilit", "claim", "teslim"],
        [[f'<span class="mono">{stamp(t)}</span>',
          f'<span class="mono">{cut(c, 20)}</span>',
          cut(sp, 26), esc(amt),
          tag("YAPILDI" if (out or "") == "delivered" else "YAPILAMADI",
              "t-acc" if (out or "") in ("delivered", "claimed") else "t-skip"),
          cut(ans, 70),
          tag(k, "t-acc" if int(k or 0) else "t-dim"),
          tag(max(int(rv or 0), int(_cl or 0)), "t-acc" if (int(rv or 0) or int(_cl or 0)) else "t-dim"),
          esc(ago(dl))]
         for t, c, sp, amt, asset, out, ans, k, rv, dl, _cl in jobs],
        "kabul edilmiş iş yok — henüz hiç iş alınmadı",
    )

    market_tbl = table(
        ["zaman", "oda", "varlık", "tutar", "rail", "bizim karar", "risk",
         "neden", "teklif (brief)"],
        [[f'<span class="mono">{stamp(t)}</span>',
          f'<span class="mono">{cut(room, 12)}#{esc(seq)}</span>',
          tag(asset or "—", "t-safe" if (asset or "").upper() == "FLOP"
              else "t-dim"),
          esc(amt), esc(rail or "paper"),
          tag(dc, "t-acc" if dc == "accept" else "t-skip"),
          tag(risk or "—", {"SAFE": "t-safe", "RISKY": "t-risky",
                            "DANGEROUS": "t-dang",
                            "WATCH": "t-risky"}.get((risk or "").upper(), "t-dim")),
          cut(rs, 40), cut(sp, 34)]
         for t, asset, amt, rail, dc, risk, rs, sp, room, seq in d["market"]],
        "pazarda henüz teklif görülmedi",
    )

    usage_purpose_tbl = table(
        ["amaç", "servis", "çağrı", "toplam token", "cevap token", "ort. ms",
         "son çağrı"],
        [[cut(p, 30), tag(sv or "(bilinmiyor)", "t-dim"), num(n), num(tok),
          num(out), num(ms), esc(ago(ts))]
         for p, sv, n, tok, out, ms, ts in d["usage_purpose"]],
        "son 24 saatte token kaydı yok — henüz ölçülmüş model çağrısı yok",
    )

    usage_tbl = table(
        ["zaman", "servis", "amaç", "model", "giren token", "çıkan token",
         "toplam", "ms"],
        [[f'<span class="mono">{stamp(t)}</span>', cut(sv, 12), cut(p, 22),
          cut(m, 24), num(pin), num(pout), tag(num(tot), "t-safe"), num(ms)]
         for t, sv, p, m, pin, pout, tot, ms in d["usage_recent"]],
        "henüz token kaydı yok — tablo ilk model çağrısında dolacak",
    )

    models_tbl = table(
        ["model", "24 saatte karar", "son kullanım"],
        [[f'<span class="mono">{cut(m, 34)}</span>', esc(n), esc(ago(t))]
         for m, n, t in d["models"]],
        "24 saatte LLM kararı yok",
    )

    llm_tbl = table(
        ["zaman", "model", "puan", "tier", "gerekçe", "kim", "karara konu metin"],
        [[f'<span class="mono">{stamp(t)}</span>', cut(m, 24), esc(s),
          tag(tier, "t-safe" if tier == "SAFE" else "t-dim"),
          cut(r, 44), cut(nick, 18), cut(txt, 90)]
         for t, m, s, tier, r, nick, txt in d["llm"]],
        "henüz LLM kararı kaydı yok",
    )

    jev_tbl = table(
        ["zaman", "tier", "güven", "Jev gerekçesi", "model", "karar", "brief",
         "teklif özeti"],
        [[f'<span class="mono">{stamp(t)}</span>',
          tag(tier, {"SAFE": "t-safe", "RISKY": "t-risky"}.get(tier, "t-dang")),
          esc(f"{c:.2f}" if c is not None else None), cut(jr, 50), cut(jm, 20),
          tag(dc, "t-acc" if dc == "accept" else "t-skip"),
          tag("yok" if sm else "var", "t-skip" if sm else "t-safe"),
          cut(sp or r, 56)]
         for t, tier, c, jr, jm, dc, r, sp, sm in d["jev"]],
        "henüz Jev kararı yok — Jev çağrılmadı",
    )

    flow_tbl = table(
        ["zaman", "karar", "risk", "gerekçe", "rail", "tutar", "brief", "iş",
         "sonuç", "gönderilen cevap"],
        [[f'<span class="mono">{stamp(t)}</span>',
          tag(dc, "t-acc" if dc == "accept" else "t-skip"),
          tag(risk, "t-safe" if risk == "SAFE" else "t-risky"),
          cut(rs, 40), cut(rail, 16), esc(amt),
          tag("yok" if sm else "var", "t-skip" if sm else "t-safe"),
          cut(sp, 30),
          tag(out or "—", {"delivered": "t-acc", "no_answer": "t-risky"}.get(
              out or "", "t-dim")),
          cut(ans, 44)]
         for t, dc, risk, rs, rail, amt, sp, sm, out, ans in d["flow"]],
        "henüz teklif kararı yok",
    )

    earn_tbl = table(
        ["zaman", "tip", "rail", "varlık", "tutar", "contract", "gönderilen cevap"],
        [[f'<span class="mono">{stamp(t)}</span>', esc(k), esc(rail), esc(asset),
          esc(amt), f'<span class="mono">{cut(c, 22)}</span>', cut(ans, 30)]
         for t, k, rail, asset, amt, c, ans in d["earn"]],
        "kazanç yok — kendi contract'larımıza hiç kilit/ödeme frame'i gelmedi",
    )
    myframes = ", ".join(f"{esc(k)} ×{esc(n)}" for k, n in d["myframes"]) or "—"

    return f"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH_S}">
<title>LUMI · canlı günlük</title><style>{CSS}</style></head><body>
<h1>LUMI · canlı günlük</h1>
<p class="sub">LLM ne yaptı, Jev ne karar verdi — ayrı ayrı. Salt okunur,
{REFRESH_S} saniyede yenilenir · sayfa saati {stamp(datetime.now(UTC))} UTC</p>
{banner}
{cards}
<h2>Harcama · LLM token tüketimi</h2>
<p class="sub">Kaynak <span class="mono">llm_usage</span> — her model çağrısı kendi
muhasebesini yazar (giren prompt + çıkan cevap, gizli reasoning token'ları dahil).
"amaç" kolonu hangi işin yaktığını söyler: <span class="mono">tclk-produce</span> =
pazardaki brief'i gerçekten üretmek, <span class="mono">kibble</span> = kibble işi.
Son çağrı {ago(u_last)}. Ortalama <strong>{tok_per_call:,.0f}</strong> token/çağrı
(24 saat, binlik ayraç nokta).</p>
<div class="cards">
  <div class="card"><div class="k">Son 1 saat</div>
    <div class="v">{esc(u_c1)}</div>
    <div class="n">çağrı · {num(u_t1)} token</div></div>
  <div class="card"><div class="k">Son 24 saat</div>
    <div class="v">{esc(u_c24)}</div>
    <div class="n">çağrı · {num(u_t24)} token</div></div>
  <div class="card"><div class="k">24 saat · giren / çıkan</div>
    <div class="v">{num(u_in24)} / {num(u_out24)}</div>
    <div class="n">prompt / tamamlanan token</div></div>
  <div class="card"><div class="k">Ortalama · token/çağrı</div>
    <div class="v">{tok_per_call:,.0f}</div>
    <div class="n">son 24 saat</div></div>
  <div class="card"><div class="k">Son 7 gün</div>
    <div class="v">{num(u_t7)}</div>
    <div class="n">{esc(u_c7)} çağrı</div></div>
  <div class="card"><div class="k">Toplam (baştan beri)</div>
    <div class="v">{num(u_t_all)}</div>
    <div class="n">{esc(u_c_all)} çağrı</div></div>
</div>
{usage_purpose_tbl}
<div style="height:12px"></div>
{usage_tbl}
<h2>Pazar · canlı teklif akışı ve skorumuz</h2>
<p class="sub">Kaynak <span class="mono">tclk_offer_audits</span> — pazarda gördüğümüz
<strong>her</strong> teklif: tutar, varlık (<span class="mono">FLOP</span> = gerçek para
hattı, <span class="mono">PAPER</span> = oynanış parası), bizim kararımız ve neden.
Skorumuz: kabul ettiğimiz işlerin ne kadarını gerçekten teslim ettik ve kaçında ödeme
hakkını aldık. Kabul oranı (24 saat) <strong>%{share:.1f}</strong>, teslim başarısı
<strong>%{rate:.1f}</strong>.</p>
<div class="cards">
  <div class="card"><div class="k">Pazar payı · 24 saat</div>
    <div class="v">{esc(sc_acc24)} / {esc(seen24)}</div>
    <div class="n">kabul / görülen teklif · %{share:.1f}</div></div>
  <div class="card"><div class="k">Teslim başarısı</div>
    <div class="v {"good-t" if rate >= 50 else "bad-t"}">%{rate:.1f}</div>
    <div class="n">teslim+claim {ok_all} / kabul {acc_all}</div></div>
  <div class="card"><div class="k">Para hattı · FLOP</div>
    <div class="v {"good-t" if sc_flop_acc else "bad-t"}">{esc(sc_flop_acc)} / {esc(sc_flop_seen)}</div>
    <div class="n">aldığımız / görülen FLOP teklifi</div></div>
  <div class="card"><div class="k">Hakkı alınan · claim</div>
    <div class="v {"good-t" if sc_claim else "bad-t"}">{esc(sc_claim)}</div>
    <div class="n">sırrı açıklanıp ödeme hakkı işlenen</div></div>
  <div class="card"><div class="k">Çözülemeyen</div>
    <div class="v {"bad-t" if sc_noa else ""}">{esc(sc_noa)}</div>
    <div class="n">cevap üretilemedi — ödeme yok</div></div>
  <div class="card"><div class="k">Toplam kabul</div>
    <div class="v">{esc(sc_acc)}</div>
    <div class="n">baştan beri aldığımız iş</div></div>
</div>
{market_tbl}
<h2>1 · Görev yapma durumu — işi gerçekten yaptı mı?</h2>
<p class="sub">Kabul ettiğimiz <strong>her iş</strong> için ayrı satır: brief neydi,
iş <strong>üretildi mi</strong> (YAPILDI) yoksa üretilemedi mi (YAPILAMADI),
odaya giden gerçek cevap, escrow kilidi geldi mi, claim (reveal) ettik mi.
Bu bölüm "karar verdi mi" değil <strong>"işi yaptı mı"</strong> sorusunu yanıtlar.
Şu an: kabul {n_jobs} · yapıldı {n_done} · kilit gelen {n_locked} · claim {n_claimed}.</p>
{job_tbl}
<h2>2 · LLM günlüğü</h2>
<p class="sub">Kaynak <span class="mono">agent_evaluations</span> — hangi model,
hangi puan/tier, hangi gerekçe, hangi metne baktı.</p>
{models_tbl}
<div style="height:12px"></div>
{llm_tbl}
<h2>3 · Jev günlüğü</h2>
<p class="sub">Kaynak <span class="mono">tclk_offer_audits</span> (jev_ran=true) —
Jev hangi teklife ne dedi, hangi modelle, kaç güvenle, brief var mıydı.</p>
{jev_tbl}
<h2>4 · tclk iş akışı</h2>
<p class="sub">Gelen teklif → karar → kabul → teslimat. "no_answer" = tahmin
göndermedik (yapamayacağımız iş); "cevap" kolonu odaya giden gerçek metin.</p>
{flow_tbl}
<h2>5 · Kazanç</h2>
<p class="sub">Kendi contract'larımıza gelen <strong>kilit/ödeme</strong> frame'leri.
Şu an contract'larımıza bağlı frame tipleri: <span class="mono">{myframes}</span></p>
{earn_tbl}
<footer>LUMI Observatory · <span class="mono">apps/logs</span> ·
<a href="/saglik">/saglik</a> (JSON özet) · <a href="/ham">/ham</a> (JSON satırlar)</footer>
</body></html>"""


def json_summary(d: dict) -> dict:
    (llm5, llm60, llm24, jv5, jv60, jv24, acc24, del24, noa24, locked,
     last_llm, last_jev, last_audit) = d["summary"]
    return {
        "llm": {"son5dk": llm5, "son1saat": llm60, "son24saat": llm24,
                "son_kayit": last_llm.isoformat() if last_llm else None,
                "calisiyor": is_fresh(last_llm)},
        "jev": {"son5dk": jv5, "son1saat": jv60, "son24saat": jv24,
                "son_cagri": last_jev.isoformat() if last_jev else None,
                "calisiyor": is_fresh(last_jev)},
        "tclk": {"son_teklif": last_audit.isoformat() if last_audit else None,
                 "kabul24s": acc24, "teslim24s": del24, "cozulemedi24s": noa24},
        "kazanc": {"kilitli": locked},
        "harcama": {
            "cagri_1s": int(d["usage"][0] or 0),
            "token_1s": int(d["usage"][1] or 0),
            "cagri_24s": int(d["usage"][2] or 0),
            "token_24s": int(d["usage"][3] or 0),
            "prompt_24s": int(d["usage"][4] or 0),
            "cevap_24s": int(d["usage"][5] or 0),
            "cagri_7g": int(d["usage"][6] or 0),
            "token_7g": int(d["usage"][7] or 0),
            "token_toplam": int(d["usage"][9] or 0),
            "son_cagri": d["usage"][10].isoformat() if d["usage"][10] else None,
            "amac_kirilimi": [
                {"amac": r[0], "servis": r[1], "cagri": int(r[2] or 0),
                 "token": int(r[3] or 0), "cevap_token": int(r[4] or 0)}
                for r in d["usage_purpose"]
            ],
        },
        "skor": {
            "gorulen_24s": int(d["score"][0] or 0),
            "kabul_24s": int(d["score"][1] or 0),
            "kabul_toplam": int(d["score"][2] or 0),
            "teslim_toplam": int(d["score"][3] or 0),
            "claim": int(d["score"][4] or 0),
            "no_answer": int(d["score"][5] or 0),
            "flop_gorulen": int(d["score"][6] or 0),
            "flop_kabul": int(d["score"][7] or 0),
        },
        "gorev": {
            "kabul": len(d["jobs"]),
            "yapildi": sum(1 for r in d["jobs"] if (r[5] or "") in ("delivered", "claimed")),
            "yapilamadi": sum(1 for r in d["jobs"] if (r[5] or "") == "no_answer"),
            "kilit_gelen": sum(1 for r in d["jobs"] if int(r[7] or 0) > 0),
            "claim": sum(1 for r in d["jobs"] if int(r[10] or 0) > 0),
        },
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "lumi-logs/1.0"
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?")[0].rstrip("/") or "/"
        try:
            data = collect()
            if path in ("/", "/index.html"):
                self._send(200, render(data).encode(),
                           "text/html; charset=utf-8")
            elif path in ("/saglik", "/healthz"):
                self._send(200, json.dumps(json_summary(data), ensure_ascii=False,
                                           indent=2).encode(),
                           "application/json; charset=utf-8")
            elif path == "/ham":
                rows = {
                    "llm": [[str(x) for x in r] for r in data["llm"]],
                    "jev": [[str(x) for x in r] for r in data["jev"]],
                    "akis": [[str(x) for x in r] for r in data["flow"]],
                    "kazanc": [[str(x) for x in r] for r in data["earn"]],
                    "gorev": [[str(x) for x in r] for r in data["jobs"]],
                    "pazar": [[str(x) for x in r] for r in data["market"]],
                }
                self._send(200, json.dumps(rows, ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
            else:
                self._send(404, b"yok", "text/plain; charset=utf-8")
        except Exception as exc:  # sayfa asla boş 500 dönmesin
            msg = html.escape(f"{type(exc).__name__}: {exc}")
            self._send(200, f'<div style="font:14px monospace;color:#f85149">'
                            f"günlük okunamadı<br>{msg}</div>".encode(),
                       "text/html; charset=utf-8")

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    print(f"lumi-logs dinliyor :{PORT} (yenileme {REFRESH_S}s)", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
