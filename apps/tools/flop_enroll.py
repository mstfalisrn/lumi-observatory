#!/usr/bin/env python3
"""Enrol a FLOP devnet gateway key for our DID account.

Reads the Ed25519 seed from /root/secrets/lumi-observatory/did.ed25519, proves it
derives to the expected DID and SS58 account (aborts otherwise), signs the
single-use challenge, and stores the returned gateway key with 0600.

The key is never printed: only length and a 6-char fingerprint.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import urllib.error
import urllib.request

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

SEED = "/root/secrets/lumi-observatory/did.ed25519"
KEY_OUT = "/root/secrets/lumi-observatory/flop-gateway.key"
EXPECTED_DID = "z6Mkk5VyfvyY6GzwBeBm9vaW8mDsDv2oDoL1GMVbiWaxn3u2"
EXPECTED_SS58 = "5DxHXcRUngwAidPMMcCicBevPzJcdWF7qQ1S2MFAGJJWndSa"
BASE = "https://api.flop.network"
UA = "lumi-enroll/1.0"

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58(data: bytes) -> str:
    num = int.from_bytes(data, "big")
    out = ""
    while num:
        num, rem = divmod(num, 58)
        out = B58[rem] + out
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + out


def http(method: str, path: str, payload: dict | None = None, key: str | None = None) -> dict:
    req = urllib.request.Request(  # noqa: S310 — BASE is a fixed https:// literal
        BASE + path,
        method=method,
        headers={"User-Agent": UA, "Accept": "application/json"},
    )
    if key:
        req.add_header("Authorization", "Bearer " + key)
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, body, timeout=30) as resp:  # noqa: S310 — fixed https host, no user URL
        return json.loads(resp.read())


def main() -> int:
    seed = open(SEED, "rb").read()
    if len(seed) != 32:
        print(f"seed is {len(seed)} bytes, expected 32")
        return 2

    sk = Ed25519PrivateKey.from_private_bytes(seed)
    pub = sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    did = "z" + b58(b"\xed\x01" + pub)
    pre = b"SS58PRE" + bytes([42]) + pub
    ss58 = b58(bytes([42]) + pub + hashlib.blake2b(pre, digest_size=64).digest()[:2])

    print(f"derived did: {did}")
    print(f"derived ss58: {ss58}")
    if did != EXPECTED_DID or ss58 != EXPECTED_SS58:
        print("DERIVATION_MISMATCH — aborting, this is not our account")
        return 2

    ch = http("GET", f"/v1/enroll/wallet/challenge?address={ss58}")
    message = ch["message"]
    print(f"challenge ok: nonce={ch['nonce'][:12]}… expires_at={ch['expires_at']}")

    sig = "0x" + sk.sign(message.encode()).hex()
    try:
        res = http(
            "POST",
            "/v1/enroll/wallet",
            {"address": ss58, "message": message, "signature": sig},
        )
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        detail = exc.read().decode(errors="replace")
        print(f"ENROLL_FAILED http={exc.code} body={detail[:400]}")
        return 3

    key = res.get("api_key") or res.get("key") or res.get("token") or ""
    if not key:
        print("ENROLL_NO_KEY: " + json.dumps({k: v for k, v in res.items() if "key" not in k})[:300])
        return 4

    fd = os.open(KEY_OUT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key + "\n")
    os.chmod(KEY_OUT, stat.S_IRUSR | stat.S_IWUSR)
    fp = hashlib.sha256(key.encode()).hexdigest()[:6]
    print(f"ENROLL_OK key_len={len(key)} fingerprint={fp} saved={KEY_OUT} 0600")
    print("  extra fields: " + ",".join(k for k in res if k not in ("api_key", "key", "token")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
