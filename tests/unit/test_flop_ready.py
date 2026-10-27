"""Unit tests for apps/tools/flop_ready.py — no network, no secrets."""

from __future__ import annotations

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2] / "apps" / "tools"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS.parent / "earn"))

import flop_ready as ready

CHAIN_OK = {
    "ss58": "5DxHXcRUngwAidPMMcCicBevPzJcdWF7qQ1S2MFAGJJWndSa",
    "balance": "25",
    "checks": {"genesis_matches": True, "account_exists": True},
}
CHAIN_UNFUNDED = {
    "ss58": "5DxHXcRUngwAidPMMcCicBevPzJcdWF7qQ1S2MFAGJJWndSa",
    "balance": None,
    "checks": {"genesis_matches": True, "account_exists": False},
}
CHAIN_WRONG_GENESIS = {
    "ss58": "5DxHXcRUngwAidPMMcCicBevPzJcdWF7qQ1S2MFAGJJWndSa",
    "balance": "25",
    "checks": {"genesis_matches": False, "account_exists": True},
}


class FakeGateway:
    PRICE_PER_1K_TOKENS = 0.006

    def __init__(self, key=True, status=None, probe=None, receipts=None):
        self._key = key
        self._status = status or {"ok": True, "credential": "owner", "remaining_quota": 20000}
        self._probe = probe or {"ok": True, "text": "ready", "cost_flop": 0.0001}
        self._receipts = receipts or {"ok": True, "count": 1}

    def available(self):
        return self._key

    def status(self):
        return self._status

    def complete(self, *a, **k):
        return self._probe

    def models(self):
        return {"ok": True, "count": 1, "ids": ["qwen/qwen2.5-0.5b-instruct"]}

    def receipts(self):
        return self._receipts


def patch_chain(monkeypatch, payload):
    monkeypatch.setattr(ready.rail, "check", lambda **kwargs: payload)


def test_wrong_genesis_blocks_everything(monkeypatch):
    patch_chain(monkeypatch, CHAIN_WRONG_GENESIS)
    monkeypatch.setattr(ready, "_gateway", lambda: FakeGateway())
    report = ready.readiness()
    assert report["blocker"] == "genesis_mismatch"
    assert report["ready"] is False


def test_unfunded_account_points_at_the_faucet(monkeypatch):
    patch_chain(monkeypatch, CHAIN_UNFUNDED)
    monkeypatch.setattr(ready, "_gateway", lambda: FakeGateway())
    report = ready.readiness()
    assert report["blocker"] == "unfunded"
    assert "faucet" in report["action"]


def test_missing_gateway_key(monkeypatch):
    patch_chain(monkeypatch, CHAIN_OK)
    monkeypatch.setattr(ready, "_gateway", lambda: FakeGateway(key=False))
    report = ready.readiness()
    assert report["blocker"] == "no_gateway_key"
    assert "flop_enroll" in report["action"]


def test_rejected_gateway_status(monkeypatch):
    patch_chain(monkeypatch, CHAIN_OK)
    monkeypatch.setattr(
        ready,
        "_gateway",
        lambda: FakeGateway(status={"ok": False, "reason": "key_rejected"}),
    )
    report = ready.readiness()
    assert report["blocker"] == "key_rejected"


def test_unfunded_channel_is_the_blocker(monkeypatch):
    patch_chain(monkeypatch, CHAIN_OK)
    monkeypatch.setattr(
        ready,
        "_gateway",
        lambda: FakeGateway(probe={"ok": False, "reason": "channel_unfunded"}),
    )
    report = ready.readiness()
    assert report["blocker"] == "channel_unfunded"
    assert report["ready"] is False
    assert ready.main(["--no-probe"]) in (0, 5, 6)


def test_full_readiness(monkeypatch):
    patch_chain(monkeypatch, CHAIN_OK)
    monkeypatch.setattr(ready, "_gateway", lambda: FakeGateway())
    report = ready.readiness()
    assert report["ready"] is True
    assert report["receipts"]["count"] == 1
    assert ready.main([]) == 0


def test_exit_codes_follow_the_blocker(monkeypatch, capsys):
    patch_chain(monkeypatch, CHAIN_UNFUNDED)
    monkeypatch.setattr(ready, "_gateway", lambda: FakeGateway())
    assert ready.main([]) == 3
    patch_chain(monkeypatch, CHAIN_OK)
    monkeypatch.setattr(ready, "_gateway", lambda: FakeGateway(key=False))
    assert ready.main([]) == 4
    monkeypatch.setattr(ready, "_gateway", lambda: FakeGateway(probe={"ok": False, "reason": "channel_unfunded"}))
    assert ready.main([]) == 5
    capsys.readouterr()


def test_json_output_is_machine_readable(monkeypatch, capsys):
    import json

    patch_chain(monkeypatch, CHAIN_OK)
    monkeypatch.setattr(ready, "_gateway", lambda: FakeGateway())
    assert ready.main(["--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ready"] is True
    assert out["chain"]["ss58"].startswith("5DxHXcR")


def test_chain_unreachable_is_reported_not_raised(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("RPC unreachable: HTTP Error 503")

    monkeypatch.setattr(ready.rail, "check", boom)
    monkeypatch.setattr(ready, "_gateway", lambda: FakeGateway())
    report = ready.readiness()
    assert report["blocker"] == "chain_unreachable"
    assert ready.main([]) == 8


def test_empty_catalogue_is_no_supply(monkeypatch):
    patch_chain(monkeypatch, CHAIN_OK)

    class EmptyCatalogue(FakeGateway):
        def models(self):
            return {"ok": True, "count": 0, "ids": []}

    monkeypatch.setattr(ready, "_gateway", lambda: EmptyCatalogue())
    report = ready.readiness()
    assert report["blocker"] == "no_supply"
    assert report["ready"] is False
    assert ready.main([]) == 7


def test_model_not_advertised_mid_probe(monkeypatch):
    patch_chain(monkeypatch, CHAIN_OK)

    class NoSupply(FakeGateway):
        def models(self):
            return {"ok": True, "count": 1, "ids": ["qwen/qwen2.5-0.5b-instruct"]}

        def complete(self, *a, **k):
            return {"ok": False, "reason": "no_supply"}

    monkeypatch.setattr(ready, "_gateway", lambda: NoSupply())
    report = ready.readiness()
    assert report["blocker"] == "no_supply"
    assert "supply" in report["action"]
