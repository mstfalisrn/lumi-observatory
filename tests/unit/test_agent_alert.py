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
