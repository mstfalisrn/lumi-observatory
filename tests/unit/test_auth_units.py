# LUMI — PHASE 12 auth unit tests (password, session, RBAC, rate limit)
import time

import pytest
from fastapi import HTTPException

from observability.auth import (
    RateLimiter,
    create_session_token,
    decode_session_token,
    hash_password,
    require_role,
    verify_password,
)


def test_hash_password_roundtrip():
    stored = hash_password("test-password-123")
    assert stored.startswith("pbkdf2_sha256$")
    assert verify_password("test-password-123", stored) is True
    assert verify_password("incorrect", stored) is False


def test_verify_password_malformed():
    assert verify_password("x", "not-a-valid-hash") is False


def test_session_token_roundtrip():
    token = create_session_token("user-1", "operator")
    payload = decode_session_token(token)
    assert payload["sub"] == "user-1"
    assert payload["role"] == "operator"


def test_session_token_expired():
    token = create_session_token("u", "viewer", expires_seconds=-1)
    import jwt as _jwt
    with pytest.raises(_jwt.ExpiredSignatureError):
        decode_session_token(token)


@pytest.mark.asyncio
async def test_require_role_admin_blocks_viewer():
    dep = require_role("admin")
    with pytest.raises(HTTPException):
        await dep({"role": "viewer"})
    assert await dep({"role": "admin"}) == {"role": "admin"}


@pytest.mark.asyncio
async def test_require_role_operator_allows_admin():
    dep = require_role("operator")
    assert (await dep({"role": "admin"}))["role"] == "admin"


@pytest.mark.asyncio
async def test_rate_limiter_memory_fallback():
    rl = RateLimiter()
    rl._redis = None
    rl._redis_tried = True
    # 2 istek izinli, 3. reddedilir
    assert await rl.check("k1", limit=2, window_seconds=60) is True
    assert await rl.check("k1", limit=2, window_seconds=60) is True
    assert await rl.check("k1", limit=2, window_seconds=60) is False


@pytest.mark.asyncio
async def test_rate_limiter_window_expiry():
    rl = RateLimiter()
    rl._redis = None
    rl._redis_tried = True
    assert await rl.check("k2", limit=1, window_seconds=60) is True
    # make the window appear as past (old timestamp)
    rl._mem["k2"] = [time.time() - 120]
    assert await rl.check("k2", limit=1, window_seconds=60) is True
