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
        # Load the signing key once (scheduler shares the worker DID identity).
        if settings.TECHNOCORE_ENABLED and settings.TECHNOCORE_ED25519_KEY_PATH:
            try:
                self._connector.load_or_generate_key(settings.TECHNOCORE_ED25519_KEY_PATH)
                log.info("technocore DID ready: %s", getattr(self._connector, "did_public", ""))
            except Exception as e:
                log.warning("technocore key load failed: %s", str(e)[:150])

    async def poll_once(self, session) -> int:
        if not settings.TECHNOCORE_ENABLED:
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

        return processed

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
