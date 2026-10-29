"""Unit tests for apps/earn/flop_gateway.py — no network, no real key."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

MODULE_DIR = Path(__file__).resolve().parents[2] / "apps" / "earn"
sys.path.insert(0, str(MODULE_DIR))

import flop_gateway as gw


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("FLOP_GATEWAY_KEY", raising=False)
    monkeypatch.delenv("FLOP_GATEWAY_BASE", raising=False)
    monkeypatch.setenv("FLOP_GATEWAY_KEY_FILE", str(tmp_path / "missing.key"))
    return None


def test_available_needs_a_key(tmp_path, monkeypatch) -> None:
    assert gw.available() is False
    key_file = tmp_path / "gw.key"
    key_file.write_text("test-key\n")
    monkeypatch.setenv("FLOP_GATEWAY_KEY_FILE", str(key_file))
    assert gw.available() is True


def test_inline_key_wins_over_file(tmp_path, monkeypatch) -> None:
    key_file = tmp_path / "gw.key"
    key_file.write_text("file-key\n")
    monkeypatch.setenv("FLOP_GATEWAY_KEY_FILE", str(key_file))
    monkeypatch.setenv("FLOP_GATEWAY_KEY", "env-key")
    assert gw.load_key() == "env-key"


def test_foreign_host_is_refused(monkeypatch, tmp_path) -> None:
    key_file = tmp_path / "gw.key"
    key_file.write_text("k\n")
    monkeypatch.setenv("FLOP_GATEWAY_KEY_FILE", str(key_file))
    monkeypatch.setenv("FLOP_GATEWAY_BASE", "https://evil.example")
    assert gw.available() is False
    code, body = gw._request("GET", "/v1/keys/me")
    assert code == 0
    assert body["error"] == "host_not_allowed"


def test_cost_uses_the_published_price() -> None:
    assert gw.cost_flop(0, 0) == 0.0
    assert gw.cost_flop(1000, 0) == 0.006
    assert gw.cost_flop(500, 500) == 0.006
    assert gw.cost_flop(500, 250) == 0.0045


def test_classify_maps_the_channel_failure() -> None:
    body = {
        "error": {
            "code": "provider_error",
            "message": 'Error: pooled openChannel failed: {"module":{"index":103}}',
        }
    }
    assert gw.classify(503, body) == "channel_unfunded"


def test_classify_covers_auth_rate_and_success() -> None:
    assert gw.classify(200, {}) is None
    assert gw.classify(401, {"error": {"message": "bad key"}}) == "key_rejected"
    assert gw.classify(429, {}) == "rate_limited"
    assert gw.classify(400, {"error": {"message": "unknown model"}}) == "request_rejected"
    assert gw.classify(0, {}) == "transport_error"


def test_status_without_a_key_fails_closed() -> None:
    res = gw.status()
    assert res["ok"] is False
    assert res["reason"] == "client_unavailable"


def test_status_reports_quota(monkeypatch, tmp_path) -> None:
    key_file = tmp_path / "gw.key"
    key_file.write_text("k\n")
    monkeypatch.setenv("FLOP_GATEWAY_KEY_FILE", str(key_file))
    monkeypatch.setattr(
        gw,
        "_request",
        lambda *a, **k: (
            200,
            {
                "credential": "owner",
                "remaining_quota": 20000,
                "expires_at": 1,
                "model_allowlist": ["Qwen/Qwen2.5-0.5B-Instruct"],
            },
        ),
    )
    res = gw.status()
    assert res["ok"] is True
    assert res["credential"] == "owner"
    assert res["remaining_quota"] == 20000


def test_complete_returns_text_usage_and_cost(monkeypatch, tmp_path) -> None:
    key_file = tmp_path / "gw.key"
    key_file.write_text("k\n")
    monkeypatch.setenv("FLOP_GATEWAY_KEY_FILE", str(key_file))
    monkeypatch.setattr(
        gw,
        "_request",
        lambda *a, **k: (
            200,
            {
                "id": "req-1",
                "model": "qwen/qwen2.5-0.5b-instruct",
                "choices": [{"message": {"content": "ready"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 1},
            },
        ),
    )
    res = gw.complete("say ready", max_tokens=4)
    assert res["ok"] is True
    assert res["text"] == "ready"
    assert res["request_id"] == "req-1"
    assert res["cost_flop"] == gw.cost_flop(12, 1)


def test_complete_refuses_an_empty_prompt() -> None:
    assert gw.complete("   ") == {"ok": False, "reason": "empty_prompt"}


def test_complete_propagates_the_channel_failure(monkeypatch, tmp_path) -> None:
    key_file = tmp_path / "gw.key"
    key_file.write_text("k\n")
    monkeypatch.setenv("FLOP_GATEWAY_KEY_FILE", str(key_file))
    monkeypatch.setattr(
        gw,
        "_request",
        lambda *a, **k: (503, {"error": {"message": "pooled openChannel failed"}}),
    )
    res = gw.complete("hi")
    assert res["ok"] is False
    assert res["reason"] == "channel_unfunded"


def test_receipts_reads_data_or_receipts(monkeypatch, tmp_path) -> None:
    key_file = tmp_path / "gw.key"
    key_file.write_text("k\n")
    monkeypatch.setenv("FLOP_GATEWAY_KEY_FILE", str(key_file))
    monkeypatch.setattr(gw, "_request", lambda *a, **k: (200, {"data": [{"id": 1}, {"id": 2}]}))
    res = gw.receipts()
    assert res["ok"] is True
    assert res["count"] == 2
    monkeypatch.setattr(gw, "_request", lambda *a, **k: (200, {"receipts": [{"id": 9}]}))
    assert gw.receipts()["count"] == 1


def test_classify_model_not_advertised_is_supply() -> None:
    body = {
        "error": {
            "code": "model_not_found",
            "message": "Model 'qwen/qwen2.5-0.5b-instruct' is not currently advertised by FLOP.",
        }
    }
    assert gw.classify(404, body) == "no_supply"


def test_models_reports_the_catalogue(monkeypatch, tmp_path) -> None:
    key_file = tmp_path / "gw.key"
    key_file.write_text("k\n")
    monkeypatch.setenv("FLOP_GATEWAY_KEY_FILE", str(key_file))
    monkeypatch.setattr(gw, "_request", lambda *a, **k: (200, {"data": [{"id": "qwen/qwen2.5-0.5b-instruct"}]}))
    res = gw.models()
    assert res["ok"] is True
    assert res["count"] == 1
    assert res["ids"] == ["qwen/qwen2.5-0.5b-instruct"]

    monkeypatch.setattr(gw, "_request", lambda *a, **k: (200, {"data": []}))
    assert gw.models()["count"] == 0


def test_classify_survives_a_string_error_body() -> None:
    # The gateway sometimes answers {"error": "invalid_address"} — no .get on a str.
    assert gw.classify(400, {"error": "invalid_address"}) == "request_rejected"
    assert gw.classify(401, {"error": "api_key_required"}) == "key_rejected"


def test_classify_enrollment_maintenance() -> None:
    body = {
        "error": "wallet_enrollment_unavailable",
        "message": "Wallet enrollment is temporarily unavailable.",
    }
    assert gw.classify(503, body) == "enrollment_unavailable"
