#!/usr/bin/env python3
"""LUMI · live log — what the LLM and Jev did, on one readable page.

Why a separate service: the existing panel is a decision/audit-focused SPA. This
service answers one question — "is it really running, what is it doing" — and
shows the two logs separately: LLM decisions (agent_evaluations) and Jev
decisions (tclk_offer_audits).

It runs SELECT only; it writes to no table.
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


# ── queries ────────────────────────────────────────────────────────────────

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
       AND f.kind IN ('lock', 'settle', 'payment', 'receipt')
       AND f.rail = 'flop-htlc'),
  (SELECT count(*) FROM tclk_frames f
     JOIN tclk_offer_audits a ON a.contract = f.contract
     WHERE f.contract <> '' AND a.contract <> ''
       AND f.kind IN ('lock', 'settle', 'payment', 'receipt')
       AND coalesce(f.rail, '') <> 'flop-htlc'),
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

# Which frame kinds arrive when there is no income — proves the "no lock" claim
CONTRACT_FRAMES_SQL = """
SELECT f.kind, count(*) AS n
FROM tclk_frames f
JOIN tclk_offer_audits a ON a.contract = f.contract
WHERE f.contract <> '' AND a.contract <> ''
GROUP BY f.kind ORDER BY n DESC
LIMIT 8
"""


# Job completion status: for EVERY job we accepted — was it produced, delivered,
# did its lock arrive, did we claim it. A question separate from the decision log:
# "did it do the job?" (the SQL alias `kilit` is Turkish for lock; internal to this query.)
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


# ── helpers ─────────────────────────────────────────────────────────────


def esc(v: object) -> str:
    return "—" if v is None or v == "" else html.escape(str(v))


def cut(v: object, n: int) -> str:
    s = " ".join(str(v or "").split())
    return html.escape(s[:n] + ("…" if len(s) > n else "")) or "—"


def num(v: object) -> str:
    """Thousands-separated number (dot separator, for metric readability)."""
    try:
        return f"{int(v):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "—"


def ago(ts: datetime | None) -> str:
    if not ts:
        return "never"
    secs = (datetime.now(UTC) - ts).total_seconds()
    if secs < 90:
        return f"{int(secs)}s ago"
    if secs < 5400:
        return f"{int(secs // 60)}min ago"
    if secs < 172800:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


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

# ── LLM token usage (spend) ─────────────────────────────────────────────────
# Source: llm_usage — the model calls' own accounting (written by agent_core.llm).
# The table appears on the first LLM call; if it is absent the section says "no data".
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
SELECT coalesce(nullif(purpose, ''), '(purpose not specified)'), service,
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
    """All queries on one connection — to keep page latency down."""
    with psycopg.connect(dsn(), connect_timeout=8) as conn:
        cur = conn.cursor()

        def run(sql: str, args: tuple = ()) -> list[tuple]:
            cur.execute(sql, args)
            return cur.fetchall()

        def run_safe(sql: str, args: tuple = ()) -> list[tuple]:
            """If a query blows up (e.g. the table does not exist yet) do not take the page down."""
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
     locked_paper, last_llm, last_jev, last_audit) = d["summary"]

    # ── job completion: a SEPARATE question from the decision log — "did it do the job?" ──
    jobs = d["jobs"]
    n_jobs = len(jobs)
    n_done = sum(1 for r in jobs if (r[5] or "") in ("delivered", "claimed"))
    n_locked = sum(1 for r in jobs if int(r[7] or 0) > 0)
    n_claimed = sum(1 for r in jobs if int(r[10] or 0) > 0)

    # ── market + score: what is on the market, what we took, how much of it we did ──
    (seen24, sc_acc24, sc_acc, sc_ok, sc_claim, sc_noa,
     sc_flop_seen, sc_flop_acc) = d["score"]
    acc_all, ok_all = int(sc_acc or 0), int(sc_ok or 0)
    rate = (100.0 * ok_all / acc_all) if acc_all else 0.0
    share = (100.0 * int(sc_acc24 or 0) / int(seen24 or 1)) if seen24 else 0.0

    # ── LLM token spend: how many times and for how many tokens we called the model ──
    (u_c1, u_t1, u_c24, u_t24, u_in24, u_out24, u_c7, u_t7,
     u_c_all, u_t_all, u_last) = (d["usage"] or (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, None))
    tok_per_call = (float(u_t24) / float(u_c24)) if u_c24 else 0.0

    llm_ok, jev_ok = is_fresh(last_llm), is_fresh(last_jev)
    banner = (
        f'<div class="banner {"good" if llm_ok else "bad"}">'
        f'LLM: {"RUNNING" if llm_ok else "SILENT"} — last decision {ago(last_llm)}'
        f' &nbsp;·&nbsp; Jev: {"RUNNING" if jev_ok else "SILENT"} — last call '
        f' &nbsp;·&nbsp; tclk gate: last offer {ago(last_audit)}'
        f' &nbsp;·&nbsp; jobs: done {n_done}/{n_jobs}'
        f' · locks {n_locked} · claims {n_claimed}'
        f'</div>'
    )

    cards = f"""
    <div class="cards">
      <div class="card"><div class="k">LLM · last 5 min</div>
        <div class="v {"good-t" if llm_ok else "bad-t"}">{llm5}</div>
        <div class="n">1h: {llm60} · 24h: {llm24}</div></div>
      <div class="card"><div class="k">Jev · last 5 min</div>
        <div class="v {"good-t" if jev_ok else "bad-t"}">{jv5}</div>
        <div class="n">1h: {jv60} · 24h: {jv24}</div></div>
      <div class="card"><div class="k">Accepted · 24h</div><div class="v">{acc24}</div>
        <div class="n">delivered {del24} · unanswered {noa24}</div></div>
      <div class="card"><div class="k">Jobs · did it really do the work</div>
        <div class="v {"good-t" if n_done else "bad-t"}">{n_done}/{n_jobs}</div>
        <div class="n">delivered / accepted (last {n_jobs})</div></div>
      <div class="card"><div class="k">Escrow · lock &amp; claim</div>
        <div class="v {"good-t" if n_locked else "bad-t"}">{n_locked}</div>
        <div class="n">locks received · reveal {n_claimed}</div></div>
      <div class="card"><div class="k">Earnings · flop-htlc (real)</div>
        <div class="v {"" if locked else "bad-t"}">{locked}</div>
        <div class="n">worthless paper (sim) beside it: {locked_paper}</div></div>
    </div>"""

    job_tbl = table(
        ["time", "contract", "job (brief)", "amount", "DID IT DO THE JOB?",
         "answer produced", "lock", "claim", "delivery"],
        [[f'<span class="mono">{stamp(t)}</span>',
          f'<span class="mono">{cut(c, 20)}</span>',
          cut(sp, 26), esc(amt),
          tag("DONE" if (out or "") == "delivered" else "NOT DONE",
              "t-acc" if (out or "") in ("delivered", "claimed") else "t-skip"),
          cut(ans, 70),
          tag(k, "t-acc" if int(k or 0) else "t-dim"),
          tag(max(int(rv or 0), int(_cl or 0)), "t-acc" if (int(rv or 0) or int(_cl or 0)) else "t-dim"),
          esc(ago(dl))]
         for t, c, sp, amt, asset, out, ans, k, rv, dl, _cl in jobs],
        "no accepted jobs yet — nothing has been taken on",
    )

    market_tbl = table(
        ["time", "room", "asset", "amount", "rail", "our decision", "risk",
         "reason", "offer (brief)"],
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
        "no offers seen on the market yet",
    )

    usage_purpose_tbl = table(
        ["purpose", "service", "calls", "total tokens", "completion tokens", "avg ms",
         "last call"],
        [[cut(p, 30), tag(sv or "(unknown)", "t-dim"), num(n), num(tok),
          num(out), num(ms), esc(ago(ts))]
         for p, sv, n, tok, out, ms, ts in d["usage_purpose"]],
        "no token records in the last 24h — no measured model call yet",
    )

    usage_tbl = table(
        ["time", "service", "purpose", "model", "prompt tokens", "completion tokens",
         "total", "ms"],
        [[f'<span class="mono">{stamp(t)}</span>', cut(sv, 12), cut(p, 22),
          cut(m, 24), num(pin), num(pout), tag(num(tot), "t-safe"), num(ms)]
         for t, sv, p, m, pin, pout, tot, ms in d["usage_recent"]],
        "no token records yet — the table fills on the first model call",
    )

    models_tbl = table(
        ["model", "decisions in 24h", "last use"],
        [[f'<span class="mono">{cut(m, 34)}</span>', esc(n), esc(ago(t))]
         for m, n, t in d["models"]],
        "no LLM decisions in 24h",
    )

    llm_tbl = table(
        ["time", "model", "score", "tier", "reason", "who", "text evaluated"],
        [[f'<span class="mono">{stamp(t)}</span>', cut(m, 24), esc(s),
          tag(tier, "t-safe" if tier == "SAFE" else "t-dim"),
          cut(r, 44), cut(nick, 18), cut(txt, 90)]
         for t, m, s, tier, r, nick, txt in d["llm"]],
        "no LLM decisions recorded yet",
    )

    jev_tbl = table(
        ["time", "tier", "confidence", "Jev reason", "model", "decision", "brief",
         "offer summary"],
        [[f'<span class="mono">{stamp(t)}</span>',
          tag(tier, {"SAFE": "t-safe", "RISKY": "t-risky"}.get(tier, "t-dang")),
          esc(f"{c:.2f}" if c is not None else None), cut(jr, 50), cut(jm, 20),
          tag(dc, "t-acc" if dc == "accept" else "t-skip"),
          tag("missing" if sm else "present", "t-skip" if sm else "t-safe"),
          cut(sp or r, 56)]
         for t, tier, c, jr, jm, dc, r, sp, sm in d["jev"]],
        "no Jev decisions yet — Jev was not called",
    )

    flow_tbl = table(
        ["time", "decision", "risk", "reason", "rail", "amount", "brief", "job",
         "outcome", "answer sent"],
        [[f'<span class="mono">{stamp(t)}</span>',
          tag(dc, "t-acc" if dc == "accept" else "t-skip"),
          tag(risk, "t-safe" if risk == "SAFE" else "t-risky"),
          cut(rs, 40), cut(rail, 16), esc(amt),
          tag("missing" if sm else "present", "t-skip" if sm else "t-safe"),
          cut(sp, 30),
          tag(out or "—", {"delivered": "t-acc", "no_answer": "t-risky"}.get(
              out or "", "t-dim")),
          cut(ans, 44)]
         for t, dc, risk, rs, rail, amt, sp, sm, out, ans in d["flow"]],
        "no offer decisions yet",
    )

    earn_tbl = table(
        ["time", "kind", "rail", "asset", "amount", "contract", "answer sent"],
        [[f'<span class="mono">{stamp(t)}</span>', esc(k), esc(rail), esc(asset),
          esc(amt), f'<span class="mono">{cut(c, 22)}</span>', cut(ans, 30)]
         for t, k, rail, asset, amt, c, ans in d["earn"]],
        "no earnings — no lock/payment frame has arrived on our own contracts",
    )
    myframes = ", ".join(f"{esc(k)} ×{esc(n)}" for k, n in d["myframes"]) or "—"

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH_S}">
<title>LUMI · live log</title><style>{CSS}</style></head><body>
<h1>LUMI · live log</h1>
<p class="sub">What the LLM did and what Jev decided — shown separately. Read-only,
refreshes every {REFRESH_S} seconds · page clock {stamp(datetime.now(UTC))} UTC</p>
{banner}
{cards}
<h2>Spend · LLM token usage</h2>
<p class="sub">Source <span class="mono">llm_usage</span> — every model call writes its
own accounting (prompt in + answer out, hidden reasoning tokens included).
The "purpose" column says what burned it: <span class="mono">tclk-produce</span> =
actually producing the market brief, <span class="mono">kibble</span> = kibble work.
Last call {ago(u_last)}. Average <strong>{tok_per_call:,.0f}</strong> tokens/call
(24h, thousands-separated).</p>
<div class="cards">
  <div class="card"><div class="k">Last 1 hour</div>
    <div class="v">{esc(u_c1)}</div>
    <div class="n">calls · {num(u_t1)} tokens</div></div>
  <div class="card"><div class="k">Last 24 hours</div>
    <div class="v">{esc(u_c24)}</div>
    <div class="n">calls · {num(u_t24)} tokens</div></div>
  <div class="card"><div class="k">24h · in / out</div>
    <div class="v">{num(u_in24)} / {num(u_out24)}</div>
    <div class="n">prompt / completion tokens</div></div>
  <div class="card"><div class="k">Average · tokens/call</div>
    <div class="v">{tok_per_call:,.0f}</div>
    <div class="n">last 24 hours</div></div>
  <div class="card"><div class="k">Last 7 days</div>
    <div class="v">{num(u_t7)}</div>
    <div class="n">{esc(u_c7)} calls</div></div>
  <div class="card"><div class="k">Total (all time)</div>
    <div class="v">{num(u_t_all)}</div>
    <div class="n">{esc(u_c_all)} calls</div></div>
</div>
{usage_purpose_tbl}
<div style="height:12px"></div>
{usage_tbl}
<h2>Market · live offer flow and our score</h2>
<p class="sub">Source <span class="mono">tclk_offer_audits</span> — <strong>every</strong>
offer we saw on the market: amount, asset (<span class="mono">FLOP</span> = real money
line, <span class="mono">PAPER</span> = play money), our decision and why.
Our score: how much of the work we accepted we actually delivered, and in how many we
got the payout right. Acceptance rate (24h) <strong>%{share:.1f}</strong>, delivery
success <strong>%{rate:.1f}</strong>.</p>
<div class="cards">
  <div class="card"><div class="k">Market share · 24h</div>
    <div class="v">{esc(sc_acc24)} / {esc(seen24)}</div>
    <div class="n">accepted / offers seen · %{share:.1f}</div></div>
  <div class="card"><div class="k">Delivery success</div>
    <div class="v {"good-t" if rate >= 50 else "bad-t"}">%{rate:.1f}</div>
    <div class="n">delivered+claimed {ok_all} / accepted {acc_all}</div></div>
  <div class="card"><div class="k">Money line · FLOP</div>
    <div class="v {"good-t" if sc_flop_acc else "bad-t"}">{esc(sc_flop_acc)} / {esc(sc_flop_seen)}</div>
    <div class="n">FLOP offers taken / seen</div></div>
  <div class="card"><div class="k">Payout claimed</div>
    <div class="v {"good-t" if sc_claim else "bad-t"}">{esc(sc_claim)}</div>
    <div class="n">secret revealed and payout right processed</div></div>
  <div class="card"><div class="k">Unresolved</div>
    <div class="v {"bad-t" if sc_noa else ""}">{esc(sc_noa)}</div>
    <div class="n">no answer produced — no payment</div></div>
  <div class="card"><div class="k">Total accepted</div>
    <div class="v">{esc(sc_acc)}</div>
    <div class="n">work taken since the start</div></div>
</div>
{market_tbl}
<h2>1 · Job completion — did it really do the job?</h2>
<p class="sub">One row for <strong>every job</strong> we accepted: what the brief was,
whether the work was <strong>produced</strong> (DONE) or could not be produced (NOT DONE),
the real answer that went to the room, whether the escrow lock arrived, whether we
claimed (revealed) it. This section answers <strong>"did it do the job"</strong>, not
"did it decide". Right now: accepted {n_jobs} · done {n_done} · locks received {n_locked} · claims {n_claimed}.</p>
{job_tbl}
<h2>2 · LLM log</h2>
<p class="sub">Source <span class="mono">agent_evaluations</span> — which model,
which score/tier, which reason, which text it looked at.</p>
{models_tbl}
<div style="height:12px"></div>
{llm_tbl}
<h2>3 · Jev log</h2>
<p class="sub">Source <span class="mono">tclk_offer_audits</span> (jev_ran=true) —
what Jev said about which offer, with which model, at what confidence, and whether a brief existed.</p>
{jev_tbl}
<h2>4 · tclk work flow</h2>
<p class="sub">Incoming offer → decision → accept → delivery. "no_answer" = we sent
no answer (work we could not do); the "answer" column is the real text that went to the room.</p>
{flow_tbl}
<h2>5 · Earnings</h2>
<p class="sub"><strong>Lock/payment</strong> frames arriving on our own contracts.
Frame kinds currently tied to our contracts: <span class="mono">{myframes}</span></p>
{earn_tbl}
<footer>LUMI Observatory · <span class="mono">apps/logs</span> ·
<a href="/saglik">/saglik</a> (JSON summary) · <a href="/ham">/ham</a> (JSON rows)</footer>
</body></html>"""


def json_summary(d: dict) -> dict:
    (llm5, llm60, llm24, jv5, jv60, jv24, acc24, del24, noa24, locked,
     locked_paper, last_llm, last_jev, last_audit) = d["summary"]
    # NOTE: the JSON keys below are Turkish on purpose (son5dk, son_kayit, calisiyor,
    # kazanc, gorev, harcama, amac_kirilimi, ...). /saglik is a machine contract read
    # by external monitors, so the keys stay exactly as they are.
    return {
        "llm": {"son5dk": llm5, "son1saat": llm60, "son24saat": llm24,
                "son_kayit": last_llm.isoformat() if last_llm else None,
                "calisiyor": is_fresh(last_llm)},
        "jev": {"son5dk": jv5, "son1saat": jv60, "son24saat": jv24,
                "son_cagri": last_jev.isoformat() if last_jev else None,
                "calisiyor": is_fresh(last_jev)},
        "tclk": {"son_teklif": last_audit.isoformat() if last_audit else None,
                 "kabul24s": acc24, "teslim24s": del24, "cozulemedi24s": noa24},
        "kazanc": {"kilitli_flop_htlc": locked, "kilitli_paper": locked_paper},
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
                    # NOTE: these keys stay Turkish (akis, kazanc, gorev, pazar) —
                    # /ham is a JSON contract consumed outside this service.
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
                self._send(404, b"not found", "text/plain; charset=utf-8")
        except Exception as exc:  # the page must never return an empty 500
            msg = html.escape(f"{type(exc).__name__}: {exc}")
            self._send(200, f'<div style="font:14px monospace;color:#f85149">'
                            f"log could not be read<br>{msg}</div>".encode(),
                       "text/html; charset=utf-8")

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    print(f"lumi-logs listening :{PORT} (refresh {REFRESH_S}s)", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
