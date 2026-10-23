"""FLOP rail readiness check — encodings, SCALE decode and the fail-closed verdict.

These tests pin the pieces that decide whether a DID may settle on FlopRail at all:
did:key must decode to an Ed25519 AccountId32 (V1 rejects maps and foreign wallets),
the SS58 rendering must match the reference vector, the AccountInfo blob must be read
with the right widths, and a genesis mismatch must refuse the chain.
"""

import json
import sys
from pathlib import Path

import pytest

MODULE_DIR = Path(__file__).resolve().parents[2] / "apps" / "tools"
sys.path.insert(0, str(MODULE_DIR))

import flop_rail_check as frc

# Reference vectors: Alice's well-known sr25519 key bytes and its SS58 (prefix 42).
ALICE_KEY = bytes.fromhex("d43593c715fdd31c61141abd04a99fd6822c8558854ccde39a5684e7a56da27d")
ALICE_SS58 = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"


def ed25519_did(key: bytes) -> str:
    return "did:key:z" + frc.b58encode(b"\xed\x01" + key)


def account_info_blob(nonce=0, free=0, reserved=0, frozen=0, consumers=0, providers=1) -> bytes:
    return (
        nonce.to_bytes(4, "little")
        + consumers.to_bytes(4, "little")
        + providers.to_bytes(4, "little")
        + (0).to_bytes(4, "little")
        + free.to_bytes(16, "little")
        + reserved.to_bytes(16, "little")
        + frozen.to_bytes(16, "little")
        + (0).to_bytes(16, "little")
    )


def test_base58_roundtrip_including_leading_zeroes():
    for payload in (b"\x00", b"\x00\x00\x01", b"hello world", ALICE_KEY):
        assert frc.b58decode(frc.b58encode(payload)) == payload


def test_ss58_matches_the_reference_vector():
    assert frc.ss58_address(ALICE_KEY) == ALICE_SS58


def test_did_key_decodes_to_a_32_byte_ed25519_account():
    key = bytes(range(32))
    assert frc.did_to_account(ed25519_did(key)) == key


def test_did_key_rejects_foreign_multicodecs_and_short_keys():
    not_ed25519 = "did:key:z" + frc.b58encode(b"\x01\x01" + bytes(32))
    with pytest.raises(frc.CheckError):
        frc.did_to_account(not_ed25519)
    with pytest.raises(frc.CheckError):
        frc.did_to_account("did:key:z" + frc.b58encode(b"\xed\x01" + bytes(16)))
    with pytest.raises(frc.CheckError):
        frc.did_to_account("did:web:example.com")


def test_account_storage_key_is_system_account_with_blake2_concat():
    key = frc.account_storage_key(ALICE_KEY)
    assert key.startswith("0x" + frc.TWOX_SYSTEM + frc.TWOX_ACCOUNT)
    # 32 (two map/two prefix hashes) + 16 blake2 + 32 account bytes = 80 bytes
    assert len(bytes.fromhex(key[2:])) == 80
    assert key.endswith(ALICE_KEY.hex())


def test_account_info_decodes_four_u32_and_four_u128():
    blob = account_info_blob(nonce=7, free=1_500_000_000_000_000_000, reserved=5, frozen=3)
    info = frc.decode_account_info(blob)
    assert info["nonce"] == 7
    assert info["free"] == 1_500_000_000_000_000_000
    assert info["reserved"] == 5
    assert info["frozen"] == 3
    assert info["providers"] == 1


def test_account_info_rejects_a_short_blob():
    with pytest.raises(frc.CheckError):
        frc.decode_account_info(b"\x00" * 10)


def test_units_are_formatted_at_eighteen_decimals():
    assert frc.format_units(0) == "0"
    assert frc.format_units(10**18) == "1"
    assert frc.format_units(1_500_000_000_000_000_000) == "1.5"
    assert frc.format_units(1) == "0.000000000000000001"


def test_genesis_mismatch_refuses_the_chain_before_anything_else():
    code, problems = frc.verdict({"checks": {"genesis_matches": False}, "genesis": "0xaa", "expected_genesis": "0xbb"})
    assert code == 2
    assert any("refusing this chain" in problem for problem in problems)


def test_missing_account_is_a_faucet_problem_not_a_chain_problem():
    code, problems = frc.verdict({"checks": {"genesis_matches": True, "account_exists": False}, "did": "did:key:zabc"})
    assert code == 3
    assert any("faucet" in problem for problem in problems)


def test_unfunded_account_cannot_pay_fees():
    code, problems = frc.verdict(
        {"checks": {"genesis_matches": True, "account_exists": True, "can_pay_fees": False}, "did": "did:key:zabc"}
    )
    assert code == 3
    assert any("fees" in problem for problem in problems)


def test_ready_account_passes():
    code, problems = frc.verdict(
        {"checks": {"genesis_matches": True, "account_exists": True, "can_pay_fees": True}, "did": "did:key:zabc"}
    )
    assert code == 0
    assert problems == []


def test_check_wires_rpc_calls_and_decodes_the_account(monkeypatch):
    key = ALICE_KEY
    calls: list[str] = []

    def fake_rpc(url, method, params=None, timeout=20.0):
        calls.append(method)
        if method == "chain_getBlockHash":
            return frc.DEVNET_GENESIS
        if method == "system_chain":
            return "FLOP Devnet"
        if method == "system_version":
            return "1.1.0-test"
        if method == "system_health":
            return {"peers": 3, "isSyncing": False}
        if method == "system_properties":
            return {"tokenSymbol": "FLOP", "tokenDecimals": 18}
        if method == "chain_getHeader":
            return {"number": "0x10"}
        if method == "state_getStorage":
            if params and str(params[0]).startswith("0x" + frc.TWOX_SYSTEM):
                return "0x" + account_info_blob(nonce=1, free=2 * 10**18).hex()
            return "0x" + (1_700_000_000_000).to_bytes(8, "little").hex()
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(frc, "rpc", fake_rpc)
    result = frc.check(frc.DEVNET_RPC, ed25519_did(key))
    assert result["checks"]["genesis_matches"] is True
    assert result["checks"]["chain"] == "FLOP Devnet"
    assert result["checks"]["account_exists"] is True
    assert result["balance"] == "2"
    assert result["block"] == 16
    assert result["block_time_ms"] == 1_700_000_000_000
    assert result["ss58"] == ALICE_SS58
    assert "state_getStorage" in calls


def test_cli_json_reports_readiness(monkeypatch, capsys):
    monkeypatch.setattr(
        frc,
        "check",
        lambda *a, **k: {
            "rpc": "x",
            "did": "did:key:zabc",
            "account_hex": "0x00",
            "ss58": "5abc",
            "genesis": frc.DEVNET_GENESIS,
            "expected_genesis": frc.DEVNET_GENESIS,
            "block": 1,
            "block_time_ms": 1,
            "balance": "1",
            "checks": {"genesis_matches": True, "account_exists": True, "can_pay_fees": True},
        },
    )
    code = frc.main(["--did", "did:key:zabc", "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True


def test_cli_reports_unreachable_rpc_with_exit_two(monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise frc.CheckError("connection refused")

    monkeypatch.setattr(frc, "check", boom)
    assert frc.main(["--did", "did:key:zabc"]) == 2
    assert "unreachable" in capsys.readouterr().out
