# LUMI — PHASE 12 auth HTTP dependency + rate limiter redis tests
import pytest
import pytest_asyncio
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from observability import models
from observability.auth import (
    RateLimiter,
    create_session_token,
    get_current_user,
)


@pytest_asyncio.fixture
async def sqlite_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


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
async def test_get_current_user_valid_token(monkeypatch, sqlite_factory):
    # A signed JWT alone is not a session any more: the account must exist and
    # be active, and the token version must match (G06). "u1" style subjects
    # without a backing row are rejected — build a real user.
    import observability.db as db_mod

    factory = sqlite_factory
    monkeypatch.setattr(db_mod, "async_session_factory", factory)

    async with factory() as s:
        u = models.User(username="u1@example.com", display_name="u", role="admin",
                        is_active=True, password_hash="x")
        s.add(u)
        await s.commit()
        uid = str(u.id)

    token = create_session_token(uid, "admin", token_version=0)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    user = await get_current_user(creds)
    assert user["user_id"] == uid
    assert user["role"] == "admin"


@pytest.mark.asyncio
async def test_get_current_user_unknown_subject_rejected(monkeypatch, sqlite_factory):
    # valid signature, but no such account: rejected rather than trusted
    import observability.db as db_mod

    factory = sqlite_factory
    monkeypatch.setattr(db_mod, "async_session_factory", factory)

    token = create_session_token("00000000-0000-0000-0000-000000000000", "admin")
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    with pytest.raises(HTTPException) as e:
        await get_current_user(creds)
    assert e.value.status_code == 401


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
