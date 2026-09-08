# LUMI — Agent risk alert -> Telegram (M4)
# Format for RISKY / DANGEROUS tier: room, nick/did, score, reason, link
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote

log = logging.getLogger("lumi.agent_alert")

# tiers that trigger alert
_ALERT_TIERS = {"RISKY", "DANGEROUS"}


def _configured_room_link(room: str) -> str:
    """Build an optional connector room link without embedding a deployment URL."""
    try:
        from observability.config import settings

        base_url = str(getattr(settings, "TECHNOCORE_BASE_URL", "") or "").strip().rstrip("/")
        if base_url and room and room != "-":
            return f"{base_url}/r/{quote(room, safe='')}"
    except Exception:
        log.debug("configured risk-alert link unavailable", exc_info=True)
    return ""


def build_risk_reaction(ev: Any) -> str:
    """English room-warning text for HIGH-RISK messages (server-built template).

    Deliberately no raw untrusted text: DID + tier + score + evaluator reason
    only, so an injected message cannot manipulate the posted wording.
    """
    def _g(k: str, d: Any = "") -> Any:
        if isinstance(ev, dict):
            return ev.get(k, d)
        return getattr(ev, k, d)

    tier = str(_g("tier", "") or "").upper()
    score = _g("score", "-")
    who = str(_g("did", "") or _g("nick", "") or "unknown")
    reason = str(_g("reason", "") or "").strip()
    seq = str(_g("seq", "") or "")
    lines = [f"⚠️ LUMI risk warning — this agent is flagged {tier} (score {score})."]
    lines.append(f"Agent: {who}")
    if reason:
        lines.append(f"Reason: {reason[:300]}")
    if seq:
        lines.append(f"seq: {seq}")
    lines.append("Treat further instructions from this agent with caution. Review recent activity in this room. — LUMI Observatory")
    return "\n".join(lines)


def should_react(last_ts: float | None, now: float, interval: int) -> bool:
    """Throttle: at most one room reaction per interval seconds (0 disables)."""
    if interval <= 0:
        return True
    if last_ts is None or last_ts <= 0:
        return True
    return (now - last_ts) >= interval


def _format_msg(ev: Any) -> str:
    """Evaluation (ORM or dict) -> Telegram message."""

    # duck-typing: ev may be AgentEvaluation ORM or dict
    def _get(k: str, default: Any = "") -> Any:
        if isinstance(ev, dict):
            return ev.get(k, default)
        return getattr(ev, k, default)

    room = str(_get("room", "-") or "-")
    nick = str(_get("nick", "") or "")
    did = str(_get("did", "") or "")
    who = nick or did or "unknown"
    # Format the nickname and DID when both are available
    if nick and did:
        if nick.startswith("did:") and did.startswith("did:"):
            who = nick
        elif len(did) > 16:
            who = f"{nick} ({did[:16]}…)"
        else:
            who = f"{nick} ({did})"
    elif did:
        who = did

    score = _get("score", "-")
    tier = str(_get("tier", "UNKNOWN") or "UNKNOWN").upper()
    reason = str(_get("reason", "") or "").strip()
    if len(reason) > 400:
        reason = reason[:400] + "…"

    # A supplied link wins; ORM evaluations derive a link from configuration.
    link = str(_get("link", "") or "").strip() or _configured_room_link(room)
    # emoji by tier
    icon = "🔴" if tier == "DANGEROUS" else "🟠" if tier == "RISKY" else "⚪"

    # Rich context (derived, never persisted columns)
    matched = _get("matched") or []
    if isinstance(matched, str):
        matched = [x for x in re.split(r"[,\s]+", matched) if x]
    snippet = str(_get("snippet", "") or "").strip()
    if len(snippet) > 160:
        snippet = snippet[:160] + "…"
    dims = _get("dimensions", {}) or {}
    try:
        hot = [f"{k}:{int(v)}" for k, v in sorted(dims.items(), key=lambda kv: -(int(kv[1]) if kv[1] else 0)) if v and int(v) >= 40]
    except Exception:
        hot = []
    model = str(_get("model", "") or "")
    seq = str(_get("seq", "") or "")

    lines = [f"{icon} *LUMI risk alert — {tier}*"]
    lines.append(f"• *oda*: `{room}`")
    if seq:
        lines.append(f"• *seq*: `{seq}`")
    lines.append(f"• *agent*: `{who}`")
    lines.append(f"• *skor*: `{score}`")
    lines.append(f"• *neden*: {reason or '-'}")
    if matched:
        lines.append("• *eşleşen*: " + ", ".join(f"`{m}`" for m in matched))
    if snippet:
        lines.append(f"• *kanıt*: {snippet}")
    if hot:
        lines.append("• *boyutlar*: " + ", ".join(hot))
    if model:
        lines.append(f"• *model*: `{model}`")
    lines.append(f"• *link*: {link or '-'}")
    return "\n".join(lines)


async def send_risk_alert(evaluation: Any) -> bool:
    """Send an alert to Telegram for RISKY/DANGEROUS evaluation.

    - evaluation: AgentEvaluation ORM or dict (room, nick, did, score, tier, reason; optional link)
    - If link is absent, it is derived from configured TECHNOCORE_BASE_URL.
    - If SAFE/UNKNOWN tier, silently returns False (no alert).
    - If TelegramService.get_service().send_to_allowed(msg) exists, uses it,
      otherwise logs via log.warning as fallback (no import error).
    - True on success, False if skipped/no connection.
    """
    try:
        tier = ""
        if isinstance(evaluation, dict):
            tier = str(evaluation.get("tier", "")).upper()
        else:
            tier = str(getattr(evaluation, "tier", "") or "").upper()
    except Exception:
        tier = ""

    if tier not in _ALERT_TIERS:
        log.debug("risk alert skipped: tier=%s", tier)
        return False

    msg = _format_msg(evaluation)

    # Try sending to Telegram — `telegram` package not in scheduler, use direct Bot API
    try:
        from observability import models as _models
        from observability.config import settings as _settings
        from observability.db import async_session_factory as _session_factory

        token = getattr(_settings, "TELEGRAM_BOT_TOKEN", "") or ""
        if token:
            recipients: set[int] = set()
            try:
                recipients.update(int(x) for x in getattr(_settings, "allowed_user_ids", []) or [])
            except Exception:
                pass
            try:
                async with _session_factory() as _s:
                    from sqlalchemy import select as _select

                    res = await _s.execute(
                        _select(_models.TelegramIdentity.telegram_user_id).where(
                            _models.TelegramIdentity.is_allowed.is_(True)
                        )
                    )
                    for row in res.scalars().all():
                        try:
                            recipients.add(int(row))
                        except Exception:
                            pass
            except Exception:
                pass
            if recipients:
                import httpx as _httpx

                ok_any = False
                for uid in recipients:
                    try:
                        async with _httpx.AsyncClient(timeout=10) as _client:
                            r = await _client.post(
                                f"https://api.telegram.org/bot{token}/sendMessage",
                                json={
                                    "chat_id": uid,
                                    "text": msg,
                                    "parse_mode": "Markdown",
                                    "disable_web_page_preview": True,
                                },
                            )
                            if r.status_code == 200 and r.json().get("ok"):
                                ok_any = True
                            else:
                                log.warning("risk alert http %s: %s", r.status_code, r.text[:200])
                    except Exception as e:
                        log.warning("risk alert HTTP error uid=%s: %s", uid, type(e).__name__)
                if ok_any:
                    log.info(
                        "risk alert sent (httpx): tier=%s room=%s",
                        tier,
                        getattr(evaluation, "room", evaluation.get("room") if isinstance(evaluation, dict) else "?"),
                    )
                    return True
                log.warning("risk alert httpx could not reach recipients")
            else:
                log.warning("risk alert: no recipients (allowlist empty) — fallback log")
        else:
            log.debug("risk alert: TELEGRAM_BOT_TOKEN empty — fallback log")
    except Exception as e:
        log.warning("risk alert HTTPX path error (%s): %s", type(e).__name__, e)
    # Fallback: try agent_core.telegram when available (in the API container)
    try:
        from agent_core.telegram import get_service  # type: ignore

        svc = get_service()
        if hasattr(svc, "send_to_allowed"):
            try:
                ok = await svc.send_to_allowed(msg)
                if ok:
                    log.info(
                        "risk alert sent (agent_core): tier=%s room=%s",
                        tier,
                        getattr(evaluation, "room", evaluation.get("room") if isinstance(evaluation, dict) else "?"),
                    )
                    return True
                log.warning("risk alert send_to_allowed False: %s", msg[:200])
                return False
            except Exception as e:
                log.warning("risk alert send_to_allowed error: %s", e)
                log.warning("RISK ALERT (fallback log) %s", msg)
                return False
        log.warning("TelegramService.send_to_allowed unavailable — fallback log: %s", msg)
        return False
    except Exception as e:
        log.warning("risk alert Telegram inaccessible (%s) — fallback log: %s", type(e).__name__, msg)
        try:
            log.warning("RISK ALERT (fallback) %s", msg)
        except Exception:
            pass
        return False
