# LUMI — web UI password change + admin env-sync tests
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from observability import models
from observability.auth import create_session_token, hash_password, verify_password


@pytest_asyncio.fixture
async def sqlite_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_change_password_endpoint_flow(monkeypatch, sqlite_factory):
    import apps.api.app as api_mod
    from apps.api.app import app

    import observability.db as db_mod

    factory = sqlite_factory
    monkeypatch.setattr(db_mod, "async_session_factory", factory)
    monkeypatch.setattr(api_mod, "async_session_factory", factory)

    async with factory() as s:
        user = models.User(username="op@example.com", display_name="Op", role="admin",
                           is_active=True, password_hash=hash_password("old-pass-123"))
        s.add(user)
        await s.commit()
        uid = str(user.id)

    headers = {"Authorization": f"Bearer {create_session_token(uid, 'admin')}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # wrong current password -> 401
        r = await client.post("/api/v1/auth/change-password",
                              json={"current_password": "wrong-current", "new_password": "new-pass-456"},
                              headers=headers)
        assert r.status_code == 401
        # too short -> 400
        r = await client.post("/api/v1/auth/change-password",
                              json={"current_password": "old-pass-123", "new_password": "short"},
                              headers=headers)
        assert r.status_code == 400
        # unchanged -> 400
        r = await client.post("/api/v1/auth/change-password",
                              json={"current_password": "old-pass-123", "new_password": "old-pass-123"},
                              headers=headers)
        assert r.status_code == 400
        # happy path — the response carries a replacement token (sessions are
        # versioned, so the old token is revoked the moment the password changes)
        r = await client.post("/api/v1/auth/change-password",
                              json={"current_password": "old-pass-123", "new_password": "new-pass-456"},
                              headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert body.get("ok") is True and body.get("token")
        r_old = await client.get("/api/v1/auth/me", headers=headers)
        assert r_old.status_code == 401
        r_new = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {body['token']}"},
        )
        assert r_new.status_code == 200

        # DB really changed
        async with factory() as s:
            u = await s.get(models.User, uuid.UUID(uid))
            assert verify_password("new-pass-456", u.password_hash)
            assert not verify_password("old-pass-123", u.password_hash)

        # login: new password works, old one does not
        r = await client.post("/api/v1/auth/login",
                              json={"email": "op@example.com", "password": "new-pass-456"})
        assert r.status_code == 200
        r = await client.post("/api/v1/auth/login",
                              json={"email": "op@example.com", "password": "old-pass-123"})
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_admin_env_sync_applies_only_when_env_changes(monkeypatch, sqlite_factory):
    import apps.api.app as api_mod
    from apps.api.app import _sync_admin_password

    factory = sqlite_factory
    monkeypatch.setattr(api_mod, "async_session_factory", factory)
    monkeypatch.setattr(api_mod.settings, "ADMIN_EMAIL", "admin@example.com")

    env_a = hash_password("env-a-123")
    env_b = hash_password("env-b-456")
    ui_hash = hash_password("ui-choice-789")

    # first boot: seeds the admin + records the applied env value
    async with factory() as s:
        await _sync_admin_password(s, env_a)
    async with factory() as s:
        u = (await s.execute(models.User.__table__.select())).first()
        assert u is not None
        applied = await s.get(models.AppState, "admin_password_env_hash")
        assert applied.value == env_a

    # restart with the same env: no-op
    async with factory() as s:
        await _sync_admin_password(s, env_a)

    # a password changed from the web UI must survive a restart with unchanged env
    async with factory() as s:
        usr = (await s.execute(models.User.__table__.select())).first()
        await s.execute(models.User.__table__.update().where(models.User.id == usr.id).values(password_hash=ui_hash))
        await s.commit()
    async with factory() as s:
        await _sync_admin_password(s, env_a)
    async with factory() as s:
        usr = (await s.execute(models.User.__table__.select())).first()
        assert usr.password_hash == ui_hash  # UI choice survived

    # wizard reconfigure: a NEW env value is applied
    async with factory() as s:
        await _sync_admin_password(s, env_b)
    async with factory() as s:
        usr = (await s.execute(models.User.__table__.select())).first()
        assert usr.password_hash == env_b
        applied = await s.get(models.AppState, "admin_password_env_hash")
        assert applied.value == env_b
