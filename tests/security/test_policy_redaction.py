# LUMI — policy motoru + redaction birim testleri (workflow-agnostic)

from observability.security import redact
from policy.engine import PolicyEngine, action_hash


class TestPolicy:
    def test_read_only_auto(self):
        e = PolicyEngine()
        d = e.decide("technocore_read")
        assert d.decision == "ALLOW"
        assert d.action_class == "READ_ONLY"

    def test_safe_write_auto(self):
        e = PolicyEngine()
        d = e.decide("db_self_write")
        assert d.decision == "ALLOW"

    def test_public_write_needs_approval(self):
        e = PolicyEngine()
        d = e.decide("technocore_signed_write")
        assert d.decision == "REQUIRE_APPROVAL"

    def test_privileged_needs_approval(self):
        e = PolicyEngine()
        d = e.decide("apply_privileged")
        assert d.decision == "REQUIRE_APPROVAL"

    def test_destructive_denied(self):
        e = PolicyEngine()
        d = e.decide("destructive_op")
        assert d.decision == "DENY"

    def test_unknown_tool_defaults_read_only_allow(self):
        e = PolicyEngine()
        assert e.decide("unknown_tool").decision == "DENY"


class TestRedaction:
    def test_bearer_redacted(self):
        out = redact("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.secret")
        assert "<REDACTED>" in out

    def test_telegram_token_redacted(self):
        token = "123456789:" + ("T" * 35)
        out = redact(f"token={token}")
        assert "<TG_TOKEN_REDACTED>" in out

    def test_jwt_redacted(self):
        out = redact("bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
        assert "<JWT_REDACTED>" in out

    def test_explicit_env_assignment_redacted(self):
        out = redact("TELEGRAM_BOT_TOKEN=123456789:AAHSecretTokenValue")
        assert "<REDACTED>" in out

    def test_runtime_env_value_redacted(self):
        from observability.security import load_secrets_from_env
        load_secrets_from_env({"JWT_SECRET": "runtime-unique-JWT-9999-super-secret-xyz123"})
        out = redact("leaked runtime-unique-JWT-9999-super-secret-xyz123 in logs")
        assert "runtime-unique-JWT-9999-super-secret-xyz123" not in out
        assert "<ENV_REDACTED>" in out

    def test_runtime_env_idempotent(self):
        from observability.security import load_secrets_from_env
        load_secrets_from_env({"LLM_API_KEY": "idempotent-key-1234567890-ABCDEF123456"})
        n2 = load_secrets_from_env({"LLM_API_KEY": "idempotent-key-1234567890-ABCDEF123456"})
        assert n2 == 0  # should not insert a second time
        out = redact("idempotent-key-1234567890-ABCDEF123456 leaked")
        assert "<ENV_REDACTED>" in out

    def test_runtime_env_ignores_placeholder(self):
        from observability.security import load_secrets_from_env
        n = load_secrets_from_env({"JWT_SECRET": "CHANGE_ME"})
        assert n == 0
        n2 = load_secrets_from_env({"DB_PASSWORD": "short"})
        assert n2 == 0  # <12 char

    def test_action_hash_binds_content(self):
        h1 = action_hash("PUBLIC_WRITE", "/r/lobby", {"x": 1})
        h2 = action_hash("PUBLIC_WRITE", "/r/lobby", {"x": 2})
        assert h1 != h2  # approval cannot be moved to another content
