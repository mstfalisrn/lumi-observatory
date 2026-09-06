# LUMI — PHASE 4 approval tests (hash binding + HMAC token + consume logic)
import hashlib
import hmac

from policy.engine import action_hash, build_approval_token, canonical_json


class TestActionHash:
    def test_payload_order_independent(self):
        h1 = action_hash("PUBLIC_WRITE", "/r/lobby", {"a": 1, "b": 2})
        h2 = action_hash("PUBLIC_WRITE", "/r/lobby", {"b": 2, "a": 1})
        assert h1 == h2, "hash must not depend on key order (canonical)"

    def test_different_payload_different_hash(self):
        h1 = action_hash("PUBLIC_WRITE", "/r/lobby", {"x": 1})
        h2 = action_hash("PUBLIC_WRITE", "/r/lobby", {"x": 2})
        assert h1 != h2

    def test_different_target_different_hash(self):
        h1 = action_hash("PUBLIC_WRITE", "/r/lobby", {"x": 1})
        h2 = action_hash("PUBLIC_WRITE", "/r/other", {"x": 1})
        assert h1 != h2


class TestApprovalToken:
    def test_hmac_not_plain_sha256(self):
        raw = "approval-1:hash123:user-1:1234567890"
        plain = hashlib.sha256(raw.encode()).hexdigest()
        hmac_token = build_approval_token("approval-1", "hash123", "user-1", 1234567890)
        assert hmac_token != plain, "token must not be plain SHA-256 (must be HMAC)"

    def test_hmac_key_bound(self):
        t1 = build_approval_token("a", "h", "u", 1)
        # different key gives different token (key-dependent)
        alt = hmac.new(b"wrong-key", b"a:h:u:1", hashlib.sha256).hexdigest()
        assert t1 != alt


class TestCanonicalJson:
    def test_deterministic(self):
        a = canonical_json({"b": [1, 2], "a": {"x": "y"}})
        b = canonical_json({"a": {"x": "y"}, "b": [1, 2]})
        assert a == b
