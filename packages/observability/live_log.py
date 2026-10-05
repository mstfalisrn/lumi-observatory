"""Live log — the read-only data layer behind the earnings/logs view.

One source of truth for both consumers:

- ``apps/logs/app.py`` — the standalone live-log page (``/``, ``/saglik``, ``/ham``)
- ``apps/api/app.py``   — ``GET /api/v1/live/log`` + ``GET /api/v1/live/summary``
  (the same data as a module inside the main web UI)

It runs SELECT only; it writes to no table. ``psycopg`` is imported lazily so the
module can be imported without a database driver present.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

# ── configuration (env-compatible with the standalone logs service) ─────────

LIMIT = int(os.environ.get("LOGS_ROWS", "25"))
MARKET_ROWS = int(os.environ.get("LOGS_MARKET_ROWS", "20"))
USAGE_ROWS = int(os.environ.get("LOGS_USAGE_ROWS", "15"))
FRESH_S = int(os.environ.get("LOGS_FRESH_SECONDS", "900"))


def dsn() -> str:
    """Connection string: LOGS_DATABASE_URL, then DATABASE_URL, else the POSTGRES_* env set."""
    url = (
        os.environ.get("LOGS_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or os.environ.get("POSTGRES_URL")
    )
    if url:
        # SQLAlchemy-style URLs carry a driver suffix psycopg does not accept
        return (
            url.replace("postgresql+asyncpg://", "postgresql://")
            .replace("postgresql+psycopg://", "postgresql://")
            .replace("postgresql+psycopg2://", "postgresql://")
        )
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
# "did it do the job?"
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
"""


# ── collection ─────────────────────────────────────────────────────────────


def collect(dsn_url: str | None = None) -> dict:
    """All queries on one connection — to keep page latency down."""
    import psycopg

    with psycopg.connect(dsn_url or dsn(), connect_timeout=8) as conn:
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


# ── helpers ────────────────────────────────────────────────────────────────


def is_fresh(ts: datetime | None) -> bool:
    return bool(ts) and (datetime.now(UTC) - ts).total_seconds() < FRESH_S


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


def stamp(ts: datetime | None) -> str:
    return ts.astimezone(UTC).strftime("%m-%d %H:%M:%S") if ts else "—"


def num(v: object) -> str:
    """Thousands-separated number (dot separator, for metric readability)."""
    try:
        return f"{int(v):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "—"


def _iso(v: object) -> str | None:
    return v.isoformat() if isinstance(v, datetime) else (None if v is None else str(v))


def _rows(rows: list[tuple]) -> list[list]:
    """JSON-safe row lists: datetimes become ISO strings, everything else passes through."""
    return [[_iso(v) for v in r] for r in rows]


# ── JSON contracts ─────────────────────────────────────────────────────────


def json_summary(d: dict) -> dict:
    """The machine contract (same shape as the standalone service's /saglik)."""
    (llm5, llm60, llm24, jv5, jv60, jv24, acc24, del24, noa24, locked,
     locked_paper, last_llm, last_jev, last_audit) = d["summary"]
    # NOTE: the JSON keys below are Turkish on purpose (son5dk, son_kayit, calisiyor,
    # kazanc, gorev, harcama, amac_kirilimi, ...). /saglik is a machine contract read
    # by external monitors, so the keys stay exactly as they are.
    return {
        "llm": {"son5dk": llm5, "son1saat": llm60, "son24saat": llm24,
                "son_kayit": _iso(last_llm),
                "calisiyor": is_fresh(last_llm)},
        "jev": {"son5dk": jv5, "son1saat": jv60, "son24saat": jv24,
                "son_cagri": _iso(last_jev),
                "calisiyor": is_fresh(last_jev)},
        "tclk": {"son_teklif": _iso(last_audit),
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
            "son_cagri": _iso(d["usage"][10]),
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


def rows_json(d: dict) -> dict:
    """The raw-rows contract (same shape and formatting as the standalone /ham)."""
    # NOTE: these keys stay Turkish (akis, kazanc, gorev, pazar) — /ham is a JSON
    # contract consumed outside this service, so cells keep the legacy str() form.
    return {
        "llm": [[str(x) for x in r] for r in d["llm"]],
        "jev": [[str(x) for x in r] for r in d["jev"]],
        "akis": [[str(x) for x in r] for r in d["flow"]],
        "kazanc": [[str(x) for x in r] for r in d["earn"]],
        "gorev": [[str(x) for x in r] for r in d["jobs"]],
        "pazar": [[str(x) for x in r] for r in d["market"]],
    }


def page_payload(d: dict) -> dict:
    """Everything the live-log module in the web UI renders — one payload, JSON-safe."""
    return {
        "summary": json_summary(d),
        "usage": {
            "calls_1h": int(d["usage"][0] or 0),
            "tokens_1h": int(d["usage"][1] or 0),
            "calls_24h": int(d["usage"][2] or 0),
            "tokens_24h": int(d["usage"][3] or 0),
            "prompt_24h": int(d["usage"][4] or 0),
            "completion_24h": int(d["usage"][5] or 0),
            "calls_7d": int(d["usage"][6] or 0),
            "tokens_7d": int(d["usage"][7] or 0),
            "calls_all": int(d["usage"][8] or 0),
            "tokens_all": int(d["usage"][9] or 0),
            "last_call": _iso(d["usage"][10]),
        },
        "rows": {
            "llm": _rows(d["llm"]),
            "models": _rows(d["models"]),
            "jev": _rows(d["jev"]),
            "flow": _rows(d["flow"]),
            "earn": _rows(d["earn"]),
            "myframes": _rows(d["myframes"]),
            "jobs": _rows(d["jobs"]),
            "market": _rows(d["market"]),
            "usage_purpose": _rows(d["usage_purpose"]),
            "usage_recent": _rows(d["usage_recent"]),
        },
    }
