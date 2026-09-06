# LUMI — PHASE 12 auth HTTP dependency + rate limiter redis tests
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from observability.auth import (
    RateLimiter,
    create_session_token,
    get_current_user,
)


# ---------------------------------------------------------------------------
# get_current_user
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_current_user_no_creds():
    with pytest.raises(HTTPException) as e:
        await get_current_user(None)
    assert e.value.status_code == 401


@pytest.mark.asyncio
async def test_get_current_user_invalid_token():
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="bozuk-token")
    with pytest.raises(HTTPException) as e:
        await get_current_user(creds)
    assert e.value.status_code == 401


@pytest.mark.asyncio
async def test_get_current_user_expired_token():
    expired = create_session_token("u1", "admin", expires_seconds=-10)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=expired)
    with pytest.raises(HTTPException) as e:
        await get_current_user(creds)
    assert e.value.status_code == 401


@pytest.mark.asyncio
async def test_get_current_user_valid_token():
    token = create_session_token("u1", "admin")
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    user = await get_current_user(creds)
    assert user["user_id"] == "u1"
    assert user["role"] == "admin"


# ---------------------------------------------------------------------------
# RateLimiter — redis yolu + reset + info
# ---------------------------------------------------------------------------
class _FakePipe:
    async def execute(self):
        return (1, True)


class _FakeRedis:
    def pipeline(self):
        return _FakePipe()

    async def delete(self, key):
        return 1


@pytest.mark.asyncio
async def test_rate_limiter_redis_path():
    rl = RateLimiter()
    rl._redis = _FakeRedis()
    rl._redis_tried = True
    assert await rl.check("k1", 10, 60) is True


@pytest.mark.asyncio
async def test_rate_limiter_memory_fallback():
    rl = RateLimiter()
    rl._redis = None
    rl._redis_tried = True
    assert await rl.check("k2", 2, 60) is True
    assert await rl.check("k2", 2, 60) is True
    assert await rl.check("k2", 2, 60) is False  # limit 2 exceeded
