# LUMI — audit follow-up: session revocation (G06) + streaming body cap (G07)
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from observability import models


@pytest_asyncio.fixture
async def sqlite_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _patch(monkeypatch, factory):
    # auth resolves observability.db at call time; the app module holds its own
    # imported reference — patch both.
    import apps.api.app as api_mod

    import observability.db as db_mod

    monkeypatch.setattr(db_mod, "async_session_factory", factory)
    monkeypatch.setattr(api_mod, "async_session_factory", factory)


async def _user(factory, *, email="a@b.c", role="admin", active=True, ver=0, pw="x"):
    async with factory() as s:
        u = models.User(
            username=email, display_name="T", role=role, is_active=active, password_hash=pw,
            token_version=ver,
        )
        s.add(u)
        await s.commit()
        return str(u.id)


async def _set_version(factory, uid, ver):
    import uuid as _uuid

    async with factory() as s:
        u = await s.get(models.User, _uuid.UUID(uid))
        u.token_version = ver
        await s.commit()


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# G06 — live account state is enforced on every request
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_revoked_token_rejected(monkeypatch, sqlite_factory):
    from apps.api.app import app

    from observability.auth import create_session_token

    factory = sqlite_factory
    _patch(monkeypatch, factory)
    uid = await _user(factory)
    token = create_session_token(uid, "admin", 3600, 0)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/v1/auth/me", headers=_bearer(token))
        assert r.status_code == 200, r.text
        await _set_version(factory, uid, 1)  # e.g. logout / password change elsewhere
        r2 = await c.get("/api/v1/auth/me", headers=_bearer(token))
        assert r2.status_code == 401
        assert "revoked" in r2.text


@pytest.mark.asyncio
async def test_inactive_account_rejected(monkeypatch, sqlite_factory):
    from apps.api.app import app

    from observability.auth import create_session_token

    factory = sqlite_factory
    _patch(monkeypatch, factory)
    uid = await _user(factory, active=False)
    token = create_session_token(uid, "admin", 3600, 0)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/v1/auth/me", headers=_bearer(token))
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_role_change_applies_immediately(monkeypatch, sqlite_factory):
    """The role comes from the DB row, not the token claim."""
    import uuid as _uuid

    from apps.api.app import app

    from observability.auth import create_session_token

    factory = sqlite_factory
    _patch(monkeypatch, factory)
    uid = await _user(factory, role="admin")
    token = create_session_token(uid, "admin", 3600, 0)
    async with factory() as s:
        u = await s.get(models.User, _uuid.UUID(uid))
        u.role = "viewer"
        await s.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/v1/auth/me", headers=_bearer(token))
        assert r.status_code == 200
        assert r.json()["role"] == "viewer"


@pytest.mark.asyncio
async def test_logout_revokes_the_token(monkeypatch, sqlite_factory):
    from apps.api.app import app

    from observability.auth import create_session_token

    factory = sqlite_factory
    _patch(monkeypatch, factory)
    uid = await _user(factory)
    token = create_session_token(uid, "admin", 3600, 0)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/auth/logout", headers=_bearer(token))
        assert r.status_code == 200, r.text
        r2 = await c.get("/api/v1/auth/me", headers=_bearer(token))
        assert r2.status_code == 401  # copied token cannot outlive the logout


@pytest.mark.asyncio
async def test_change_password_rotates_the_token(monkeypatch, sqlite_factory):
    from apps.api.app import app

    from observability.auth import create_session_token, hash_password

    factory = sqlite_factory
    _patch(monkeypatch, factory)
    uid = await _user(factory, pw=hash_password("oldpw12345"))
    token = create_session_token(uid, "admin", 3600, 0)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            "/api/v1/auth/change-password",
            headers=_bearer(token),
            json={"current_password": "oldpw12345", "new_password": "newpw12345"},
        )
        assert r.status_code == 200, r.text
        fresh = r.json().get("token")
        assert fresh  # the caller gets a replacement token
        r_old = await c.get("/api/v1/auth/me", headers=_bearer(token))
        assert r_old.status_code == 401  # old token revoked
        r_new = await c.get("/api/v1/auth/me", headers=_bearer(fresh))
        assert r_new.status_code == 200


# ---------------------------------------------------------------------------
# G07 — actual byte cap, including chunked bodies with no Content-Length
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_chunked_body_over_limit_is_413(monkeypatch):
    from apps.api.app import app

    async def chunks():
        for _ in range(12):  # ~1.2 MB > default 1 MiB cap, sent chunked
            yield b"x" * 100_000

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        # the login route is exempt from the header-based middleware check —
        # the byte-count middleware must still stop it
        r = await c.post("/api/v1/auth/login", content=chunks())
        assert r.status_code == 413, r.text[:200]


@pytest.mark.asyncio
async def test_oversized_content_length_is_413(monkeypatch):
    from apps.api.app import app

    big = b"y" * (2 * 1_048_576)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/auth/login", content=big)
        assert r.status_code == 413
