# LUMI — PHASE 8 memory tests (lifecycle, DLP, verified/active retrieval)

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
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
class TestLifecycle:
    async def test_candidate_to_active(self, session):
        svc = MemoryService(session)
        item = await svc.create_candidate(content="an important fact", source="test", confidence=0.8)
        assert item.status == MemoryStatus.CANDIDATE.value
        await svc.approve(item.id)
        assert item.status == MemoryStatus.APPROVED.value
        await svc.mark_active(item.id)
        assert item.status == MemoryStatus.ACTIVE.value

    async def test_approve_guard_requires_candidate(self, session):
        svc = MemoryService(session)
        item = await svc.create_candidate(content="x", source="test")
        await svc.mark_active(item.id)  # CANDIDATE -> mark_active guard: only APPROVED/AUTO
        assert item.status == MemoryStatus.CANDIDATE.value  # unchanged

    async def test_reject(self, session):
        svc = MemoryService(session)
        item = await svc.create_candidate(content="x", source="test")
        await svc.reject(item.id)
        assert item.status == MemoryStatus.REJECTED.value


@pytest.mark.asyncio
class TestDLP:
    async def test_secret_redacted_on_create(self, session):
        svc = MemoryService(session)
        secret = "123456789:" + ("T" * 35)
        item = await svc.create_candidate(content=f"token {secret} stored", source="test")
        assert secret not in item.content, "secret must not be written to memory"


@pytest.mark.asyncio
class TestRetrieval:
    async def test_only_verified_active_returned(self, session):
        svc = MemoryService(session)
        # unverified candidate
        c = await svc.create_candidate(content="draft data", source="test")
        res = await svc.retrieve_for_context("draft", limit=10)
        assert all(m.id != c.id for m in res), "unverified candidates must not enter context"

        # approve + active
        await svc.approve(c.id)
        await svc.mark_active(c.id)
        res2 = await svc.retrieve_for_context("draft", limit=10)
        assert any(m.id == c.id for m in res2), "verified active candidates must enter context"
