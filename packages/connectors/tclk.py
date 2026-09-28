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
FRAME_KINDS = ("offer", "accept", "lock", "reveal", "refund", "cancel", "heartbeat")
DEAL_ROOM_PREFIX = "mb-p-tclk-"
# Fields we are allowed to persist/log per kind (everything else is dropped).
_KIND_SAFE_FIELDS = {
    "offer": ("type", "amount", "asset", "rail", "nonce", "spec"),
    "accept": ("type", "ref", "contract"),
    "lock": ("type", "contract", "ref", "rail"),
    "reveal": ("type", "contract"),
    "refund": ("type", "contract", "ref"),
    "heartbeat": ("type", "contract", "nonce"),
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


def claim_seed(raw_key: bytes) -> bytes:
    """Purpose-bound subkey for derived claim secrets (never the signing key itself)."""
    import hashlib
    import hmac

    return hmac.new(bytes(raw_key)[:32], b"tclk/1|claim-seed", hashlib.sha256).digest()


def derived_hashlock(offer_id: str, seed: bytes) -> tuple[str, str]:
    """Claim secret derived from the offer id — so a lock is claimable later.

    A random preimage lives only in the process that minted it: after a restart,
    or once a lock lands past the active TTL, the escrow could never be claimed.
    Deriving preimage = HMAC(seed, offer id) keeps the secret out of the database
    and out of every log while staying recomputable from our own audit row
    (which stores the offer id). sha256(preimage) is the public statement.
    """
    import hashlib
    import hmac

    preimage = hmac.new(seed, f"tclk/1|claim|{offer_id}".encode(), hashlib.sha256).digest()
    return f"0x{preimage.hex()}", f"0x{hashlib.sha256(preimage).hexdigest()}"


def new_nonce() -> str:
    """Fresh 16-hex frame nonce (venue duplicate filter wants a new one per frame)."""
    return secrets.token_hex(8)


# ── tclk/1 canonical ids (normative port of flop-labs/tclk src/frames.ts) ────
# SPEC.md §3.1-3.2: every later frame names the contract by a hash over the
# offer AND the acceptance, so a payer can only match an accept that carries the
# same id it computed. Getting this wrong is silent: the accept is published,
# nothing ever locks against it, and no value moves.
TCLK_DOMAIN = "FLOP::tclk::v1"


def _js_json(value: Any) -> str:
    """JS JSON.stringify for scalars == python ensure_ascii output."""
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, compact, non-ASCII escaped (toAscii)."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return _js_json(value)
    if isinstance(value, list):
        return "[" + ",".join(canonical_json(v) for v in value) + "]"
    return "{" + ",".join(
        f"{_js_json(k)}:{canonical_json(value[k])}" for k in sorted(value)
    ) + "}"


def domain_hash(tag: str, payload: str) -> str:
    import hashlib

    return "0x" + hashlib.sha256(f"{TCLK_DOMAIN}|{tag}|{payload}".encode()).hexdigest()


def offer_id(fields: dict) -> str:
    """`id` = sha256 over the domain-tagged canonical offer fields (without id)."""
    return domain_hash("offer", canonical_json({k: v for k, v in fields.items() if k != "id"}))


def contract_id(offer: dict, core: dict) -> str:
    """`contract` = sha256 over canonical {offer, accept-core}.

    core = {from, ref, statement, nonce, paymentKey?} — the accept fields the id
    commits to. Both sides recompute it; a mismatch rejects the frame.
    """
    return domain_hash("contract", canonical_json({"offer": offer, "accept": core}))


def deal_room(contract: str) -> str:
    """Post-accept frames live in mb-p-tclk-<first 16 hex of the contract id>."""
    hexed = contract[2:] if contract.startswith("0x") else contract
    return f"mb-p-tclk-{hexed[:16]}"


def state_note(contract: str) -> tuple[str, str]:
    """CAS state pointer is sharded: kv/tclk-<hh>/<next 14 hex>."""
    hexed = contract[2:] if contract.startswith("0x") else contract
    return hexed[:2], hexed[2:16]


def build_accept(*, sender: str, ref: str, statement: str, contract: str, nonce: str) -> str:
    """Signed-lane accept: ref = the offer id, contract = the derived contract id.

    All six required fields travel INSIDE the frame (from, ref, statement,
    contract, nonce, type) — the venue's transport from/nonce are separate and
    do not substitute for them.
    """
    return build_frame(
        "accept",
        **{"from": sender},
        ref=ref,
        statement=statement,
        contract=contract,
        nonce=nonce,
    )


def build_heartbeat(*, sender: str, contract: str, nonce: str, note: str = "") -> str:
    """Signed liveness signal (either party, accepted/locked). Creates the deal room."""
    fields: dict[str, Any] = {"contract": contract, "nonce": nonce}
    if note:
        fields["note"] = note
    return build_frame("heartbeat", **{"from": sender}, **fields)


def build_delivery(*, contract: str, body: str) -> str:
    """The deliverable: one signed message in the deal room.

    There is no `deliver` frame type (§ frame table) — the work ships as an
    ordinary signed message in the deal room and `reveal` is what claims the
    escrow. The contract id leads the line so the payer can match it.
    """
    return f"tclk-deliver {contract} :: {body}".strip()


def build_reveal(secret: str) -> str:
    """Revealing the preimage IS the claim — publish only after we verified a lock."""
    return build_frame("reveal", secret=secret)


def offer_raw_spec(frame: TclkFrame) -> str:
    """The offer's task brief exactly as published (job.context, else spec/task/desc)."""
    if frame.kind != "offer":
        return ""
    d = frame.data
    spec = str(d.get("spec", "") or d.get("task", "") or d.get("desc", "") or "")
    job = d.get("job")
    if isinstance(job, dict) and not spec:
        spec = str(job.get("context", "") or "")
        proto = str(job.get("proto", "") or "")
        # A proto is a transport hint, not a task. An offer carrying only a proto
        # has no brief at all, so it must stay spec-less and let policy refuse it
        # rather than pass the capability check on the proto's name.
        if proto and spec:
            spec = f"{proto} {spec}"
    return spec


def offer_job_pointer(frame: TclkFrame) -> tuple[str, str]:
    """``(proto, job id)`` of an offer's `job` marker, empty when it has none.

    An offer carrying a `job` marker with no inline brief states the work only
    by reference: the id names a JOB published in the venue's task room. That
    pointer MUST be resolved before the offer can be answered — treating it as
    an empty brief is what turned every such accept into a silent `no_answer`.
    """
    if frame.kind != "offer":
        return ("", "")
    d = frame.data if isinstance(frame.data, dict) else {}
    job = d.get("job")
    if not isinstance(job, dict):
        return ("", "")
    return (
        str(job.get("proto", "") or "").strip(),
        str(job.get("id", "") or "").strip(),
    )


def offer_spec(frame: TclkFrame) -> str:
    """Normalized task description of an offer, lowercase.

    The market states the work in `job.context` (often the whole brief, sometimes
    a /kv/ pointer) — reading only spec/task/desc made every real job look
    spec-less and pushed the accept path onto a default task nobody asked for.
    """
    return re.sub(r"[^a-z0-9 ,._-]", " ", offer_raw_spec(frame).lower())


def offer_rails(frame: TclkFrame) -> list[str]:
    """Rails an offer will settle on. The market uses the plural `rails` list;
    the singular `rail` is only what lock frames carry."""
    d = frame.data if isinstance(frame.data, dict) else {}
    rails = d.get("rails")
    if isinstance(rails, list):
        return [str(r).strip().lower() for r in rails if str(r).strip()]
    rail = str(d.get("rail", "") or "").strip().lower()
    return [rail] if rail else []


def offer_difficulty(frame: TclkFrame) -> int:
    """Stated difficulty `[difficulty n/m]`, or 0 when the offer does not say."""
    m = re.search(r"difficulty\s+(\d+)\s*/\s*(\d+)", offer_raw_spec(frame), re.IGNORECASE)
    return int(m.group(1)) if m else 0


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


def offer_allows(
    frame: TclkFrame,
    allowed_rails: str,
    max_amount: str,
    patterns: str,
    accept_specless: bool = False,
    max_difficulty: int = 0,
) -> tuple[bool, str]:
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
    offered = offer_rails(frame)
    if not offered:
        asset = str(d.get("asset", "") or "").upper()
        if asset == "FLOP" and ALLOW_RAIL_DEFAULT not in rails:
            return False, "flop offers disabled"
        if asset not in ("FLOP", ""):
            return False, f"no rail and non-FLOP asset: {asset}"
    elif not (set(offered) & rails):
        return False, f"rails {','.join(offered)} outside allowed {allowed_rails}"
    try:
        amount = int(str(d.get("amount", "0") or 0))
    except ValueError:
        return False, "unparseable amount"
    if amount <= 0:
        return False, "non-positive amount"
    max_a = int(max_amount or 0) or 1_000_000
    if amount > max_a:
        return False, f"amount {amount} over cap {max_a}"
    if max_difficulty and offer_difficulty(frame) > max_difficulty:
        return False, f"difficulty {offer_difficulty(frame)} over ceiling {max_difficulty}"
    want = {w.strip() for w in (patterns or "").split(",") if w.strip()}
    if not want:
        return False, "no capability patterns configured"
    spec = offer_spec(frame)
    # spec is usually a compact blob like "scan tclk-offers and report stats";
    # accept when ANY capability keyword appears in it. Spec-less offers (the
    # overwhelming majority of this market) pass ONLY when the operator opts in;
    # they then map onto our default capability: the read-only market digest.
    if not any(w in spec for w in want) and not accept_specless:
        return False, f"task not in capabilities: {spec[:60] or '(bos)'}"
    return True, "ok"


# ---------------------------------------------------------------------------
# Offer security audit ("denetim")
# ---------------------------------------------------------------------------
# Risk ranks are ordered least→most dangerous; an operator sets the highest rank
# they are willing to accept (TCLK_AGENT_MIN_TIER).
RISK_ORDER = ("safe", "watch", "risky", "dangerous")


def _risk_ok(risk: str, max_risk: str) -> bool:
    """True when `risk` is at or below the operator's accepted ceiling."""
    try:
        return RISK_ORDER.index(risk) <= RISK_ORDER.index((max_risk or "safe").lower())
    except ValueError:
        return False  # unknown tier anywhere → fail closed


def audit_offer(
    frame: TclkFrame,
    *,
    allowed_rails: str,
    max_amount: str,
    patterns: str,
    accept_specless: bool = False,
    max_difficulty: int = 0,
) -> dict[str, Any]:
    """Full deterministic security audit of ONE incoming offer.

    Never raises and never performs I/O: the caller persists the returned record
    whether the offer is accepted or rejected, so the audit trail exists for
    every offer the market produced — not just the ones we acted on.

    Returns a JSON-serialisable dict:
      decision     "accept" | "skip"
      reason       human-readable verdict line (masked, no secrets)
      risk         safe | watch | risky | dangerous
      spec         the offer's task spec ("" when absent)
      spec_missing True when the offer carried no spec at all
      checks       per-check detail, for the audit record
    """
    d = frame.data if isinstance(frame.data, dict) else {}
    spec = offer_spec(frame) if frame.kind == "offer" else ""
    checks: dict[str, Any] = {
        "kind": frame.kind,
        "signed": bool(frame.signed),
        "rail": str(d.get("rail", "") or ""),
        "rails": offer_rails(frame),
        "asset": str(d.get("asset", "") or ""),
        "amount": str(d.get("amount", "") or ""),
        "difficulty": offer_difficulty(frame),
        "spec_missing": not spec,
    }

    def out(decision: str, risk: str, reason: str) -> dict[str, Any]:
        checks["decision"] = decision
        checks["risk"] = risk
        return {
            "decision": decision,
            "reason": reason[:200],
            "risk": risk,
            "spec": spec[:200],
            "spec_missing": not spec,
            "checks": checks,
        }

    if frame.kind != "offer":
        return out("skip", "dangerous", "not an offer")
    if not frame.signed:
        # An unsigned frame is data, not a commitment: nothing to escrow against.
        return out("skip", "dangerous", "unsigned offer is data, not a commitment")

    rails = {r.strip() for r in (allowed_rails or "").split(",") if r.strip()}
    if not rails:
        return out("skip", "dangerous", "no rails configured")
    offered = offer_rails(frame)
    if not offered:
        asset = str(d.get("asset", "") or "").upper()
        if asset == "FLOP" and ALLOW_RAIL_DEFAULT not in rails:
            return out("skip", "risky", "flop offers disabled")
        if asset not in ("FLOP", ""):
            return out("skip", "risky", f"no rail and non-FLOP asset: {asset}")
    elif not (set(offered) & rails):
        return out("skip", "risky", f"rails {','.join(offered)} outside allowed {allowed_rails}")

    if str(d.get("role", "") or "").strip().lower() == "payee":
        # SPEC §frames: `role` is the side the SENDER takes. An offer from a
        # payee asks us to pay it — accepting would put our own funds in
        # escrow. We never take the paying side of someone else's deal.
        return out("skip", "risky", "sender takes payee side — we would be the payer")

    try:
        amount = int(str(d.get("amount", "0") or 0))
    except ValueError:
        return out("skip", "risky", "unparseable amount")
    if amount <= 0:
        return out("skip", "risky", "non-positive amount")
    max_a = int(max_amount or 0) or 1_000_000
    if amount > max_a:
        return out("skip", "risky", f"amount {amount} over cap {max_a}")

    if offer_expired(frame):
        return out("skip", "risky", "offer expired")

    if max_difficulty and offer_difficulty(frame) > max_difficulty:
        return out(
            "skip", "risky", f"difficulty {offer_difficulty(frame)} over ceiling {max_difficulty}"
        )

    want = {w.strip() for w in (patterns or "").split(",") if w.strip()}
    if not want:
        return out("skip", "dangerous", "no capability patterns configured")
    if spec and not any(w in spec for w in want):
        return out("skip", "risky", f"task not in capabilities: {spec[:60]}")
    if not spec and not accept_specless:
        return out("skip", "risky", "task not in capabilities: (bos)")

    # Everything hard-checked and clean. A spec-less offer stays "watch" — we
    # accept it under policy, but the missing spec is still recorded as risk.
    if not spec:
        return out("accept", "watch", "ok (spec yok — varsayilan is: market digest)")
    return out("accept", "safe", "ok")