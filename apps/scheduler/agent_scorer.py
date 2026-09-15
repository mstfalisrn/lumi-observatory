# LUMI — AgentScorer poller (M3)
# 15s interval: /r/events discovery + ROOMS poll + evaluate + AgentEvaluation + Telegram alert + cursor
from __future__ import annotations

import asyncio
import logging
import re
import time

from connectors.technocore import TechnocoreConnector
from observability.config import settings

log = logging.getLogger("lumi.agent_scorer")

try:
    from connectors import agent_evaluator as _ae  # type: ignore
except Exception:
    _ae = None  # type: ignore


def configured_rooms(raw: str) -> list[str]:
    return [x.strip() for x in raw.split(",") if x.strip()]

def _get_rooms() -> list[str]:
    from observability.config import settings as _s
    if not _s.TECHNOCORE_ENABLED or not _s.TECHNOCORE_MONITORED_ROOMS.strip():
        return []
    return configured_rooms(_s.TECHNOCORE_MONITORED_ROOMS)

# Backward compat alias — tests may import ROOMS
ROOMS: list[str] = _get_rooms()

# Task list for scheduler hook (import: from scheduler.agent_scorer import _AGENT_SCORER_TASK)
_AGENT_SCORER_TASK: list[asyncio.Task] = []


class AgentScorer:
    """Periodically scan Technocore rooms, evaluate messages, write to DB, send alerts."""

    def __init__(self, base_url: str | None = None, interval: int = 15) -> None:
        self.base_url = (base_url or settings.TECHNOCORE_BASE_URL or "").rstrip("/")
        if not self.base_url and settings.TECHNOCORE_ENABLED:
            log.warning("TECHNOCORE_ENABLED but TECHNOCORE_BASE_URL empty")
        self.interval = interval
        self._connector = TechnocoreConnector(base_url=self.base_url)
        self._discovered: set[str] = set()
        self._last_react: dict[str, float] = {}
        # tclk/1 marketplace surveillance rooms (opt-in, parse-only, zero LLM)
        self._tclk_rooms = (
            [r.strip() for r in settings.TCLK_MONITOR_ROOMS.split(",") if r.strip()]
            if settings.TCLK_ENABLED
            else []
        )
        # tclk claim radar + safe agent state (in-memory ONLY; preimages never persisted)
        self._tclk_claim_rails = {
            r.strip() for r in settings.TCLK_AGENT_RAILS.split(",") if r.strip()
        }
        self._tclk_lock_rail: dict[str, str] = {}  # deal slug -> rail (from lock frames)
        self._tclk_claimed: set[str] = set()  # slugs already reported
        self._tclk_active: dict[str, dict] = {}  # ref -> pending accept (preimage in memory)
        self._tclk_seen: set[str] = set()  # offer identities already considered
        self._tclk_agent_armed = bool(
            settings.TCLK_ENABLED and settings.TCLK_AGENT_ENABLED and getattr(self._connector, "did_public", "")
        )
        if self._tclk_agent_armed:
            log.info(
                "tclk agent mode armed (DID %s…) rails=%s max_active=%d",
                getattr(self._connector, "did_public", "")[:12],
                settings.TCLK_AGENT_RAILS,
                settings.TCLK_AGENT_MAX_ACTIVE,
            )
        # Load the signing key once (scheduler shares the worker DID identity).
        if settings.TECHNOCORE_ENABLED and settings.TECHNOCORE_ED25519_KEY_PATH:
            try:
                self._connector.load_or_generate_key(settings.TECHNOCORE_ED25519_KEY_PATH)
                log.info("technocore DID ready: %s", getattr(self._connector, "did_public", ""))
            except Exception as e:
                log.warning("technocore key load failed: %s", str(e)[:150])

    async def poll_once(self, session) -> int:
        if not settings.TECHNOCORE_ENABLED:
            # tclk marketplace surveillance is independent of lobby surveillance
            if self._tclk_rooms:
                return await self._poll_tclk(session)
            return 0
        """Single poll loop. Manages cursors with the given AsyncSession. Returns the number of processed messages."""
        processed = 0

        # 1) New room discovery via /r/events
        try:
            events_cursor = await self._connector.get_cursor("events", session)
        except Exception:
            events_cursor = 0

        try:
            data = await self._connector.read_room("events", since=events_cursor, wait=2, session=session)
            msgs = data.get("messages", []) or []
            for m in msgs:
                candidate: str | None = None
                if isinstance(m, dict):
                    candidate = m.get("room") or m.get("target_room") or m.get("channel")
                    if not candidate:
                        txt = str(m.get("text", "") or "")
                        mt = re.search(r"/r/([a-z0-9][a-z0-9_-]{0,47})", txt)
                        if mt:
                            candidate = mt.group(1)
                    if candidate:
                        candidate = candidate.strip()
                        if candidate and candidate not in _get_rooms() and candidate not in self._discovered:
                            # simple validation
                            if re.match(r"^[a-z0-9][a-z0-9_-]{0,47}$", candidate):
                                self._discovered.add(candidate)
                                log.info("discovered room: %s", candidate)
            # update cursor
            try:
                last = int(data.get("last_seq", events_cursor) or events_cursor)
                if last > events_cursor:
                    await self._connector.set_cursor("events", last, session)
                    await session.commit()
            except Exception:
                try:
                    await session.rollback()
                except Exception:
                    pass
        except Exception as e:
            log.debug("events poll skipped: %s", type(e).__name__)

        # 2) For each room, read with since=cursor, evaluate, write, send alert, advance cursor
        all_rooms = [*_get_rooms(), *sorted(self._discovered)]
        for room in all_rooms:
            try:
                cursor = await self._connector.get_cursor(room, session)
            except Exception:
                cursor = 0

            try:
                room_data = await self._connector.read_room(room, since=cursor, wait=2, session=session)
            except Exception as e:
                log.debug("read_room skipped room=%s err=%s", room, type(e).__name__)
                continue

            messages: list = room_data.get("messages", []) or []
            last_seq_raw = room_data.get("last_seq", cursor)
            try:
                last_seq = int(last_seq_raw) if last_seq_raw is not None else cursor
            except Exception:
                last_seq = cursor

            max_seq = cursor
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                # extract seq
                try:
                    seq = int(msg.get("seq", 0) or 0)
                except Exception:
                    seq = 0
                if seq == 0:
                    try:
                        seq = int(msg.get("global_seq", 0) or 0)
                    except Exception:
                        seq = 0
                # If seq is 0, do not generate a synthetic seq based on last_seq — skip
                if seq == 0:
                    continue

                text = str(msg.get("text", "") or msg.get("message", "") or "")
                disp, _from_did = _ae.extract_author(msg) if _ae is not None else ("", "")
                nick = disp
                did = _from_did or (str(msg.get("did")) if msg.get("did") is not None else None)

                # evaluate — via connectors.agent_evaluator.evaluate (module-level _ae import)
                result: dict | None = None
                if _ae is not None:
                    # spec: agent_evaluator.evaluate — actual function evaluate_agent_message
                    fn = getattr(_ae, "evaluate", None) or getattr(_ae, "evaluate_agent_message", None)
                    if fn is not None:
                        try:
                            result = await fn(
                                text,
                                nick=nick,
                                did=did,
                                room=room,
                                seq=seq,
                                global_seq=int(msg.get("global_seq", seq) or seq),
                                raw_json=msg,
                            )
                        except TypeError:
                            # fallback: old signature (text, nick, did, room)
                            try:
                                result = await fn(text, nick=nick, did=did, room=room)
                            except Exception as e2:
                                log.debug("evaluate failed room=%s seq=%s %s", room, seq, type(e2).__name__)
                        except Exception as e:
                            log.debug("evaluate failed room=%s seq=%s %s", room, seq, type(e).__name__)

                if result is None:
                    # skip if evaluate is missing
                    if seq > max_seq:
                        max_seq = seq
                    continue

                # Write to AgentEvaluation table (idempotent: room+seq unique)
                try:
                    from sqlalchemy import select

                    from observability.models import AgentEvaluation

                    # duplicate guard
                    chk = await session.execute(
                        select(AgentEvaluation)
                        .where(
                            AgentEvaluation.room == room,
                            AgentEvaluation.seq == seq,
                        )
                        .limit(1)
                    )
                    if chk.scalar_one_or_none() is not None:
                        if seq > max_seq:
                            max_seq = seq
                        continue

                    try:
                        gseq = int(msg.get("global_seq", seq) or seq)
                    except Exception:
                        gseq = seq

                    ev = AgentEvaluation(
                        room=room,
                        seq=seq,
                        global_seq=gseq,
                        nick=(nick[:120] if nick else "unknown"),
                        did=did,
                        text=text[:4000] if text else "",
                        raw_json=msg,
                        score=int(result.get("score", 0) or 0),
                        tier=str(result.get("tier", "SAFE") or "SAFE"),
                        reason=str(result.get("reason", "") or "")[:500],
                        dimensions=result.get("dimensions", {}) or {},
                        model=str(result.get("model", "") or "")[:80],
                    )
                    session.add(ev)
                    await session.flush()
                except Exception as e:
                    log.debug("AgentEvaluation write error room=%s seq=%s %s", room, seq, type(e).__name__)
                    try:
                        await session.rollback()
                    except Exception:
                        pass
                    if seq > max_seq:
                        max_seq = seq
                    continue

                # Rich alert context (derived, not persisted columns)
                try:
                    ev.matched = result.get("matched") or []
                    ev.snippet = result.get("snippet") or ""
                except Exception:
                    pass

                # React INTO the room (external write, opt-in): high-risk → English warning.
                if settings.TECHNOCORE_ROOM_REACT_ENABLED:
                    try:
                        from connectors.agent_alert import build_risk_reaction, should_react

                        tier = str(getattr(ev, "tier", "") or "").upper()
                        if tier in ("RISKY", "DANGEROUS"):
                            last = self._last_react.get(room, 0.0)
                            if should_react(last, time.time(), settings.TECHNOCORE_ROOM_REACT_INTERVAL):
                                txt = build_risk_reaction(ev)
                                if txt:
                                    await self._connector.signed_post(room, txt)
                                    self._last_react[room] = time.time()
                                    log.info(
                                        "risk reaction posted room=%s did=%s",
                                        room,
                                        getattr(ev, "did", "") or getattr(ev, "nick", ""),
                                    )
                    except Exception as e:
                        log.warning("risk reaction failed room=%s: %s", room, str(e)[:150])

                # Telegram is an external write: alerts are opt-in even when monitoring is enabled.
                if settings.RISK_ALERTS_ENABLED:
                    try:
                        from connectors.agent_alert import send_risk_alert  # type: ignore

                        # ev ORM object is not committed but alert works with dict/ORM
                        await send_risk_alert(ev)
                    except Exception:
                        # Alert delivery must never block durable evaluation persistence.
                        pass

                if seq > max_seq:
                    max_seq = seq
                processed += 1

            # update cursor
            # take the larger of response last_seq or seen max_seq
            new_cursor = last_seq if last_seq > max_seq else max_seq
            if new_cursor > cursor:
                try:
                    await self._connector.set_cursor(room, new_cursor, session)
                    await session.commit()
                except Exception:
                    try:
                        await session.rollback()
                    except Exception:
                        pass
            else:
                # commit if evaluations are not committed
                try:
                    await session.commit()
                except Exception:
                    try:
                        await session.rollback()
                    except Exception:
                        pass

        # 3) tclk/1 task-marketplace surveillance (opt-in, read-only, zero LLM)
        if self._tclk_rooms:
            processed += await self._poll_tclk(session)

        return processed

    async def _poll_tclk(self, session) -> int:
        """Read-only tclk/1 marketplace pass: parse signed-lane frames, persist
        masked rows, log safe summaries. Zero LLM calls (usage-friendly) and
        reveal/preimage values are never stored or logged."""
        from connectors.tclk import parse_frame
        from observability.models import TclkFrameRow

        processed = 0
        for room in self._tclk_rooms:
            try:
                cursor = await self._connector.get_cursor(f"tclk:{room}", session)
            except Exception:
                cursor = 0
            try:
                data = await self._connector.read_room(room, since=cursor, wait=2, session=session)
            except Exception as e:
                log.debug("tclk read skipped room=%s err=%s", room, type(e).__name__)
                continue
            messages = data.get("messages", []) or []
            max_seq = cursor
            for m in messages:
                if not isinstance(m, dict):
                    continue
                try:
                    seq = int(m.get("seq", 0) or 0)
                except Exception:
                    seq = 0
                if seq <= 0:
                    continue
                max_seq = max(max_seq, seq)
                frame = parse_frame(
                    str(m.get("text", "") or ""),
                    author=str(m.get("from", "") or m.get("did", "") or ""),
                    signed=bool(m.get("sig")),
                )
                if frame is None:
                    continue
                try:
                    session.add(
                        TclkFrameRow(
                            room=room,
                            seq=seq,
                            kind=frame.kind,
                            author=frame.author[:80],
                            signed=frame.signed,
                            contract=frame.contract[:80],
                            ref=frame.ref[:80],
                            rail=frame.rail[:40],
                            asset=frame.asset[:20],
                            amount=frame.amount[:40],
                            summary=frame.safe_summary()[:300],
                        )
                    )
                    processed += 1
                    if frame.kind in ("offer", "accept", "lock"):
                        log.info("tclk %s", frame.safe_summary())
                except Exception as e:
                    log.debug("tclk persist skipped room=%s seq=%s %s", room, seq, type(e).__name__)
                # --- Post-persist actions (opt-in): claim radar + safe agent mode ---
                dr = frame.deal_room()
                slug16 = dr[len("mb-p-tclk-"):] if dr else ""
                if slug16 and (self._tclk_agent_armed or settings.TCLK_CLAIM_RADAR_ENABLED):
                    try:
                        if frame.kind == "lock":
                            rail = frame.rail
                            if rail:
                                self._tclk_lock_rail[slug16] = rail
                            if self._tclk_agent_armed:
                                await self._tclk_on_lock(session, slug16, frame)
                        elif frame.kind == "reveal":
                            rail = self._tclk_lock_rail.get(slug16, "")
                            if settings.TCLK_CLAIM_RADAR_ENABLED and rail in self._tclk_claim_rails and slug16 not in self._tclk_claimed:
                                self._tclk_claimed.add(slug16)
                                ours = any(p.get("slug") == slug16 for p in self._tclk_active.values())
                                await self._tclk_alert_claim(slug16, rail, ours=ours)
                        elif frame.kind == "offer" and self._tclk_agent_armed:
                            await self._tclk_on_offer(session, frame)
                    except Exception as e:
                        log.warning("tclk action failed kind=%s seq=%s: %s", frame.kind, seq, type(e).__name__)
            if max_seq > cursor:
                try:
                    await self._connector.set_cursor(f"tclk:{room}", max_seq, session)
                    await session.commit()
                except Exception:
                    try:
                        await session.rollback()
                    except Exception:
                        pass
        return processed

    # --- tclk/1 safe agent: accept → verify lock → inline task → reveal ---
    async def _tclk_on_offer(self, session, frame) -> None:
        from connectors.tclk import build_accept, new_hashlock, offer_allows

        ok, why = offer_allows(
            frame,
            settings.TCLK_AGENT_RAILS,
            settings.TCLK_AGENT_MAX_AMOUNT,
            settings.TCLK_AGENT_TASK_PATTERNS,
        )
        if not ok:
            log.info("tclk offer skipped: %s", why)
            return
        if _offer_expired(frame):
            log.info("tclk offer skipped: expired")
            return
        if len(self._tclk_active) >= settings.TCLK_AGENT_MAX_ACTIVE:
            log.info("tclk agent busy (%d active)", len(self._tclk_active))
            return
        nonce = str(frame.data.get("nonce", "") or "")
        if not nonce:
            return
        key = f"{frame.author}|{nonce}"
        if key in self._tclk_seen:
            return
        self._tclk_seen.add(key)
        preimage, statement = new_hashlock()
        ref = nonce if nonce.startswith("0x") else f"0x{nonce}"
        self._tclk_active[ref] = {
            "ref": ref,
            "slug": "",
            "preimage": preimage,
            "statement": statement,
            "accepted_at": time.time(),
            "amount": frame.amount,
            "asset": frame.asset,
            "offer_author": frame.author[:40],
            "locked": False,
            "spec": _tclk_spec_short(frame),
        }
        try:
            await self._connector.signed_post(self._tclk_rooms[0], build_accept(ref, statement))
            log.info("tclk agent ACCEPT posted ref=%s amount=%s %s", ref, frame.amount, frame.asset)
            from connectors.agent_alert import send_telegram_text

            await send_telegram_text(
                f"🤝 LUMI tclk görevi kabul etti: {frame.amount} {frame.asset or '?'} — "
                f"ref {ref}, iş: {_tclk_spec_short(frame)}"
            )
        except Exception as e:
            log.warning("tclk accept post failed: %s", type(e).__name__)
            self._tclk_active.pop(ref, None)

    async def _tclk_on_lock(self, session, slug16, frame) -> None:
        for ref, p in list(self._tclk_active.items()):
            if p.get("locked") or p.get("slug"):
                continue
            if not _ref_matches(ref, frame.ref) and not _ref_matches(p.get("ref", ""), frame.ref):
                continue
            if frame.rail not in self._tclk_claim_rails:
                log.info("tclk lock rail rejected: %s", frame.rail)
                continue
            p["locked"] = True
            p["slug"] = slug16
            await self._tclk_do_task_and_reveal(session, slug16, p)
            return

    async def _tclk_do_task_and_reveal(self, session, slug16, pending) -> None:
        from connectors.tclk import build_reveal

        deal_room = f"mb-p-tclk-{slug16}"
        digest = await self._tclk_digest(session)
        log.info("tclk agent task done (inline, zero-LLM): deal=%s payload=%s", deal_room, digest[:120])
        try:
            await self._connector.signed_post(deal_room, build_reveal(str(pending["preimage"])))
            log.info("tclk agent REVEAL posted deal=%s ref=%s", deal_room, pending["ref"])
            from connectors.agent_alert import send_telegram_text

            await send_telegram_text(
                f"💰 LUMI görevi tamamladı + escrow claim: {pending['amount']} "
                f"{pending['asset'] or '?'} — deal odası {deal_room}"
            )
        except Exception as e:
            log.warning("tclk reveal post failed: %s", type(e).__name__)

    async def _tclk_digest(self, session) -> str:
        """Zero-LLM 24h tclk market digest — from our own DB, no network calls."""
        try:
            from sqlalchemy import func, select, text

            from observability.models import TclkFrameRow

            rows = (
                await session.execute(
                    select(TclkFrameRow.kind, func.count())
                    .where(
                        TclkFrameRow.created_at
                        >= func.now() - text("interval '24 hours'")
                    )
                    .group_by(TclkFrameRow.kind)
                )
            ).all()
            parts = " ".join(f"{k}={c}" for k, c in sorted(rows, key=lambda r: -r[1]))
            return f"24h tclk: {parts or 'veri yok'}"
        except Exception as e:
            log.debug("tclk digest failed: %s", type(e).__name__)
            return "24h tclk: veri yok"

    async def _tclk_alert_claim(self, slug16, rail, ours: bool = False) -> None:
        from connectors.agent_alert import send_telegram_text

        who = "LUMI kendi görevi" if ours else "dış ajan"
        msg = (
            f"🏁 tclk CLAIM RADAR: *{rail}* rail'inde gerçek ödeme tamamlandı "
            f"({who}) — deal odası `mb-p-tclk-{slug16}`"
        )
        ok = await send_telegram_text(msg)
        if ok and ours:
            self._tclk_active = {k: v for k, v in self._tclk_active.items() if v.get("slug") != slug16}

    async def run_forever(self) -> None:
        """Interval 15s loop — run via create_task at scheduler startup."""
        while True:
            try:
                from observability.db import async_session_factory

                async with async_session_factory() as _s:
                    await self.poll_once(_s)
            except Exception as e:
                log.warning("agent_scorer loop error: %s", type(e).__name__)
            await asyncio.sleep(self.interval)


# --- tclk/1 safe-agent helpers (thin wrappers over connectors.tclk) ---
def _ref_matches(a: str, b: str) -> bool:
    from connectors.tclk import ref_matches

    return ref_matches(a, b)


def _offer_expired(frame) -> bool:
    from connectors.tclk import offer_expired

    return offer_expired(frame)


def _tclk_spec_short(frame) -> str:
    from connectors.tclk import spec_short

    return spec_short(frame)
