#!/usr/bin/env python3
"""FLOP rail readiness check — is this DID a usable on-chain account, on the right chain?

Why this exists: ``@flop-labs/tclk-flop`` (FlopRail) settles FLOP through native HTLC
escrows and accepts exactly one account identity — the Ed25519 public key inside a
``did:key``, used as the ``AccountId32``. V1 rejects unrelated sr25519 wallets and any
DID-to-address map, and both parties must already exist on chain with fees available.
So before any channel or escrow work, three things have to be true, and all three are
cheap to check over plain JSON-RPC:

  1. the RPC answers and block 0 matches the expected genesis pin,
  2. the DID decodes to a 32-byte Ed25519 public key (nothing else is accepted),
  3. that account exists in ``System.Account`` and can pay fees.

The check reads only public data: no key material is loaded, printed or stored. A DID
alone carries its public key, so ``--did`` is enough; pass ``--did-file`` to read a
``did:key`` line from disk instead.

Defaults match the published devnet (FlopRail VERIFICATION.md, 2026-10-22): genesis
``0x99c172c0…``, 18 decimals, symbol FLOP.

Exit codes: 0 ready · 2 unreachable or wrong chain · 3 account missing/unfunded.

Usage:
  python apps/tools/flop_rail_check.py --did did:key:z6Mk...
  python apps/tools/flop_rail_check.py --did-file secrets/did.pub --json
  python apps/tools/flop_rail_check.py --did did:key:... --rpc http://127.0.0.1:9944
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from typing import cast

DEVNET_RPC = "https://rpc.devnet.flop.network"
DEVNET_GENESIS = "0x99c172c095340ace980631f9123544c6bebd4839f3437e463533422d8be2d631"
SS58_PREFIX = 42
ED25519_MULTICODEC = b"\xed\x01"
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
# System.Account: twox128("System") + twox128("Account") + blake2_128_concat(account)
TWOX_SYSTEM = "26aa394eea5630e07c48ae0c9558cef7"
TWOX_ACCOUNT = "b99d880ec681799c0cf30e8886371da9"
ACCOUNT_INFO_BYTES = 4 * 4 + 4 * 16  # nonce/consumers/providers/sufficients + 4 balances


class CheckError(RuntimeError):
    """A check failed for a reason the operator has to act on."""


# --- encodings -------------------------------------------------------------------


def b58decode(text: str) -> bytes:
    number = 0
    for char in text:
        index = BASE58_ALPHABET.find(char)
        if index < 0:
            raise ValueError(f"invalid base58 character: {char!r}")
        number = number * 58 + index
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    pad = len(text) - len(text.lstrip("1"))
    return b"\x00" * pad + raw


def b58encode(data: bytes) -> str:
    number = int.from_bytes(data, "big")
    out = ""
    while number:
        number, rem = divmod(number, 58)
        out = BASE58_ALPHABET[rem] + out
    pad = len(data) - len(data.lstrip(b"\x00"))
    return "1" * pad + out


def did_to_account(did: str) -> bytes:
    """did:key (Ed25519) → 32-byte AccountId32. Anything else is rejected, as V1 does."""
    if not did.startswith("did:key:z"):
        raise CheckError("only did:key with a base58btc multibase ('z…') is accepted")
    decoded = b58decode(did[len("did:key:z") :])
    if not decoded.startswith(ED25519_MULTICODEC):
        raise CheckError("did:key is not an Ed25519 public key (multicodec ed01 missing)")
    key = decoded[len(ED25519_MULTICODEC) :]
    if len(key) != 32:
        raise CheckError(f"decoded key is {len(key)} bytes, expected 32")
    return key


def ss58_address(account: bytes, prefix: int = SS58_PREFIX) -> str:
    if len(account) != 32:
        raise CheckError("AccountId32 must be 32 bytes")
    payload = bytes([prefix]) + account
    checksum = hashlib.blake2b(b"SS58PRE" + payload, digest_size=64).digest()[:2]
    return b58encode(payload + checksum)


def account_storage_key(account: bytes) -> str:
    """0x + twox128("System") + twox128("Account") + blake2_128_concat(account)."""
    concat = hashlib.blake2b(account, digest_size=16).digest() + account
    return "0x" + TWOX_SYSTEM + TWOX_ACCOUNT + concat.hex()


def decode_account_info(blob: bytes) -> dict:
    """SCALE AccountInfo: four u32 counters, then free/reserved/frozen/flags u128."""
    if len(blob) < ACCOUNT_INFO_BYTES:
        raise CheckError(f"AccountInfo is {len(blob)} bytes, expected >= {ACCOUNT_INFO_BYTES}")
    view = blob[:ACCOUNT_INFO_BYTES]
    nonce, consumers, providers, sufficients = (int.from_bytes(view[i : i + 4], "little") for i in (0, 4, 8, 12))
    free, reserved, frozen, flags = (int.from_bytes(view[16 + i : 16 + i + 16], "little") for i in (0, 16, 32, 48))
    return {
        "nonce": nonce,
        "consumers": consumers,
        "providers": providers,
        "sufficients": sufficients,
        "free": free,
        "reserved": reserved,
        "frozen": frozen,
        "flags": flags,
    }


def format_units(value: int, decimals: int = 18) -> str:
    sign = "-" if value < 0 else ""
    whole, fraction = divmod(abs(value), 10**decimals)
    if not fraction:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{str(fraction).rjust(decimals, '0').rstrip('0')}"


# --- rpc -------------------------------------------------------------------------


def rpc(url: str, method: str, params: list | None = None, timeout: float = 20.0) -> object:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
    request = urllib.request.Request(  # noqa: S310 (pinned http(s) JSON-RPC endpoint only)
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "lumi-rail-check/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (same pinned endpoint)
            payload = json.loads(response.read().decode())
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise CheckError(f"RPC {url} unreachable: {exc}") from exc
    if "error" in payload:
        raise CheckError(f"RPC error for {method}: {payload['error']}")
    return payload.get("result")


def check(rpc_url: str = DEVNET_RPC, did: str = "", expected_genesis: str = DEVNET_GENESIS) -> dict:
    account = did_to_account(did) if did else b""
    result: dict = {
        "rpc": rpc_url,
        "did": did,
        "account_hex": "0x" + account.hex() if account else "",
        "ss58": ss58_address(account) if account else "",
        "expected_genesis": expected_genesis,
        "checks": {},
    }

    genesis = str(rpc(rpc_url, "chain_getBlockHash", [0]) or "")
    result["checks"]["genesis_matches"] = genesis.lower() == expected_genesis.lower()
    result["genesis"] = genesis
    result["checks"]["chain"] = rpc(rpc_url, "system_chain")
    result["checks"]["version"] = rpc(rpc_url, "system_version")
    result["checks"]["health"] = rpc(rpc_url, "system_health")
    properties = cast("dict", rpc(rpc_url, "system_properties") or {})
    result["checks"]["token_symbol"] = properties.get("tokenSymbol")
    result["checks"]["token_decimals"] = int(properties.get("tokenDecimals") or 18)

    header = cast("dict", rpc(rpc_url, "chain_getHeader") or {})
    result["block"] = int(str(header.get("number", "0x0")), 16)
    # Timestamp.Now — this runtime build's storage suffix differs from the commonly
    # quoted twox128("Now") constant, so the key is the one verified against the devnet
    # (state_getKeys under 0xf0c365c3cf59d671eb72da0e7a4113c4 returns it).
    timestamp_key = "0xf0c365c3cf59d671eb72da0e7a4113c49f1f0515f462cdcf84e0f1d6045dfcbb"
    now = rpc(rpc_url, "state_getStorage", [timestamp_key])
    result["block_time_ms"] = int.from_bytes(bytes.fromhex(str(now)[2:])[:8], "little") if now else None

    if account:
        storage = rpc(rpc_url, "state_getStorage", [account_storage_key(account)])
        if not storage:
            result["checks"]["account_exists"] = False
            result["balance"] = None
        else:
            info = decode_account_info(bytes.fromhex(str(storage)[2:]))
            result["checks"]["account_exists"] = True
            result["account_info"] = info
            result["balance"] = format_units(info["free"], result["checks"]["token_decimals"])
            result["checks"]["can_pay_fees"] = info["free"] > 0
    return result


def verdict(result: dict) -> tuple[int, list[str]]:
    problems: list[str] = []
    checks = result["checks"]
    if not checks.get("genesis_matches"):
        problems.append(
            f"genesis {result.get('genesis')} != expected {result.get('expected_genesis')} — refusing this chain"
        )
    if result.get("did"):
        if not checks.get("account_exists"):
            problems.append("account does not exist on chain — fund it from the faucet first")
        elif not checks.get("can_pay_fees"):
            problems.append("account has zero free balance — it cannot pay transaction fees")
    return (0 if not problems else 2 if not checks.get("genesis_matches") else 3), problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FLOP rail readiness check (read-only, public data only)")
    parser.add_argument("--did", default="", help="did:key:z… (Ed25519)")
    parser.add_argument("--did-file", default="", help="file containing a did:key line")
    parser.add_argument("--rpc", default=DEVNET_RPC, help=f"JSON-RPC endpoint (default {DEVNET_RPC})")
    parser.add_argument("--expected-genesis", default=DEVNET_GENESIS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    did = args.did.strip()
    if not did and args.did_file:
        with open(args.did_file, encoding="utf-8") as handle:
            did = handle.read().strip().splitlines()[0].strip()

    try:
        result = check(args.rpc, did, args.expected_genesis)
    except CheckError as exc:
        print(f"unreachable: {exc}")
        return 2

    code, problems = verdict(result)
    result["problems"] = problems
    result["ready"] = code == 0

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        checks = result["checks"]
        print(f"rpc:        {result['rpc']}")
        print(f"genesis:    {result.get('genesis')}  {'OK' if checks['genesis_matches'] else 'MISMATCH'}")
        print(f"chain:      {checks.get('chain')} · {checks.get('version')}")
        print(f"block:      {result.get('block')} · health {checks.get('health')}")
        if result.get("block_time_ms"):
            print(f"chain time: {result['block_time_ms']} ms")
        print(f"token:      {checks.get('token_symbol')} · {checks.get('token_decimals')} decimals")
        if result.get("did"):
            print(f"did:        {result['did']}")
            print(f"ss58:       {result['ss58']}  (AccountId32 0x{result['account_hex'][2:]})")
            print(
                f"balance:    {result.get('balance') if result.get('balance') is not None else '—'} {checks.get('token_symbol') or ''}".rstrip()
            )
        if problems:
            for problem in problems:
                print(f"problem:    {problem}")
        else:
            print("verdict:    ready — the DID is a funded account on the pinned chain")
    return code


if __name__ == "__main__":
    sys.exit(main())
