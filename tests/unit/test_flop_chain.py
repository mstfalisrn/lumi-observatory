"""Unit tests for apps/earn/flop_chain.py — no SDK, no node."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

MODULE_DIR = Path(__file__).resolve().parents[2] / "apps" / "earn"
sys.path.insert(0, str(MODULE_DIR))

import flop_chain as fc


class FakeReceipt:
    tx_hash = "0xdeadbeef"
    hash_lock = "0xlock"
    gn_used = 370
    block_number = 2789000


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def buy_verified_inference(self, params):
        self.calls.append(("buy", params))
        return FakeReceipt()


def make(tmp_path, **kw) -> fc.FlopChainConnector:
    seed = tmp_path / "seed"
    seed.write_bytes(b"\x01" * 32)
    return fc.FlopChainConnector(seed_path=str(seed), **kw)


def test_disabled_by_default(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("FLOP_CHAIN_RPC", raising=False)
    monkeypatch.delenv("FLOP_CHAIN_SEED", raising=False)
    conn = make(tmp_path)
    assert conn.reason() == "sdk_disabled"
    assert conn.available() is False


def test_rpc_unset(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("FLOP_CHAIN_SEED", raising=False)
    conn = make(tmp_path, enabled=True)
    assert conn.reason() == "rpc_unset"


def test_seed_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("FLOP_CHAIN_SEED", raising=False)
    conn = fc.FlopChainConnector(rpc_url="http://127.0.0.1:16111/rpc", seed_path=str(tmp_path / "nope"), enabled=True)
    assert conn.reason() == "seed_missing"


def test_seed_hex_from_file_and_env(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("FLOP_CHAIN_SEED", raising=False)
    conn = make(tmp_path)
    assert conn.seed_hex() == "0x" + "01" * 32
    monkeypatch.setenv("FLOP_CHAIN_SEED", "ab" * 32)
    assert conn.seed_hex() == "0x" + "ab" * 32


@pytest.mark.asyncio
async def test_buy_inference_returns_the_receipt(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("FLOP_CHAIN_SEED", raising=False)
    fake = FakeClient()
    conn = make(
        tmp_path,
        rpc_url="http://127.0.0.1:16111/rpc",
        enabled=True,
        client_factory=lambda *a: fake,
    )
    monkeypatch.setattr(conn, "available", lambda: True)
    monkeypatch.setattr(conn, "reason", lambda: None)
    res = await conn.buy_inference("qwen/qwen2.5-0.5b-instruct", "hi", 1)
    assert res["ok"] is True
    assert res["tx"] == "0xdeadbeef"
    assert res["gn_used"] == 370
    assert fake.calls[0][1]["max_flop"] == 1.0


@pytest.mark.asyncio
async def test_buy_inference_reports_chain_errors(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("FLOP_CHAIN_SEED", raising=False)

    class Boom(FakeClient):
        def buy_verified_inference(self, params):
            raise RuntimeError("InsufficientBalance")

    conn = make(tmp_path, rpc_url="http://x", enabled=True, client_factory=lambda *a: Boom())
    monkeypatch.setattr(conn, "reason", lambda: None)
    res = await conn.buy_inference("m", "p", 1)
    assert res["ok"] is False
    assert res["reason"] == "chain_error"
    assert "InsufficientBalance" in res["detail"]


@pytest.mark.asyncio
async def test_unexposed_legs_are_named(tmp_path) -> None:
    conn = make(tmp_path)
    for leg in ("open_channel", "top_up_escrow", "force_open"):
        res = await conn.call(leg)
        assert res["reason"] == "sdk_call_missing"
        assert leg in res["detail"]


@pytest.mark.asyncio
async def test_unknown_leg(tmp_path) -> None:
    assert (await make(tmp_path).call("steal_funds"))["reason"] == "unknown_call"
