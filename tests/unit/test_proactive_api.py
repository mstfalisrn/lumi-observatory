import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from observability import models


@pytest.mark.asyncio
async def test_operator_can_manage_safe_sources_digests_skills_and_trust_without_enabling_connectors(monkeypatch):
    import apps.api.app as api_mod
    from apps.api.app import app

    import observability.db as db_mod
    from observability.auth import create_session_token
    from observability.config import settings

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    original_db = db_mod.async_session_factory
    original_api = api_mod.async_session_factory
    db_mod.async_session_factory = factory  # type: ignore
    api_mod.async_session_factory = factory  # type: ignore
    monkeypatch.setattr(settings, "CONNECTOR_ALLOWED_HOSTS", "example.com")
    monkeypatch.setattr(settings, "SOURCE_MONITOR_ENABLED", False)
    monkeypatch.setattr(settings, "DIGEST_ENABLED", False)
    try:
        async with factory() as session:
            user = models.User(
                username="operator@example.com",
                display_name="Operator",
                role="admin",
                is_active=True,
                password_hash="x",
            )
            session.add(user)
            await session.commit()
            user_id = str(user.id)

        headers = {"Authorization": f"Bearer {create_session_token(user_id, 'admin')}"}
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            source = await client.post(
                "/api/v1/sources",
                headers=headers,
                json={
                    "name": "Approved JSON",
                    "source_type": "http_json",
                    "config": {"url": "https://example.com/data"},
                },
            )
            assert source.status_code == 201, source.text
            source_data = source.json()
            assert source_data["is_enabled"] is False
            assert source_data["reference"] == "https://example.com/data"

            sources = await client.get("/api/v1/sources", headers=headers)
            assert sources.status_code == 200
            assert len(sources.json()) == 1

            scan = await client.post(f"/api/v1/sources/{source_data['id']}/scan", headers=headers)
            assert scan.status_code == 409
            assert scan.json()["detail"] == "source_monitor_disabled"

            schedule = await client.post(
                "/api/v1/digest-schedules",
                headers=headers,
                json={"name": "Daily safe digest", "interval_minutes": 60, "source_ids": [], "minimum_tier": "WATCH"},
            )
            assert schedule.status_code == 201, schedule.text
            assert schedule.json()["delivery_mode"] == "report_only"

            preview = await client.get("/api/v1/digests/preview?hours=24", headers=headers)
            assert preview.status_code == 200
            assert preview.json()["delivery_mode"] == "report_only"

            generate = await client.post(f"/api/v1/digest-schedules/{schedule.json()['id']}/generate", headers=headers)
            assert generate.status_code == 409
            assert generate.json()["detail"] == "digest_workflow_disabled"

            skills = await client.get("/api/v1/skills", headers=headers)
            assert skills.status_code == 200
            assert any(item["id"] == "system-health" for item in skills.json()["skills"])

            trust = await client.get("/api/v1/trust/summary", headers=headers)
            assert trust.status_code == 200
            assert trust.json()["tiers"] == {"SAFE": 0, "WATCH": 0, "RISKY": 0, "DANGEROUS": 0}
    finally:
        db_mod.async_session_factory = original_db  # type: ignore
        api_mod.async_session_factory = original_api  # type: ignore
        await engine.dispose()
