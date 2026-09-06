# LUMI — PHASE 12 ApprovalService tests (create/decide/consume/replay/expiry)
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from observability.models import ApprovalStatus, Base
from policy.approval import ApprovalService


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    S = async_sessionmaker(engine, expire_on_commit=False)
    async with S() as s:
        yield s
    await engine.dispose()


async def _create(session, **kw):
    svc = ApprovalService(session)
    a = await svc.create(
        run_id="",
        action_id=kw.get("action_id", "a1"),
        tool=kw.get("tool", "github_repo_read"),
        arguments=kw.get("arguments", {}),
        action_class=kw.get("action_class", "PUBLIC_WRITE"),
        target=kw.get("target", "repo:x/y"),
        ttl_seconds=kw.get("ttl_seconds", 3600),
    )
    await session.commit()
    return a


@pytest.mark.asyncio
async def test_create_and_get(session):
    a = await _create(session)
    assert a.status == ApprovalStatus.PENDING.value
    assert a.action_hash
    got = await ApprovalService(session).get(str(a.id))
    assert got is not None and str(got.id) == str(a.id)


@pytest.mark.asyncio
async def test_get_invalid_id(session):
    assert await ApprovalService(session).get("not-a-uuid") is None


@pytest.mark.asyncio
async def test_decide_approve_then_consume_replay(session):
    a = await _create(session)
    svc = ApprovalService(session)
    d = await svc.decide(str(a.id), "approve", str(uuid.uuid4()))
    assert d.status == ApprovalStatus.APPROVED.value
    await session.commit()
    assert await svc.consume(str(a.id)) is True
    await session.commit()
    # replay protection: second consume is rejected
    assert await svc.consume(str(a.id)) is False


@pytest.mark.asyncio
async def test_decide_reject(session):
    a = await _create(session)
    d = await ApprovalService(session).decide(str(a.id), "reject", str(uuid.uuid4()))
    assert d.status == ApprovalStatus.REJECTED.value


@pytest.mark.asyncio
async def test_decide_twice_rejected(session):
    a = await _create(session)
    svc = ApprovalService(session)
    await svc.decide(str(a.id), "approve", str(uuid.uuid4()))
    await session.commit()
    with pytest.raises(ValueError):
        await svc.decide(str(a.id), "reject", str(uuid.uuid4()))


@pytest.mark.asyncio
async def test_decide_not_found(session):
    with pytest.raises(ValueError):
        await ApprovalService(session).decide(str(uuid.uuid4()), "approve", str(uuid.uuid4()))


@pytest.mark.asyncio
async def test_decide_invalid_decision(session):
    a = await _create(session)
    with pytest.raises(ValueError):
        await ApprovalService(session).decide(str(a.id), "maybe", str(uuid.uuid4()))


@pytest.mark.asyncio
async def test_consume_pending_rejected(session):
    a = await _create(session)
    # A PENDING approval cannot be consumed
    assert await ApprovalService(session).consume(str(a.id)) is False


@pytest.mark.asyncio
async def test_consume_invalid_id(session):
    assert await ApprovalService(session).consume("not-a-uuid") is False
