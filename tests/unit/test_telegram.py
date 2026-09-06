# LUMI — PHASE 5 Telegram tests (singleton, dedup helper, redact, allowlist)
from agent_core.telegram import TelegramService, get_service, webhook_opaque_path
from observability.security import redact


class TestSingleton:
    def test_get_service_returns_same_instance(self):
        a = get_service()
        b = get_service()
        assert a is b, "a new instance should not be created on each call (singleton)"


class TestOpaquePath:
    def test_deterministic(self):
        p1 = webhook_opaque_path("my-secret")
        p2 = webhook_opaque_path("my-secret")
        assert p1 == p2
        assert len(p1) == 32  # first 32 SHA-256 hex characters

    def test_different_secret_different_path(self):
        assert webhook_opaque_path("a") != webhook_opaque_path("b")

    def test_empty_secret_empty_path(self):
        assert webhook_opaque_path("") == ""


class TestRedactToken:
    def test_telegram_token_masked(self):
        token = "123456789:" + ("T" * 35)
        out = redact(f"error: {token} request failed")
        assert token not in out, "token must not appear in log/text"
        assert "TG_TOKEN_REDACTED" in out

    def test_llm_key_masked(self):
        key = "sk-" + ("K" * 32)
        out = redact(f"key={key}")
        assert key not in out
        assert "REDACTED" in out


class TestAllowlist:
    def test_empty_env_denies(self):
        svc = TelegramService.__new__(TelegramService)  # bypass __init__; do not read the settings token
        # if env allowlist is empty, allowed() returns False (fail-closed)
        # This test only verifies the 'allowed' method's empty-list behavior
        from observability.config import settings
        original = settings.allowed_user_ids
        settings.TELEGRAM_ALLOWED_USER_IDS = ""
        try:
            assert svc.allowed(123456789) is False
        finally:
            settings.TELEGRAM_ALLOWED_USER_IDS = ",".join(str(x) for x in original) if original else ""
