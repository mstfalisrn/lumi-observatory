# LUMI — PHASE 2 auth/RBAC tests (local session, no Cloudflare Access)
import jwt as pyjwt
import pytest

from observability.auth import (
    ROLE_ORDER,
    create_session_token,
    decode_session_token,
    hash_password,
    verify_password,
)


class TestPasswordHash:
    def test_hash_and_verify(self):
        h = hash_password("s3cret-pass")
        assert verify_password("s3cret-pass", h)
        assert not verify_password("wrong", h)

    def test_hash_is_not_plaintext(self):
        h = hash_password("s3cret-pass")
        assert "s3cret-pass" not in h
        assert h.startswith("pbkdf2_sha256$")


class TestSessionToken:
    def test_roundtrip(self):
        tok = create_session_token("u-1", "admin", 3600)
        dec = decode_session_token(tok)
        assert dec["sub"] == "u-1"
        assert dec["role"] == "admin"
        assert dec["iss"] == "lumi-observatory"

    def test_expired_rejected(self):
        # negative expires_seconds -> expire immediately (exp in the past)
        tok = create_session_token("u-1", "viewer", expires_seconds=-10)
        with pytest.raises(pyjwt.ExpiredSignatureError):
            decode_session_token(tok)

    def test_tampered_rejected(self):
        tok = create_session_token("u-1", "admin", 3600)
        # break the signature
        parts = tok.split(".")
        parts[1] = "AAAA" + parts[1][4:]
        tampered = ".".join(parts)
        with pytest.raises(Exception):
            decode_session_token(tampered)

    def test_wrong_secret_rejected(self):
        tok = create_session_token("u-1", "admin", 3600)
        with pytest.raises(pyjwt.InvalidSignatureError):
            pyjwt.decode(tok, "wrong-secret-key-for-test", algorithms=["HS256"])


class TestRBAC:
    def test_role_order(self):
        assert ROLE_ORDER["admin"] > ROLE_ORDER["operator"] > ROLE_ORDER["viewer"]

    def test_require_role_logic(self):
        # require_role behavior: viewer < operator < admin
        assert ROLE_ORDER.get("viewer", -1) < ROLE_ORDER.get("operator", 0)
        assert ROLE_ORDER.get("admin", -1) >= ROLE_ORDER.get("operator", 0)
