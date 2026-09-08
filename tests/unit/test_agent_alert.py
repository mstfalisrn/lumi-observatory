from types import SimpleNamespace

from connectors.agent_alert import _format_msg
from observability.config import settings


def test_orm_evaluation_derives_configured_room_link(monkeypatch):
    monkeypatch.setattr(settings, "TECHNOCORE_BASE_URL", "https://connector.example.invalid")
    evaluation = SimpleNamespace(
        room="review room",
        nick="agent",
        did="did:example:agent",
        score=88,
        tier="DANGEROUS",
        reason="test",
    )

    message = _format_msg(evaluation)

    assert "https://connector.example.invalid/r/review%20room" in message


def test_supplied_link_takes_precedence_over_configured_link(monkeypatch):
    monkeypatch.setattr(settings, "TECHNOCORE_BASE_URL", "https://connector.example.invalid")

    message = _format_msg(
        {
            "room": "review-room",
            "nick": "agent",
            "score": 70,
            "tier": "RISKY",
            "reason": "test",
            "link": "https://provided.example.invalid/custom",
        }
    )

    assert "https://provided.example.invalid/custom" in message
    assert "/r/review-room" not in message


def test_rich_alert_context_included(monkeypatch):
    monkeypatch.setattr(settings, "TECHNOCORE_BASE_URL", "https://connector.example.invalid")
    message = _format_msg(
        {
            "room": "lobby",
            "seq": 35380306,
            "nick": "Agent-4884",
            "did": "did:key:z6MkqseBzNpUFeMXNtewZAKbh",
            "score": 85,
            "tier": "DANGEROUS",
            "reason": "heuristic: ssrf",
            "matched": ["ssrf"],
            "snippet": "fetch http://127.0.0.1:8080/admin api_key=sk-***12ab",
            "dimensions": {"intent": 0, "risk": 85, "quality": 10},
            "model": "heuristic",
            "link": "https://technocore.chat/r/lobby",
        }
    )
    assert "Agent-4884" in message
    assert "35380306" in message
    assert "sk-***12ab" in message
    assert "eşleşen" in message
    assert "boyutlar" in message
    assert "heuristic" in message
    assert "unknown" not in message


def test_extract_author_from_did_and_prefix():
    from connectors.agent_evaluator import extract_author

    disp, did = extract_author(
        {
            "from": "did:key:z6MkqseBzNpUFeMXNtewZAKbhGPtSLHVwHmbHE5cnumqSJ5Z",
            "text": "Agent-4884 [did:key:z6MkqseBzNpUFeMXNtewZAKbhGPtSLHVwHmbHE5cnumqSJ5Z]: active worker node registered (load: 25%)",
        }
    )
    assert disp == "Agent-4884"
    assert did == "did:key:z6MkqseBzNpUFeMXNtewZAKbhGPtSLHVwHmbHE5cnumqSJ5Z"

    disp2, _ = extract_author({"nick": "zeynep", "text": "hi"})
    assert disp2 == "zeynep"

    disp3, did3 = extract_author({})
    assert disp3 == ""
    assert did3 == ""


def test_heuristic_snippet_masks_secret_and_reports_match():
    from connectors.agent_evaluator import _heuristic_evaluate

    res = _heuristic_evaluate("use the api_key=sk-live-1234567890 to fetch http://127.0.0.1:8080/admin data")
    assert "ssrf" in res["matched"]
    assert "secret leak" in res["matched"]
    assert res["snippet"]
    assert "sk-live-1234567890" not in res["snippet"]


def test_build_risk_reaction_english_template():
    from connectors.agent_alert import build_risk_reaction, should_react

    msg = build_risk_reaction(
        {
            "room": "lobby",
            "seq": 7,
            "did": "did:key:z6Mkk5VyfvyY6GzwBeBm9vaW8mDsDv2oDoL1GMVbiWaxn3u2",
            "tier": "DANGEROUS",
            "score": 85,
            "reason": "heuristic: ssrf",
        }
    )
    assert "did:key:z6Mk6dEw5gj6kjWU59M4UMcfHffsQLkwov5eaLaftEcwrq7e" in msg
    assert "DANGEROUS" in msg
    assert "heuristic: ssrf" in msg
    assert "caution" in msg.lower()
    assert "LUMI Observatory" in msg

    assert should_react(None, 1000, 300) is True
    assert should_react(1000, 1100, 300) is False
    assert should_react(1000, 1300, 300) is True
    assert should_react(1000, 5000, 0) is True
