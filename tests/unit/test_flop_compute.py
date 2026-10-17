"""FLOP testnet compute-spend path — spec constants, credit math and fail-closed rules.

The rules asserted here come from the public yellowpaper (v0.5 §8.2, R8.7/R8.8) and the
ratified decisions D-0514/0515/0517/0522: one lane (four calls) may spend locked
principal, only a settled payable accrues credit, credit is floor(K / 3), and a local
ceiling stands in for the protocol spend cap that Appendix H.4 still lists as open.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

MODULE_DIR = Path(__file__).resolve().parents[2] / "apps" / "earn"
sys.path.insert(0, str(MODULE_DIR))

import flop_compute as fc


def test_four_calls_may_spend_and_only_three_credit():
    assert set(fc.SPENDABLE_CALLS) == {
        "agent_transfer",
        "open_channel",
        "top_up_escrow",
        "force_open",
    }
    # §8.2: locked principal spent through agent_transfer is outside the payable.
    assert "agent_transfer" not in fc.COMPUTE_CREDIT_CALLS


def test_credit_is_floor_third_of_the_locked_part():
    assert fc.credit_for_payable(0) == 0
    assert fc.credit_for_payable(2) == 0
    assert fc.credit_for_payable(3) == 1
    assert fc.credit_for_payable(8) == 2
    assert fc.credit_for_payable(900) == 300
    with pytest.raises(ValueError):
        fc.credit_for_payable(-1)


def test_cap_is_three_quarters_of_principal():
    assert fc.spend_cap(1200) == 900
    assert fc.spend_cap(0) == 0
    assert fc.spend_cap(1) == 0
    with pytest.raises(ValueError):
        fc.spend_cap(-5)
    with pytest.raises(ValueError):
        fc.spend_cap(100, 1.5)


def test_plan_projects_a_quarter_liquid_at_the_cap():
    payload = fc.plan(1200, 0)
    assert payload["cap_flop"] == 900
    assert payload["remaining_flop"] == 900
    assert payload["projected_unlock_flop"] == 300
    assert payload["dispute_window_days"] == 7
    assert "open" in payload["note"]


def test_plan_remaining_never_goes_negative():
    payload = fc.plan(1200, 5000)
    assert payload["remaining_flop"] == 0
    assert payload["spent_flop"] == 5000


@pytest.mark.parametrize(
    ("call", "final", "closed", "upheld", "expected"),
    [
        ("open_channel", True, True, 0, True),
        ("top_up_escrow", True, True, 0, True),
        ("force_open", True, True, 0, True),
        ("open_channel", False, True, 0, False),
        ("open_channel", True, False, 0, False),
        ("open_channel", True, True, 1, False),
        ("agent_transfer", True, True, 0, False),
        ("refund", True, True, 0, False),
    ],
)
def test_credit_needs_a_compute_leg_a_final_settlement_and_a_clean_window(call, final, closed, upheld, expected):
    assert fc.is_creditable(call, final, closed, upheld) is expected


def test_spend_refuses_unknown_call():
    row = fc.attempt_spend("refund", 10, principal=1200, enabled=True, client_ok=True)
    assert row["kind"] == "blocked"
    assert row["reason"] == "unknown_call"


def test_spend_refuses_non_positive_amount():
    row = fc.attempt_spend("open_channel", 0, principal=1200, enabled=True, client_ok=True)
    assert row["kind"] == "blocked"
    assert row["reason"] == "invalid_amount"


def test_spend_refuses_past_the_local_cap():
    row = fc.attempt_spend("open_channel", 50, principal=1200, spent_flop=900, enabled=True, client_ok=True)
    assert row["kind"] == "blocked"
    assert row["reason"] == "cap_exceeded"
    assert "900" in row["detail"]


def test_spend_fails_closed_while_the_feature_is_off():
    row = fc.attempt_spend("open_channel", 40, principal=1200, enabled=False, client_ok=True)
    assert row["kind"] == "blocked"
    assert row["reason"] == "disabled"


def test_spend_fails_closed_without_a_published_client():
    row = fc.attempt_spend("open_channel", 40, principal=1200, enabled=True, client_ok=False)
    assert row["kind"] == "blocked"
    assert row["reason"] == "client_unavailable"
    assert "testnet client" in row["detail"]


def test_client_call_never_invents_an_interface():
    outcome = fc.client_call("open_channel", 40)
    assert outcome["ok"] is False
    assert outcome["reason"] == "client_unavailable"


def test_dry_run_records_intent_and_is_not_a_spend(tmp_path):
    row = fc.attempt_spend("open_channel", 40, principal=1200, dry_run=True)
    assert row["kind"] == "dry_run"
    assert row["creditable_leg"] is True
    fc.record(row, tmp_path)
    summary = fc.summarize(fc.read_ledger(tmp_path))
    assert summary["spent_flop"] == 0
    assert summary["dry_runs"] == 1


def test_ledger_is_append_only_jsonl(tmp_path):
    fc.record({"ts": fc.utc_now(), "kind": "dry_run", "call": "open_channel", "amount_flop": 10}, tmp_path)
    fc.record({"ts": fc.utc_now(), "kind": "blocked", "call": "open_channel", "reason": "disabled"}, tmp_path)
    path = tmp_path / fc.LEDGER_NAME
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 2
    assert all(json.loads(line)["kind"] for line in lines)


def test_summary_credits_only_clean_settled_payables(tmp_path):
    rows = [
        {"kind": "spend", "call": "open_channel", "amount_flop": 300},
        {"kind": "spend", "call": "agent_transfer", "amount_flop": 100},
        {"kind": "blocked", "reason": "cap_exceeded"},
        {"kind": "blocked", "reason": "cap_exceeded"},
        {"kind": "dry_run"},
        {
            "kind": "settlement",
            "settlement_final": True,
            "dispute_window_closed": True,
            "upheld_disputes": 0,
            "k_locked": 300,
        },
        {
            "kind": "settlement",
            "settlement_final": True,
            "dispute_window_closed": False,
            "upheld_disputes": 0,
            "k_locked": 90,
        },
    ]
    summary = fc.summarize(rows)
    assert summary["spent_flop"] == 400
    assert summary["spent_by_call"]["open_channel"] == 300
    assert summary["blocked_by_reason"] == {"cap_exceeded": 2}
    assert summary["credited_k_locked_flop"] == 300
    assert summary["unlocked_flop"] == 100
    assert summary["pending_settlements"] == 1
    assert summary["dry_runs"] == 1


def test_status_reports_plan_and_next_action(tmp_path, monkeypatch):
    monkeypatch.setenv("LUMI_EARN_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("FLOP_GRANT_PRINCIPAL", "1200")
    monkeypatch.setenv("FLOP_COMPUTE_ENABLED", "false")
    monkeypatch.delenv("FLOP_CLI", raising=False)
    monkeypatch.delenv("FLOP_TESTNET_RPC", raising=False)
    payload = fc.status(tmp_path)
    assert payload["enabled"] is False
    assert payload["client_available"] is False
    assert payload["plan"]["cap_flop"] == 900
    assert payload["plan"]["projected_unlock_flop"] == 300
    assert "compute" in payload["next_action"]


def test_env_ratio_falls_back_when_nonsense(monkeypatch):
    monkeypatch.setenv("FLOP_SPEND_CAP_RATIO", "not-a-number")
    assert fc.cap_ratio_from_env() == fc.DEFAULT_CAP_RATIO
    monkeypatch.setenv("FLOP_SPEND_CAP_RATIO", "2")
    assert fc.cap_ratio_from_env() == fc.DEFAULT_CAP_RATIO
    monkeypatch.setenv("FLOP_SPEND_CAP_RATIO", "0.5")
    assert fc.cap_ratio_from_env() == 0.5


def test_cli_status_json_and_fail_closed_exit_code(tmp_path):
    env = {**os.environ, "LUMI_EARN_STATE_DIR": str(tmp_path), "FLOP_GRANT_PRINCIPAL": "1200"}
    env.pop("FLOP_CLI", None)
    env.pop("FLOP_TESTNET_RPC", None)
    module = str(MODULE_DIR / "flop_compute.py")

    status_run = subprocess.run(  # noqa: S603 (fixed interpreter and module path, no untrusted input)
        [sys.executable, module, "--status", "--json"], capture_output=True, text=True, env=env, timeout=60
    )
    assert status_run.returncode == 0, status_run.stderr
    payload = json.loads(status_run.stdout)
    assert payload["plan"]["cap_flop"] == 900

    spend_run = subprocess.run(  # noqa: S603 (fixed interpreter and module path, no untrusted input)
        [sys.executable, module, "--spend", "--call", "open_channel", "--amount", "40", "--json"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert spend_run.returncode == 2
    result = json.loads(spend_run.stdout)["result"]
    assert result["kind"] == "blocked"
    assert result["reason"] in {"disabled", "client_unavailable"}
    assert (tmp_path / fc.LEDGER_NAME).exists()
