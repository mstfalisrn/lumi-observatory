# LUMI — F01: blockrewards restart recovery (delivered deals survive a restart)
import json

from apps.earn import blockrewards as br


def test_recover_pending_rebuilds_delivered_deal(tmp_path, monkeypatch):
    oid_ok = "0x" + "ab" * 20
    oid_inflight = "0x" + "cd" * 20
    state = {
        "pending": {
            "0xaaa1": {"oid": oid_ok, "room": "mb-p-tclk-x", "phase": "delivered",
                       "payer": "did:key:zPayer", "rails": ["paper"], "amount": "5", "asset": "FLOP"},
            "0xbbb2": {"oid": oid_inflight, "room": "mb-p-tclk-y", "phase": "accepted"},
        }
    }
    sf = tmp_path / "state.json"
    sf.write_text(json.dumps(state))
    monkeypatch.setattr(br, "STATE", sf)
    key = tmp_path / "did.ed25519"
    key.write_bytes(b"k" * 32)
    monkeypatch.setattr(br, "KEY_PATH", str(key))
    monkeypatch.setenv("TCLK_CLAIM_SEED", "00" * 32)

    w = br.Worker(dry=True)
    # delivered deal recovered with a re-derived preimage (no secret on disk)
    assert "0xaaa1" in w.pending
    rec = w.pending["0xaaa1"]
    assert rec["phase"] == "delivered" and rec["preimage"]
    assert rec["ref"] == oid_ok and rec["payer"] == "did:key:zPayer"
    # accepted-but-not-delivered is NOT auto-recovered (no double delivery)
    assert "0xbbb2" not in w.pending


def test_pending_record_has_no_secret_on_disk(tmp_path, monkeypatch):
    """The persisted record must never carry the preimage/statement."""
    oid = "0x" + "ee" * 20
    state = {"pending": {"0xccc3": {"oid": oid, "room": "r", "phase": "delivered"}}}
    sf = tmp_path / "state.json"
    sf.write_text(json.dumps(state))
    monkeypatch.setattr(br, "STATE", sf)
    key = tmp_path / "did.ed25519"
    key.write_bytes(b"k" * 32)
    monkeypatch.setattr(br, "KEY_PATH", str(key))
    monkeypatch.setenv("TCLK_CLAIM_SEED", "11" * 32)
    br.Worker(dry=True)
    raw = sf.read_text()
    assert "preimage" not in raw and "statement" not in raw
