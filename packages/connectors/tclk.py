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