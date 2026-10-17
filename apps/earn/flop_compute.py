#!/usr/bin/env python3
"""FLOP testnet compute-spend path — the only lane that accrues agent-cohort credit.

Why this exists (yellowpaper v0.5 §8.2, R8.7/R8.8; decisions D-0514/0515/0517/0522):

  * An Agent grant's still-locked principal MAY be spent only through four calls —
    ``agent_transfer``, ``open_channel``, ``top_up_escrow`` and ``force_open`` — and
    only for compute.
  * Spending is not what pays: only a settled ``payable`` accrues credit. The spec is
    explicit that locked principal spent through ``agent_transfer``, through a
    compute-channel refund, through the R12.1d penalty or through any other leg
    OUTSIDE the payable MUST NOT accrue credit.
  * Credit for a payable is ``floor(K / 3)`` where ``K`` is the locked part of the
    payables credited to this DID: three FLOP of compute spend unlock one liquid FLOP.
  * A payable is credited only once its session settlement is final AND the seven-day
    dispute window has closed with no upheld fraud dispute.
  * Appendix H.4 still lists the Agent spend credit and the spend cap as open, so the
    protocol does not enforce the three-quarters ceiling yet. The ceiling is therefore
    enforced HERE, locally, BEFORE a call is made — spending past it would burn locked
    principal for no credit.

What is deliberately missing: the public testnet client. ``flop-labs`` has published no
client or node release, so no call can be made and this module fails closed on every real
spend (``--spend``) while still planning, projecting and recording intent. Wire the
released client into :func:`client_call` when it ships; nothing else here should change.

Controls:
  FLOP_COMPUTE_ENABLED   default false — live spends are refused while false
  FLOP_GRANT_PRINCIPAL   locked principal of the Agent grant (FLOP)
  FLOP_SPEND_CAP_RATIO   local ceiling as a fraction of principal (default 0.75)
  FLOP_TESTNET_RPC       testnet endpoint once published (unset today)
  FLOP_CLI               testnet client binary once published (unset today)
  LUMI_EARN_STATE_DIR    default /var/lib/lumi-earn

State: ``<state dir>/flop-compute.jsonl`` — append-only ledger (intent, dry run, blocked
call, spend, refund, settlement, credit). No balances are ever written: the ledger records
what was attempted and what the network settled, never a claimed number.

Usage:
  python apps/earn/flop_compute.py --status
  python apps/earn/flop_compute.py --plan 1200
  python apps/earn/flop_compute.py --dry-run --call open_channel --amount 40
  python apps/earn/flop_compute.py --spend --call top_up_escrow --amount 40
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

SPENDABLE_CALLS = ("agent_transfer", "open_channel", "top_up_escrow", "force_open")
# Only the legs that land in a payable accrue credit; agent_transfer is explicitly
# outside the payable (§8.2), so the projection below counts compute legs only.
COMPUTE_CREDIT_CALLS = ("open_channel", "top_up_escrow", "force_open")
CREDIT_DIVISOR = 3
DEFAULT_CAP_RATIO = 0.75
DISPUTE_WINDOW_DAYS = 7
LEDGER_NAME = "flop-compute.jsonl"
DEFAULT_STATE_DIR = "/var/lib/lumi-earn"


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def state_dir() -> Path:
    return Path(os.environ.get("LUMI_EARN_STATE_DIR", DEFAULT_STATE_DIR))


def flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def principal_from_env() -> int:
    raw = os.environ.get("FLOP_GRANT_PRINCIPAL", "0").strip()
    try:
        return max(0, int(float(raw)))
    except ValueError:
        return 0


def cap_ratio_from_env() -> float:
    raw = os.environ.get("FLOP_SPEND_CAP_RATIO", str(DEFAULT_CAP_RATIO)).strip()
    try:
        ratio = float(raw)
    except ValueError:
        return DEFAULT_CAP_RATIO
    return ratio if 0.0 <= ratio <= 1.0 else DEFAULT_CAP_RATIO


def spend_cap(principal: int, ratio: float = DEFAULT_CAP_RATIO) -> int:
    """Local ceiling: three quarters of principal by default (D-0522)."""
    if principal < 0:
        raise ValueError("principal must be >= 0")
    if not 0.0 <= ratio <= 1.0:
        raise ValueError("cap ratio must be within [0, 1]")
    return int(principal * ratio)


def credit_for_payable(k_locked: int) -> int:
    """Credit unlocked by one credited payable: floor(K / 3)."""
    if k_locked < 0:
        raise ValueError("locked part must be >= 0")
    return k_locked // CREDIT_DIVISOR


def is_creditable(
    call: str,
    settlement_final: bool,
    dispute_window_closed: bool,
    upheld_disputes: int,
) -> bool:
    """A payable is credited only when its leg can credit at all and its window is clean."""
    if call not in COMPUTE_CREDIT_CALLS:
        return False
    if not settlement_final or not dispute_window_closed:
        return False
    return upheld_disputes == 0


def plan(principal: int, spent_flop: int = 0, ratio: float = DEFAULT_CAP_RATIO) -> dict:
    cap = spend_cap(principal, ratio)
    spent = max(0, spent_flop)
    remaining = max(0, cap - spent)
    return {
        "principal_flop": principal,
        "cap_ratio": ratio,
        "cap_flop": cap,
        "spent_flop": spent,
        "remaining_flop": remaining,
        # At the cap the whole spend lands in payables, so the unlock is floor(cap / 3),
        # i.e. a quarter of principal — "three quarters spent on compute, one quarter
        # becomes liquid" (D-0522).
        "projected_unlock_flop": credit_for_payable(cap),
        "dispute_window_days": DISPUTE_WINDOW_DAYS,
        "note": (
            "cap is enforced locally: Appendix H.4 still lists the protocol spend cap and "
            "the Agent spend credit as open, and R8.8's payable-first draw is not applied yet"
        ),
    }


def client_available() -> bool:
    """The public testnet client is not published: both controls are unset today."""
    rpc = os.environ.get("FLOP_TESTNET_RPC", "").strip()
    cli = os.environ.get("FLOP_CLI", "").strip()
    if rpc and rpc.startswith(("http://", "https://")):
        return True
    return bool(cli and (Path(cli).exists() or shutil.which(cli)))


def client_call(call: str, amount_flop: int) -> dict:
    """The single place to wire the released testnet client. Refuses until it ships.

    The client contract (binary name, arguments, response shape) is unknown while the
    network is unpublished, so no call is invented here: the function reports the refusal
    and the ledger records it. Replace the body — not the call sites — when the client
    arrives.
    """
    return {
        "ok": False,
        "reason": "client_unavailable",
        "detail": (
            "no public FLOP testnet client/node release exists; set FLOP_CLI or "
            "FLOP_TESTNET_RPC and implement client_call() against the released interface"
        ),
        "call": call,
        "amount_flop": amount_flop,
    }


def attempt_spend(
    call: str,
    amount_flop: int,
    principal: int,
    spent_flop: int = 0,
    ratio: float = DEFAULT_CAP_RATIO,
    enabled: bool | None = None,
    client_ok: bool | None = None,
    dry_run: bool = False,
) -> dict:
    """Validate → cap → enable → client. Every refusal carries a machine-readable reason."""
    enabled = flag("FLOP_COMPUTE_ENABLED") if enabled is None else enabled
    client_ok = client_available() if client_ok is None else client_ok
    row: dict = {"ts": utc_now(), "call": call, "amount_flop": amount_flop}

    if call not in SPENDABLE_CALLS:
        return {
            **row,
            "kind": "blocked",
            "reason": "unknown_call",
            "detail": f"spendable: {', '.join(SPENDABLE_CALLS)}",
        }
    if amount_flop <= 0:
        return {**row, "kind": "blocked", "reason": "invalid_amount"}
    cap = spend_cap(principal, ratio)
    if amount_flop > max(0, cap - max(0, spent_flop)):
        return {
            **row,
            "kind": "blocked",
            "reason": "cap_exceeded",
            "detail": f"cap {cap} FLOP, spent {spent_flop} FLOP",
        }
    if dry_run:
        return {**row, "kind": "dry_run", "reason": "dry_run", "creditable_leg": call in COMPUTE_CREDIT_CALLS}
    if not enabled:
        return {**row, "kind": "blocked", "reason": "disabled", "detail": "FLOP_COMPUTE_ENABLED is not true"}
    if not client_ok:
        return {
            **row,
            "kind": "blocked",
            "reason": "client_unavailable",
            "detail": client_call(call, amount_flop)["detail"],
        }

    outcome = client_call(call, amount_flop)
    if not outcome.get("ok"):
        return {
            **row,
            "kind": "blocked",
            "reason": outcome.get("reason", "client_error"),
            "detail": outcome.get("detail", ""),
        }
    return {**row, "kind": "spend", "status": "submitted", "tx": outcome.get("tx", "")}


def record(row: dict, directory: Path | None = None) -> Path:
    """Append one ledger line. Never rewrites history, never stores a secret."""
    directory = directory or state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / LEDGER_NAME
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
    return path


def read_ledger(directory: Path | None = None) -> list[dict]:
    path = (directory or state_dir()) / LEDGER_NAME
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def summarize(rows: list[dict]) -> dict:
    """Ledger roll-up: what was spent, what was refused, what the network credited."""
    spent_by_call: dict[str, int] = {}
    blocked_by_reason: dict[str, int] = {}
    spent_total = 0
    dry_runs = 0
    credited_k_locked = 0
    unlocked = 0
    pending_settlements = 0

    for row in rows:
        kind = row.get("kind")
        if kind == "spend":
            amount = int(row.get("amount_flop") or 0)
            spent_total += amount
            spent_by_call[row.get("call", "?")] = spent_by_call.get(row.get("call", "?"), 0) + amount
        elif kind == "blocked":
            reason = str(row.get("reason", "unknown"))
            blocked_by_reason[reason] = blocked_by_reason.get(reason, 0) + 1
        elif kind == "dry_run":
            dry_runs += 1
        elif kind == "settlement":
            if row.get("settlement_final") and row.get("dispute_window_closed") and not row.get("upheld_disputes"):
                k_locked = int(row.get("k_locked") or 0)
                credited_k_locked += k_locked
                unlocked += credit_for_payable(k_locked)
            else:
                pending_settlements += 1

    return {
        "rows": len(rows),
        "spent_flop": spent_total,
        "spent_by_call": spent_by_call,
        "blocked_total": sum(blocked_by_reason.values()),
        "blocked_by_reason": blocked_by_reason,
        "dry_runs": dry_runs,
        "credited_k_locked_flop": credited_k_locked,
        "unlocked_flop": unlocked,
        "pending_settlements": pending_settlements,
    }


def status(directory: Path | None = None) -> dict:
    directory = directory or state_dir()
    principal = principal_from_env()
    rows = read_ledger(directory)
    summary = summarize(rows)
    return {
        "enabled": flag("FLOP_COMPUTE_ENABLED"),
        "client_available": client_available(),
        "state_dir": str(directory),
        "ledger": str(directory / LEDGER_NAME),
        "spendable_calls": list(SPENDABLE_CALLS),
        "credit_calls": list(COMPUTE_CREDIT_CALLS),
        "plan": plan(principal, summary["spent_flop"], cap_ratio_from_env()),
        "ledger_summary": summary,
        "next_action": (
            "write the released testnet client into client_call(), then open a channel and "
            "fund escrow for compute; credit follows each settled payable after its "
            "seven-day dispute window"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FLOP testnet compute-spend path (fail-closed)")
    parser.add_argument("--status", action="store_true", help="config, budget plan and ledger roll-up")
    parser.add_argument("--plan", type=int, metavar="PRINCIPAL", help="budget plan for a given locked principal")
    parser.add_argument("--spend", action="store_true", help="attempt a live spend (refused until the client ships)")
    parser.add_argument("--dry-run", action="store_true", help="record the intent without any call")
    parser.add_argument("--call", choices=SPENDABLE_CALLS, default="open_channel")
    parser.add_argument("--amount", type=int, default=0, metavar="FLOP")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--ledger", action="store_true", help="dump the ledger roll-up only")
    args = parser.parse_args(argv)

    if args.plan is not None:
        payload: dict = {"plan": plan(args.plan, 0, cap_ratio_from_env())}
    elif args.spend or args.dry_run:
        rows = read_ledger()
        row = attempt_spend(
            call=args.call,
            amount_flop=args.amount,
            principal=principal_from_env(),
            spent_flop=summarize(rows)["spent_flop"],
            ratio=cap_ratio_from_env(),
            dry_run=args.dry_run,
        )
        record(row)
        payload = {
            "result": row,
            "plan": plan(principal_from_env(), summarize(read_ledger())["spent_flop"], cap_ratio_from_env()),
        }
    elif args.ledger:
        payload = {"ledger_summary": summarize(read_ledger())}
    else:
        payload = status()

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for key, value in payload.items():
            print(f"{key}: {json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value}")

    if "result" in payload and payload["result"].get("kind") == "blocked":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
