#!/usr/bin/env python3
"""One-command readiness report for the FLOP earning path.

Answers a single question — *if the testnet opened tomorrow, could we actually spend and
accrue credit?* — by checking every link in the chain, in order, and reporting the first
blocker with the exact command that clears it.

Links checked:
  1. identity     the DID decodes to an Ed25519 AccountId32 on the pinned chain
  2. funding      System.Account exists and the free balance clears the minimum
  3. gateway      api.flop.network key present and accepted (/v1/keys/me)
  4. channel      a 4-token completion settles (this is the real spend path)
  5. receipts     settled receipts are readable (the evidence a payable is real)

Exit codes: 0 ready · 2 identity/genesis · 3 unfunded · 4 no gateway key · 5 channel
blocked · 6 gateway error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(_TOOLS))
sys.path.insert(0, str(_TOOLS.parent / "earn"))

import flop_rail_check as rail

DEFAULT_DID = "did:key:z6Mkk5VyfvyY6GzwBeBm9vaW8mDsDv2oDoL1GMVbiWaxn3u2"


def _gateway():
    try:
        import flop_gateway
    except ImportError:  # pragma: no cover - module layout differs when vendored
        return None
    return flop_gateway


def readiness(did: str = DEFAULT_DID, rpc_url: str = rail.DEVNET_RPC, probe: bool = True) -> dict:
    """Walk the five links and stop reporting progress at the first blocker."""
    report: dict = {"did": did, "links": {}, "blocker": None, "action": None, "ready": False}

    try:
        chain = rail.check(rpc_url=rpc_url, did=did)
    except Exception as exc:  # rail.CheckError and transport failures alike
        report["blocker"] = "chain_unreachable"
        report["action"] = f"the pinned RPC did not answer ({str(exc)[:120]}); retry later"
        return report
    checks = chain.get("checks", {})
    account = chain.get("ss58") or ""
    balance = chain.get("balance") or "0"
    report["chain"] = {
        "ss58": account,
        "genesis_ok": bool(checks.get("genesis_matches", False)),
        "exists": bool(checks.get("account_exists", False)),
        "balance": balance,
    }
    if not checks.get("genesis_matches", False):
        report["blocker"] = "genesis_mismatch"
        report["action"] = "the RPC is not the pinned chain — do not spend against it"
        return report
    if not checks.get("account_exists", False):
        report["blocker"] = "unfunded"
        report["action"] = 'fund the account: POST https://faucet.flop.network/fund {"address": <ss58>}'
        return report
    report["links"]["funding"] = True

    gateway = _gateway()
    if gateway is None or not gateway.available():
        report["blocker"] = "no_gateway_key"
        report["action"] = "python apps/tools/flop_enroll.py"
        return report

    st = gateway.status()
    report["gateway"] = {k: st.get(k) for k in ("ok", "credential", "remaining_quota", "model_allowlist", "reason")}
    if not st.get("ok"):
        report["blocker"] = st.get("reason", "gateway_error")
        report["action"] = "re-enrol the devnet key: python apps/tools/flop_enroll.py"
        return report
    report["links"]["gateway"] = True

    if probe:
        cat = gateway.models()
        report["models"] = {"ok": cat.get("ok"), "count": cat.get("count", 0), "ids": cat.get("ids", [])}
        if cat.get("ok") and not cat.get("count"):
            report["blocker"] = "no_supply"
            report["action"] = (
                "the catalogue is empty: no provider is serving inference right now. Not a "
                "funding problem — re-run later, the readiness watch will pick it up"
            )
            return report
        res = gateway.complete("Reply with the single word: ready", max_tokens=4)
        report["probe"] = {k: res.get(k) for k in ("ok", "reason", "text", "cost_flop", "request_id")}
        if not res.get("ok"):
            report["blocker"] = res.get("reason", "probe_failed")
            if res.get("reason") == "no_supply":
                report["action"] = (
                    "the model is listed but no provider is serving it right now — retry later; "
                    "this is supply, not our account"
                )
            else:
                report["action"] = (
                    "channel_unfunded: the balance is below the channel minimum — drip again "
                    "(adres başına 24 saat) or use the verified-human tier; then re-run this tool"
                )
            return report
        report["links"]["channel"] = True

        rec = gateway.receipts()
        report["receipts"] = {"ok": rec.get("ok"), "count": rec.get("count", 0)}
        report["links"]["receipts"] = bool(rec.get("ok"))

    report["ready"] = all(report["links"].get(k) for k in ("funding", "gateway", "channel"))
    if report["ready"]:
        report["action"] = "ready — set FLOP_COMPUTE_ENABLED=true and run the spend lane"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FLOP earning-path readiness report")
    parser.add_argument("--did", default=DEFAULT_DID)
    parser.add_argument("--rpc", default=rail.DEVNET_RPC)
    parser.add_argument("--no-probe", action="store_true", help="skip the live completion")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    report = readiness(args.did, args.rpc, probe=not args.no_probe)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        chain = report.get("chain", {})
        print(f"DID          {report['did']}")
        print(f"account      {chain.get('ss58', '-')}")
        print(f"balance      {chain.get('balance', '-')} FLOP")
        print(
            f"gateway      {report.get('gateway', {}).get('ok')} quota={report.get('gateway', {}).get('remaining_quota')}"
        )
        print(f"probe        {report.get('probe', {}).get('ok')} reason={report.get('probe', {}).get('reason', '-')}")
        print(f"ready        {report['ready']}")
        if report["blocker"]:
            print(f"blocker      {report['blocker']}")
            print(f"action       {report['action']}")

    if report["ready"]:
        return 0
    return {
        "genesis_mismatch": 2,
        "unfunded": 3,
        "no_gateway_key": 4,
        "channel_unfunded": 5,
        "no_supply": 7,
        "chain_unreachable": 8,
    }.get(report["blocker"] or "", 6)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
