# LUMI — PHASE 12 memory service additional tests (approve/reject/supersede/sweep/search/vector)
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from memory.service import MemoryService
from observability.models import Base, MemoryStatus


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    S = async_sessionmaker(engine, expire_on_commit=False)
    async with S() as s:
        yield s
    await engine.dispose()


async def _candidate(session, content="memory content", **kw):
    svc = MemoryService(session)
    return await svc.create_candidate(content=content, source=kw.get("source", "test"),
                                      confidence=kw.get("confidence", 0.8),
                                      ttl_seconds=kw.get("ttl_seconds"), category=kw.get("category"))


@pytest.mark.asyncio
async def test_approve_auto(session):
    svc = MemoryService(session)
    item = await _candidate(session)
    r = await svc.approve(item.id, auto=True)
    assert r.status == MemoryStatus.AUTO_APPROVED.value
    assert r.verification_status == "verified"


@pytest.mark.asyncio
async def test_approve_wrong_status_returns_none(session):
    svc = MemoryService(session)
    item = await _candidate(session)
    await svc.approve(item.id)
    # ikinci approve (zaten APPROVED) → None
    assert await svc.approve(item.id) is None


@pytest.mark.asyncio
async def test_reject(session):
    svc = MemoryService(session)
    item = await _candidate(session)
    await svc.reject(item.id)
    got = await session.get(type(item), item.id)
    assert got.status == MemoryStatus.REJECTED.value


@pytest.mark.asyncio
async def test_mark_active(session):
    svc = MemoryService(session)
    item = await _candidate(session)
    await svc.approve(item.id)
    await svc.mark_active(item.id)
    got = await session.get(type(item), item.id)
    assert got.status == MemoryStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_supersede(session):
    svc = MemoryService(session)
    old = await _candidate(session, content="old information")
    new = await _candidate(session, content="new information")
    await svc.approve(old.id)
    await svc.approve(new.id)
    await svc.mark_active(old.id)
    await svc.mark_active(new.id)
    await svc.supersede(old.id, new.id)
    got = await session.get(type(old), old.id)
    assert got.status == MemoryStatus.SUPERSEDED.value


@pytest.mark.asyncio
async def test_supersede_invalid_new(session):
    svc = MemoryService(session)
    old = await _candidate(session)
    # Missing new_id → no-op
    await svc.supersede(old.id, uuid.uuid4())


@pytest.mark.asyncio
async def test_search_status_filter(session):
    svc = MemoryService(session)
    item = await _candidate(session, content="benzersiz-kelime-xyz")
    await svc.approve(item.id)
    await svc.mark_active(item.id)
    await session.commit()
    res = await svc.search("benzersiz-kelime", status=MemoryStatus.ACTIVE.value)
    assert any(str(i.id) == str(item.id) for i in res)


@pytest.mark.asyncio
async def test_search_wildcard_escaped(session):
    svc = MemoryService(session)
    # query containing '%' should not act as wildcard
    await _candidate(session, content="yuzde-50")
    await session.commit()
    res = await svc.search("yuzde%", verified_only=False)
    assert len(res) == 0


@pytest.mark.asyncio
async def test_expire_sweep(session):
    svc = MemoryService(session)
    await _candidate(session, content="past", ttl_seconds=-1)  # already expired
    await session.commit()
    n = await svc.expire_sweep()
    assert n >= 1


@pytest.mark.asyncio
async def test_list_status_invalid(session):
    svc = MemoryService(session)
    assert await svc.list_status("BOGUS") == []


@pytest.mark.asyncio
async def test_vector_search_fallback(session):
    svc = MemoryService(session)
    # SQLite has no vector operator → fallback empty list (exception caught)
    res = await svc.vector_search([0.1, 0.2, 0.3])
    assert res == []


@pytest.mark.asyncio
async def test_create_candidate_dlp_redact(session):
    svc = MemoryService(session)
    # TG bot token pattern (known DLP pattern) should be redacted
    token = "123456789:" + ("T" * 35)
    item = await svc.create_candidate(content=f"token: {token}", source="test")
    assert token not in item.content
    assert "<TG_TOKEN_REDACTED>" in item.content
