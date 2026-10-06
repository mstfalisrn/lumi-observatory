"""Python port of the reference tclk fold (flop-labs/tclk machine.ts + foldTranscript).

Why: the blockrewards "protocol" family (the biggest judged family) asks for the
final status of a transcript folded with the reference rules. The reference fold
verifies transport signatures, but the task material only carries
`room | time | sender | frame line`, so this port keeps the machine semantics
(structural validation, room expectations, deadline guards, secret checks) and
takes the four given columns as authenticated.

Ported from https://github.com/flop-labs/tclk (Apache-2.0): src/machine.ts,
src/transcript.ts, src/frames.ts, src/locks.ts. Rules of the port: fail-closed
(never throws on a bad frame), rejections carry a reason, state only advances on
frames that pass the guards.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime

TCLK_DOMAIN = "FLOP::tclk::v1"
OFFER_ROOM = "tclk-offers"
TERMINAL = {"claimed", "refunded", "cancelled"}
TYPES = {"offer", "accept", "lock", "reveal", "refund", "cancel", "receipt", "heartbeat"}

DID_RE = re.compile(r"^did:key:z[1-9A-HJ-NP-Za-km-z]+$")
HEX32 = re.compile(r"^0x[0-9a-f]{64}$")
NONCE_RE = re.compile(r"^[0-9a-f]{8,64}$")
AMOUNT_RE = re.compile(r"^[0-9]+$")
ASSET_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
RAIL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")

# required / allowed keys per frame type (frames.ts FRAME_FIELDS, required subset)
FIELDS = {
    "offer": ({"type", "from", "role", "amount", "asset", "lock", "rails", "expiresMs", "claimByMs", "refundAfterMs", "nonce", "id"},
              {"paymentKey", "job"}),
    "accept": ({"type", "from", "ref", "statement", "contract", "nonce"}, {"paymentKey"}),
    "lock": ({"type", "from", "contract", "rail", "ref"}, {"presig"}),
    "reveal": ({"type", "from", "contract", "secret"}, {"ref"}),
    "refund": ({"type", "from", "contract"}, {"ref", "reason"}),
    "cancel": ({"type", "from", "contract"}, {"reason"}),
    "receipt": ({"type", "from", "contract", "outcome"}, {"rail", "ref"}),
    "heartbeat": ({"type", "from", "contract", "nonce"}, {"note"}),
}


def _fail(msg: str):
    raise ValueError(f"tclk: {msg}")


def _canon(value) -> str:
    """JS JSON.stringify-compatible canonical form (sorted keys, no spaces)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def domain_hash(tag: str, payload: str) -> str:
    return "0x" + hashlib.sha256(f"{TCLK_DOMAIN}|{tag}|{payload}".encode()).hexdigest()


def offer_id(fields: dict) -> str:
    return domain_hash("offer", _canon({k: v for k, v in fields.items() if k != "id"}))


def contract_id(offer: dict, core: dict) -> str:
    core = {k: v for k, v in core.items() if v is not None}
    return domain_hash("contract", _canon({"offer": offer, "accept": core}))


def deal_room(contract: str) -> str:
    return f"mb-p-tclk-{contract[2:18]}"


def verify_hash_preimage(hash_str: str, preimage: str) -> bool:
    try:
        raw = bytes.fromhex(preimage[2:] if preimage.startswith("0x") else preimage)
        if len(raw) != 32:
            return False
        return "0x" + hashlib.sha256(raw).hexdigest() == hash_str.lower()
    except Exception:
        return False


def _req_str(value, name, pattern=None):
    if not isinstance(value, str):
        _fail(f"{name} must be a string")
    if pattern is not None and not pattern.match(value):
        _fail(f"{name} is malformed")


def _req_ms(value, name) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _fail(f"{name} must be a non-negative integer")
    return value


def validate_frame(frame: dict) -> None:
    if not isinstance(frame, dict):
        _fail("frame must be an object")
    ftype = frame.get("type")
    if ftype not in TYPES:
        _fail(f"unknown frame type: {ftype}")
    required, optional = FIELDS[ftype]
    keys = set(frame)
    missing = required - keys
    if missing:
        _fail(f"{ftype}: missing {sorted(missing)}")
    extra = keys - required - optional
    if extra:
        _fail(f"{ftype}: unexpected {sorted(extra)}")
    _req_str(frame.get("from"), "from", DID_RE)

    if ftype == "offer":
        if frame.get("role") not in ("payer", "payee"):
            _fail("role must be payer|payee")
        _req_str(frame.get("amount"), "amount", AMOUNT_RE)
        _req_str(frame.get("asset"), "asset", ASSET_RE)
        if frame.get("lock") not in ("hash", "point"):
            _fail("lock must be hash|point")
        rails = frame.get("rails")
        if not isinstance(rails, list) or not rails:
            _fail("rails must be a non-empty array")
        for rail in rails:
            _req_str(rail, "rail", RAIL_RE)
        claim_by = _req_ms(frame.get("claimByMs"), "claimByMs")
        refund_after = _req_ms(frame.get("refundAfterMs"), "refundAfterMs")
        _req_ms(frame.get("expiresMs"), "expiresMs")
        if claim_by >= refund_after:
            _fail("claimByMs must be strictly before refundAfterMs")
        if frame.get("lock") == "point" and frame.get("paymentKey") is None:
            _fail("point locks require paymentKey")
        _req_str(frame.get("nonce"), "nonce", NONCE_RE)
        if frame.get("id") != offer_id(frame):
            _fail(f"offer id mismatch (expected {offer_id(frame)})")
    elif ftype == "accept":
        _req_str(frame.get("ref"), "ref", HEX32)
        _req_str(frame.get("statement"), "statement", re.compile(r"^0x(?:[0-9a-f]{64}|[0-9a-f]{66})$"))
        _req_str(frame.get("contract"), "contract", HEX32)
        _req_str(frame.get("nonce"), "nonce", NONCE_RE)
    elif ftype == "lock":
        _req_str(frame.get("contract"), "contract", HEX32)
        _req_str(frame.get("rail"), "rail", RAIL_RE)
        _req_str(frame.get("ref"), "ref")
    elif ftype == "reveal":
        _req_str(frame.get("contract"), "contract", HEX32)
        if frame.get("ref") is not None:
            _req_str(frame["ref"], "ref")
        _req_str(frame.get("secret"), "secret", HEX32)
    elif ftype == "refund":
        _req_str(frame.get("contract"), "contract", HEX32)
        if frame.get("ref") is not None:
            _req_str(frame["ref"], "ref")
        if frame.get("reason") is not None:
            _req_str(frame["reason"], "reason")
    elif ftype == "cancel":
        _req_str(frame.get("contract"), "contract", HEX32)
        if frame.get("reason") is not None:
            _req_str(frame["reason"], "reason")
    elif ftype == "receipt":
        _req_str(frame.get("contract"), "contract", HEX32)
        if frame.get("outcome") not in TERMINAL:
            _fail("outcome must be claimed|refunded|cancelled")
        if frame.get("rail") is not None:
            _req_str(frame["rail"], "rail", RAIL_RE)
        if frame.get("ref") is not None:
            _req_str(frame["ref"], "ref")
    elif ftype == "heartbeat":
        _req_str(frame.get("contract"), "contract", HEX32)
        _req_str(frame.get("nonce"), "nonce", NONCE_RE)
        if frame.get("note") is not None:
            _req_str(frame["note"], "note")


def _is_party(state: dict, did: str) -> bool:
    return did in (state.get("offer_from"), state.get("payer"), state.get("payee"))


def _rail_offered(rails: list, selected: str) -> bool:
    if selected in rails:
        return True
    target = selected.lower()
    return any(str(r).lower() == target for r in rails)


def open_contract(offer: dict) -> dict:
    validate_frame(offer)
    return {
        "status": "proposed",
        "offer": offer,
        "offer_from": offer["from"],
        "payer": offer["from"] if offer["role"] == "payer" else None,
        "payee": offer["from"] if offer["role"] == "payee" else None,
        "contract": None,
        "statement": None,
        "rail": None,
        "rail_ref": None,
    }


def apply_frame(state: dict, frame: dict, now_ms: int):
    """(ok, reason) — mutates a copy of state on success, mirroring machine.ts."""
    try:
        validate_frame(frame)
    except ValueError as exc:
        return False, str(exc)

    ftype = frame["type"]
    if ftype == "offer":
        return False, "contract is already open"

    if ftype == "accept":
        if state["status"] != "proposed":
            return False, f"accept in status {state['status']}"
        if frame["ref"] != state["offer"]["id"]:
            return False, "accept.ref names a different offer"
        if frame["from"] == state["offer_from"]:
            return False, "cannot accept own offer"
        if now_ms >= state["offer"]["expiresMs"]:
            return False, "offer has expired"
        expected = contract_id(state["offer"], {
            "from": frame["from"], "ref": frame["ref"],
            "statement": frame["statement"], "paymentKey": frame.get("paymentKey"),
            "nonce": frame["nonce"],
        })
        if frame["contract"] != expected:
            return False, "contract id mismatch"
        if state["offer"]["lock"] == "point" and frame.get("paymentKey") is None:
            return False, "point locks require the acceptor's paymentKey"
        acceptor_is_payer = state["offer"]["role"] == "payee"
        state["status"] = "accepted"
        state["contract"] = frame["contract"]
        state["statement"] = frame["statement"]
        if acceptor_is_payer:
            state["payer"] = frame["from"]
        else:
            state["payee"] = frame["from"]
        return True, None

    if ftype == "lock":
        if state["status"] != "accepted":
            return False, f"lock in status {state['status']}"
        if frame["contract"] != state["contract"]:
            return False, "lock names a different contract"
        if frame["from"] != state["payer"]:
            return False, "only the payer locks"
        if now_ms >= state["offer"]["refundAfterMs"]:
            return False, "refund window is already open"
        if not _rail_offered(state["offer"]["rails"], frame["rail"]):
            return False, f"rail {frame['rail']} was not offered"
        state["status"] = "locked"
        state["rail"] = frame["rail"]
        state["rail_ref"] = frame["ref"]
        return True, None

    if ftype == "reveal":
        if state["status"] != "locked":
            return False, f"reveal in status {state['status']}"
        if frame["contract"] != state["contract"]:
            return False, "reveal names a different contract"
        if frame.get("ref") is not None and frame["ref"] != state["rail_ref"]:
            return False, "reveal names a different rail ref"
        if frame["from"] != state["payee"]:
            return False, "only the payee reveals"
        if now_ms >= state["offer"]["refundAfterMs"]:
            return False, "refund window is open"
        if not verify_hash_preimage(state["offer"].get("statement") or state["statement"], frame["secret"]):
            return False, "secret does not open the statement"
        state["status"] = "claimed"
        state["secret"] = frame["secret"]
        return True, None

    if ftype == "refund":
        if state["status"] != "locked":
            return False, f"refund in status {state['status']}"
        if frame["contract"] != state["contract"]:
            return False, "refund names a different contract"
        if frame.get("ref") is not None and frame["ref"] != state["rail_ref"]:
            return False, "refund names a different rail ref"
        if frame["from"] != state["payer"]:
            return False, "only the payer refunds"
        if now_ms < state["offer"]["refundAfterMs"]:
            return False, "refund window not open yet"
        state["status"] = "refunded"
        return True, None

    if ftype == "cancel":
        if state["status"] not in ("proposed", "accepted"):
            return False, f"cancel in status {state['status']}"
        if state["status"] == "accepted" and frame["contract"] != state["contract"]:
            return False, "cancel names a different contract"
        if not _is_party(state, frame["from"]):
            return False, "cancel from a non-party"
        state["status"] = "cancelled"
        return True, None

    if ftype == "receipt":
        if state["status"] not in TERMINAL:
            return False, "receipt before a terminal status"
        if frame["contract"] != state["contract"]:
            return False, "receipt names a different contract"
        if not _is_party(state, frame["from"]):
            return False, "receipt from a non-party"
        if frame["outcome"] != state["status"]:
            return False, f"receipt outcome {frame['outcome']} does not match {state['status']}"
        if frame.get("rail") is not None and state.get("rail") is not None and frame["rail"] != state["rail"]:
            return False, f"receipt rail {frame['rail']} does not match contract rail {state['rail']}"
        if frame.get("ref") is not None and state.get("rail_ref") is not None and frame["ref"] != state["rail_ref"]:
            return False, "receipt ref does not match contract railRef"
        if state["status"] == "cancelled" and (frame.get("rail") is not None or frame.get("ref") is not None):
            return False, "receipt on cancelled contract cannot name a settlement rail"
        return True, None

    if ftype == "heartbeat":
        if state["status"] not in ("accepted", "locked"):
            return False, f"heartbeat in status {state['status']}"
        if frame["contract"] != state["contract"]:
            return False, "heartbeat names a different contract"
        if not _is_party(state, frame["from"]):
            return False, "heartbeat from a non-party"
        return True, None

    return False, f"unhandled frame type {ftype}"


LINE_RE = re.compile(r"^tclk1\s+(\{.*\})\s*$", re.S)


def decode_frame(line: str) -> dict:
    m = LINE_RE.match(line.strip())
    if not m:
        _fail("frame did not decode")
    try:
        return json.loads(m.group(1))
    except Exception:
        _fail("frame did not decode")


def fold(records: list[dict]) -> tuple[str | None, list[dict]]:
    """records: [{room, time_ms, sender, line}] in append order.

    Returns (status, rejections) where each rejection is {index, type, reason}.
    """
    state: dict | None = None
    rejections: list[dict] = []

    for index, rec in enumerate(records):
        room = str(rec.get("room") or "")
        try:
            frame = decode_frame(str(rec.get("line") or ""))
        except ValueError as exc:
            rejections.append({"index": index, "type": None, "reason": str(exc)})
            continue
        ftype = frame.get("type")
        if frame.get("from") != rec.get("sender"):
            rejections.append({"index": index, "type": ftype,
                               "reason": f"{ftype}.from does not match the record sender"})
            continue

        if state is None:
            if ftype != "offer":
                rejections.append({"index": index, "type": ftype, "reason": "no contract open yet"})
                continue
            if room != OFFER_ROOM:
                rejections.append({"index": index, "type": ftype,
                                   "reason": f"offer must be posted in {OFFER_ROOM}"})
                continue
            try:
                state = open_contract(frame)
            except ValueError as exc:
                rejections.append({"index": index, "type": ftype, "reason": str(exc)})
                state = None
            continue

        expected_room = OFFER_ROOM if (ftype in ("offer", "accept") or state.get("contract") is None) else deal_room(state["contract"])
        if room != expected_room:
            where = OFFER_ROOM if expected_room == OFFER_ROOM else f"the derived deal room {expected_room}"
            rejections.append({"index": index, "type": ftype, "reason": f"{ftype} must be posted in {where}"})
            continue

        try:
            ok, reason = apply_frame(state, frame, int(rec.get("time_ms") or 0))
        except ValueError as exc:
            ok, reason = False, str(exc)
        if not ok:
            rejections.append({"index": index, "type": ftype, "reason": reason})

    return (state or {}).get("status"), rejections


ROW_RE = re.compile(
    r"(?P<room>[A-Za-z0-9_-]+) \| (?P<ts>\d{4}-\d{2}-\d{2}T[0-9:.]+Z) \| "
    r"(?P<did>did:key:[1-9A-HJ-NP-Za-km-z]+) \| "
    r"(?P<line>tclk1 \{.*?\})(?= [A-Za-z0-9_-]+ \| \d{4}-|\s*$)",
    re.S,
)

_UTC_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _utc_milliseconds(iso_z: str) -> int:
    """Convert a source timestamp ending in ``Z`` to epoch milliseconds."""
    if not iso_z.endswith("Z"):
        raise ValueError("source timestamp must end in Z")
    stamp = datetime.fromisoformat(iso_z[:-1]).replace(tzinfo=UTC)
    delta = stamp - _UTC_EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000 + delta.microseconds // 1_000


def parse_material(text: str) -> list[dict] | None:
    """Rows: `<room> | <iso ts> | <did> | tclk1 {json}`, one per record, in order."""
    rows: list[dict] = []
    for m in ROW_RE.finditer(text):
        try:
            time_ms = _utc_milliseconds(m.group("ts"))
        except ValueError:
            return None
        rows.append({
            "room": m.group("room"),
            "time_ms": time_ms,
            "sender": m.group("did"),
            "line": m.group("line").strip(),
        })
    return rows or None


def answer(brief: str) -> str | None:
    """One-line answer for a protocol-fold brief: '<status> <rejection sentence|no rejected records>'."""
    m = re.search(r"MATERIAL:\s*(.*)\Z", brief, re.S)
    if not m or "tclk1 " not in m.group(1):
        return None
    rows = parse_material(m.group(1).strip())
    if not rows:
        return None
    status, rejections = fold(rows)
    if status is None:
        return None
    if not rejections:
        return f"{status} no rejected records"
    first = rejections[0]
    kind = first.get("type") or "frame"
    return f"{status} rejected {kind} frame: {first['reason']}"
