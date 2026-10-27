"""Unit tests for apps/tools/flop_enroll.py — no network, no real secrets."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

MOD_PATH = Path(__file__).resolve().parents[2] / "apps" / "tools" / "flop_enroll.py"

pytest.importorskip("cryptography")


def _load():
    spec = importlib.util.spec_from_file_location("flop_enroll", MOD_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["flop_enroll"] = mod
    spec.loader.exec_module(mod)
    return mod


enroll = _load()

# Reference vector: Alice's well-known public key bytes and its SS58 (prefix 42),
# the same vector tests/unit/test_flop_rail_check.py uses.
ALICE_KEY = bytes.fromhex("d43593c715fdd31c61141abd04a99fd6822c8558854ccde39a5684e7a56da27d")
ALICE_SS58 = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"


def ss58(pub: bytes, prefix: int = 42) -> str:
    pre = b"SS58PRE" + bytes([prefix]) + pub
    return enroll.b58(bytes([prefix]) + pub + hashlib.blake2b(pre, digest_size=64).digest()[:2])


def test_b58_empty() -> None:
    assert enroll.b58(b"") == ""


def test_b58_leading_zero_bytes() -> None:
    assert enroll.b58(b"\x00\x00\x01") == "112"


def test_b58_known_vector() -> None:
    assert enroll.b58(b"hello world") == "StV1DL6CwTryKyV"


def test_ss58_matches_alice_vector() -> None:
    assert ss58(ALICE_KEY) == ALICE_SS58


def test_did_key_prefix_is_ed25519() -> None:
    did = "z" + enroll.b58(b"\xed\x01" + ALICE_KEY)
    assert did.startswith("z6Mk")
    assert len(did) == 48


def test_constants_point_at_our_identity() -> None:
    assert enroll.EXPECTED_DID.startswith("z6Mk")
    assert enroll.EXPECTED_SS58.startswith("5DxHXcR")
    assert enroll.KEY_OUT.endswith("/flop-gateway.key")


def test_real_seed_derivation_when_present() -> None:
    """Runs only where the DID secret exists; CI has no secrets and skips."""
    seed_path = Path(enroll.SEED)
    if not seed_path.exists():
        pytest.skip("DID seed not available in this environment")

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    seed = seed_path.read_bytes()
    assert len(seed) == 32
    pub = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    assert "z" + enroll.b58(b"\xed\x01" + pub) == enroll.EXPECTED_DID
    assert ss58(pub) == enroll.EXPECTED_SS58
