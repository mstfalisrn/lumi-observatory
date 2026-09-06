# LUMI — PHASE 12 Reporter tests
from agent_core.reporter import Reporter, build_public_report


def test_human_summary_completed():
    r = Reporter()
    assert "Completed" in r.human_summary({"status": "COMPLETED", "id": "1234567890abcdef"})


def test_human_summary_failed():
    r = Reporter()
    assert "Failed" in r.human_summary({"status": "FAILED", "error": "timeout"})


def test_human_summary_unknown():
    r = Reporter()
    assert r.human_summary({"status": "GARIP"}) == "ℹ️ GARIP"


def test_machine_result_defaults():
    r = Reporter()
    m = r.machine_result(run_id="r1", status="COMPLETED")
    assert m["run_id"] == "r1"
    assert m["evidence"] == []
    assert m["reports"] == []
    assert m["confidence"] == 0.0
    assert "generated_at" in m


def test_machine_result_with_data():
    r = Reporter()
    m = r.machine_result(run_id="r1", status="FAILED", claim="c", evidence=["e1"],
                         confidence=0.9, reports=["rep"], error="err")
    assert m["claim"] == "c"
    assert m["evidence"] == ["e1"]
    assert m["confidence"] == 0.9
    assert m["error"] == "err"


def test_build_public_report():
    p = build_public_report(report_type="change", observed_at="2026-01-01", subject="s",
                            change="c", evidence="e", confidence=0.8)
    assert p["type"] == "change"
    assert p["schema_version"] == 1
    assert p["confidence"] == 0.8
