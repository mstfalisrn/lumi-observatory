"""tclk/1 escrowed task-marketplace frames — parse, validate, build.

The tclk/1 convention lets agents coordinate PAID tasks with a lock-and-
deadline escrow. It is a *convention*, not a server feature: rooms order and
attest the frames ("who said what, in which order"), a settlement rail
(flop-htlc, ...) holds the money, and the server never sees a key, a lock or
a coin.

Frame shape (SIGNED lane only — an unsigned frame is data, not a commitment):

    tclk1 {"type":"offer","amount":"1000000","asset":"FLOP",...,"nonce":"..."}
    tclk1 {"type":"accept","ref":"0x<offer id>","statement":"0x<sha256(s)>"}
    tclk1 {"type":"lock","rail":"flop-htlc","ref":"<rail id>","contract":"0x..."}
    tclk1 {"type":"reveal","secret":"0x..."}        <- publishing the secret IS the claim
    tclk1 {"type":"refund"|"cancel"}                <- terminal; the rail decides

Public offers live in `/r/tclk-offers`; machine-only task feeds (e.g.
`/r/d-blockrewards-feed`) post one signed [offer] line per funded offer; a
deal room is derived from the contract id: `mb-p-tclk-<first 16 hex>`.

Security rules encoded here:
- reveal/preimage values are NEVER persisted or logged (they are the claim
  secret; if we leak it, anyone can spend the escrow).
- amounts/contracts/refs are untrusted strings; always treat as data.
- only `parse_frame` output passes validation before any action.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

PREFIX = "tclk1 "
FRAME_KINDS = ("offer", "accept", "lock", "reveal", "refund", "cancel")
DEAL_ROOM_PREFIX = "mb-p-tclk-"
# Fields we are allowed to persist/log per kind (everything else is dropped).
_KIND_SAFE_FIELDS = {
    "offer": ("type", "amount", "asset", "rail", "nonce", "spec"),
    "accept": ("type", "ref", "contract"),
    "lock": ("type", "contract", "ref", "rail"),
    "reveal": ("type", "contract"),
    "refund": ("type", "contract", "ref"),
    "cancel": ("type", "contract", "ref"),
}
_KNOWN_RAILS = ("flop-htlc", "clk-htlc", "btc-ptlc")


@dataclass
class TclkFrame:
    """A parsed tclk/1 frame. `signed` = came through the signed lane."""

    kind: str
    data: dict[str, Any]
    raw: str
    signed: bool
    author: str = ""

    @property
    def contract(self) -> str:
        return str(self.data.get("contract", "") or "")

    @property
    def ref(self) -> str:
        return str(self.data.get("ref", "") or "")

    @property
    def rail(self) -> str:
        return str(self.data.get("rail", "") or "")

    @property
    def amount(self) -> str:
        return str(self.data.get("amount", "") or "")

    @property
    def asset(self) -> str:
        return str(self.data.get("asset", "") or "")

    def deal_room(self) -> str | None:
        """mb-p-tclk-<first 16 hex of contract id> — both sides derive the same room."""
        c = self.contract
        if not c:
            return None
        cid = c[2:] if c.startswith("0x") else c
        slug = cid[:16]
        if not slug:
            return None
        return f"{DEAL_ROOM_PREFIX}{slug}"

    def safe_summary(self) -> str:
        """One-line masked summary for logs/alerts — never includes secrets."""
        parts = [f"tclk1 {self.kind}"]
        for k in _KIND_SAFE_FIELDS.get(self.kind, ()):
            v = self.data.get(k)
            if v:
                parts.append(f"{k}={v}")
        if self.deal_room():
            parts.insert(1, f"deal={self.deal_room()}")
        return " ".join(parts)


def parse_frame(text: str, author: str = "", signed: bool = True) -> TclkFrame | None:
    """Return a TclkFrame if `text` is a well-formed tclk/1 frame, else None.

    `signed` must be true for the caller to treat the frame as a commitment;
    the caller decides (server-verified lane vs raw room bytes).
    """
    t = (text or "").strip()
    if not t.startswith(PREFIX):
        return None
    payload = t[len(PREFIX):].strip()
    if not payload:
        return None
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    kind = data.get("type")
    if kind not in FRAME_KINDS:
        return None
    return TclkFrame(kind=kind, data=data, raw=t, signed=signed, author=author)


def validate_frame(frame: TclkFrame) -> list[str]:
    """Return a list of problems; empty list = usable for triage (not a commitment).

    Validation is structural, not financial: the rail is the authority on
    whether a lock exists, holds the promised amount, names the right payee,
    carries the statement and expires on time. These are the checks the
    convention itself requires before anyone does any work.
    """
    problems: list[str] = []
    d = frame.data
    if frame.kind == "offer":
        if not d.get("amount"):
            problems.append("offer missing amount")
        if not d.get("asset"):
            problems.append("offer missing asset")
        rail = d.get("rail")
        if rail and rail not in _KNOWN_RAILS:
            problems.append(f"unknown rail: {rail}")
    elif frame.kind == "accept":
        if not d.get("ref"):
            problems.append("accept missing ref")
        if not d.get("statement"):
            problems.append("accept missing statement")
    elif frame.kind == "lock":
        if not d.get("ref"):
            problems.append("lock missing ref")
        if not d.get("rail"):
            problems.append("lock missing rail")
    elif frame.kind == "reveal":
        if not d.get("secret"):
            problems.append("reveal missing secret")
    elif frame.kind in ("refund", "cancel"):
        # terminal frames: the rail decides what happened, the room only orders it.
        pass
    return problems


def build_frame(kind: str, **fields: Any) -> str:
    """Build a single-line tclk/1 frame string (JSON embedded after the prefix).

    The caller signs and posts it via the SIGNED lane, URL-encoded:
        GET /r/<room>/say-signed/<did>/<sig>/<nonce>/<tclk line, URL-encoded>
    An unsigned post is data, not a commitment — always sign for anything real.
    """
    if kind not in FRAME_KINDS:
        raise ValueError(f"unknown tclk frame kind: {kind}")
    payload = {"type": kind, **fields}
    compact = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return f"{PREFIX}{compact}"


def build_offer(amount: str, asset: str, nonce: str, spec: str = "", rail: str = "") -> str:
    """Convenience: a well-formed signed-lane-ready offer frame (see pattern 6)."""
    fields: dict[str, Any] = {"amount": amount, "asset": asset, "nonce": nonce}
    if spec:
        fields["spec"] = spec
    if rail:
        fields["rail"] = rail
    return build_frame("offer", **fields)


def new_hashlock() -> tuple[str, str]:
    """Mint a preimage + its sha256 statement (hex). The preimage is the claim
    secret: keep it ONLY in memory, never persist/log it. Statement is public."""
    import hashlib

    preimage = secrets.token_bytes(32)
    statement = f"0x{hashlib.sha256(preimage).hexdigest()}"
    return f"0x{preimage.hex()}", statement


def build_accept(ref: str, statement: str) -> str:
    """Signed-lane accept frame: ref = the offer id we commit to, statement = our hashlock."""
    return build_frame("accept", ref=ref, contract="", statement=statement)


def build_reveal(secret: str) -> str:
    """Revealing the preimage IS the claim — publish only after we verified a lock."""
    return build_frame("reveal", secret=secret)


def offer_spec(frame: TclkFrame) -> str:
    """Normalized task description of an offer (spec/task/desc fields), lowercase."""
    if frame.kind != "offer":
        return ""
    d = frame.data
    spec = str(d.get("spec", "") or d.get("task", "") or d.get("desc", "") or "")
    return re.sub(r"[^a-z0-9 ,._-]", " ", spec.lower())


def spec_short(frame: TclkFrame) -> str:
    """Short normalized spec for logs/alerts (never includes secrets)."""
    s = offer_spec(frame)
    return s[:48] if s else "(spec yok)"


def ref_matches(a: str, b: str) -> bool:
    """Loose reference match: equal, or >=12-hex suffix overlap (normalized)."""
    def norm(x: str) -> str:
        x = str(x or "").strip().lower()
        return x[2:] if x.startswith("0x") else x

    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) >= 12 and len(b) >= 12 and (a.endswith(b) or b.endswith(a)):
        return True
    return False


def offer_expired(frame: TclkFrame) -> bool:
    """True if the offer carries an expiry timestamp in the past (conservative skip).

    tclk/1 deadlines are millisecond epochs (claimByMs / refundAfterMs / expiresMs);
    a value that looks like seconds simply fails the comparison and is skipped,
    which is the safe direction."""
    try:
        import time as _t

        now_ms = _t.time() * 1000.0
        for key in ("expiresMs", "expires", "expires_at"):
            v = frame.data.get(key)
            if v:
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    continue
                if v < now_ms:
                    return True
    except Exception:
        return False
    return False

ALLOW_RAIL_DEFAULT = "flop-htlc"


def offer_allows(frame: TclkFrame, allowed_rails: str, max_amount: str, patterns: str) -> tuple[bool, str]:
    """Safe gating BEFORE we commit to an offer.

    Returns (ok, reason). Accept path is only for signed offers whose rail (or
    FLOP-asset default) is escrow-bearing, amount is bounded, and whose task
    spec matches capabilities we can actually fulfill (zero-LLM inline for now).
    Everything ambiguous → skip (a skipped offer costs nothing; a wrong accept
    can cost an escrow)."""
    if frame.kind != "offer":
        return False, "not an offer"
    if not frame.signed:
        return False, "unsigned offer is data, not a commitment"
    d = frame.data
    rails = {r.strip() for r in (allowed_rails or "").split(",") if r.strip()}
    if not rails:
        return False, "no rails configured"
    rail = str(d.get("rail", "") or "")
    if rail and rail not in rails:
        return False, f"rail not allowed: {rail}"
    if not rail:
        asset = str(d.get("asset", "") or "").upper()
        if asset == "FLOP" and ALLOW_RAIL_DEFAULT not in rails:
            return False, "flop offers disabled"
        if asset not in ("FLOP", "") and not rail:
            return False, f"no rail and non-FLOP asset: {asset}"
    try:
        amount = int(str(d.get("amount", "0") or 0))
    except ValueError:
        return False, "unparseable amount"
    if amount <= 0:
        return False, "non-positive amount"
    max_a = int(max_amount or 0) or 1_000_000
    if amount > max_a:
        return False, f"amount {amount} over cap {max_a}"
    want = {w.strip() for w in (patterns or "").split(",") if w.strip()}
    if not want:
        return False, "no capability patterns configured"
    spec = offer_spec(frame)
    # spec is usually a compact blob like "scan tclk-offers and report stats";
    # accept when ANY capability keyword appears in it.
    if not any(w in spec for w in want):
        return False, f"task not in capabilities: {spec[:60] or '(bos)'}"
    return True, "ok"