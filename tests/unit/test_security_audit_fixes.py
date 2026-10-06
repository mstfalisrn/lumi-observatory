# LUMI — audit regression tests (G01 traversal, G02 login limiter, G03 verified-lock reveal)
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from observability import models


# ---------------------------------------------------------------------------
# G01 — static assets must not escape the assets root
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_assets_route_blocks_path_traversal():
    from apps.api.app import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # encoded traversal: apps/web/dist/assets/../../../../README.md would be
        # the repo README under the old code (unauthenticated 200)
        r = await client.get("/assets/%2e%2e%2f%2e%2e%2f%2e%2e%2f%2e%2e%2fREADME.md")
        assert r.status_code == 404
        assert "LUMI" not in r.text
        # absolute-ish escape attempt
        r = await client.get("/assets/..%2f..%2f..%2f..%2fpyproject.toml")
        assert r.status_code == 404
        # normal asset names still resolve through the same route (no dist in the
        # test checkout -> 404 as well, but never a traversal 200)
        r = await client.get("/assets/index-abc123.js")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# G02 — the login route carries its own limiter (it is exempt from middleware)
# ---------------------------------------------------------------------------
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
async def test_login_is_rate_limited_per_ip_and_account(monkeypatch, sqlite_factory):
    import apps.api.app as api_mod
    from apps.api.app import app

    import observability.db as db_mod

    factory = sqlite_factory
    monkeypatch.setattr(db_mod, "async_session_factory", factory)
    monkeypatch.setattr(api_mod, "async_session_factory", factory)

    calls: list[str] = []

    async def deny(key, limit, window):
        calls.append(key)
        return False

    monkeypatch.setattr(api_mod.rate_limiter, "check", deny)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/v1/auth/login", json={"email": "a@b.c", "password": "x"})
        assert r.status_code == 429
        assert any(k.startswith("rl:login-ip:") for k in calls)

    # under the limit the normal flow continues (wrong credentials -> 401, not 429)
    calls.clear()

    async def allow(key, limit, window):
        calls.append(key)
        return True

    monkeypatch.setattr(api_mod.rate_limiter, "check", allow)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/v1/auth/login", json={"email": "a@b.c", "password": "x"})
        assert r.status_code == 401
        assert any(k.startswith("rl:login-acct:a@b.c") for k in calls)


# ---------------------------------------------------------------------------
# G03 — a reveal may only follow a verified lock (signed lane, claimable rail,
#       ref binding, expected payer); anything else stays pending
# ---------------------------------------------------------------------------
def _frame(kind: str, data: dict, signed: bool = True, author: str = ""):
    from connectors.tclk import TclkFrame

    return TclkFrame(kind=kind, data=data, raw="", signed=signed, author=author)


def _worker(tmp_path, monkeypatch):
    from apps.earn import blockrewards as br

    monkeypatch.setattr(br, "STATE", tmp_path / "state.json")
    return br.Worker(dry=True)


CONTRACT = "0xfec8244c40ca0b872a8833a38be86394b963fd2f026ee9de68a0f128d3213cf7"
OFFER_ID = "0xfec8244c40ca0b872a8833a38be86394b963fd2f"
PAYER = "did:key:z6Mks5jW7ZsA8fXBmN6PMCkVKuzbPui8cpeVCkqHgnbQFik7"


def test_lock_verified_gate(tmp_path, monkeypatch):
    w = _worker(tmp_path, monkeypatch)
    p = {"ref": OFFER_ID, "payer": PAYER, "phase": "delivered", "preimage": "0x1", "room": "r"}
    good = {"type": "lock", "rail": "paper", "ref": CONTRACT, "from": PAYER, "contract": CONTRACT}

    assert w._lock_verified(_frame("lock", good), p) is True

    # unsigned lane (raw room bytes) — never a commitment
    assert w._lock_verified(_frame("lock", good, signed=False), p) is False
    # rail we do not claim on
    assert w._lock_verified(_frame("lock", {**good, "rail": "evil-rail"}), p) is False
    # ref that binds to neither the contract nor our offer id
    assert w._lock_verified(_frame("lock", {**good, "ref": "0x" + "de" * 16}), p) is False
    # lock from a different payer than the offer's author
    assert w._lock_verified(_frame("lock", {**good, "from": "did:key:zOther"}), p) is False
    # missing ref -> fail closed
    assert w._lock_verified(_frame("lock", {**good, "ref": ""}), p) is False


def test_scan_deal_rooms_ignores_unsigned_lock(monkeypatch, tmp_path):
    """End-to-end of the gate: an unsigned lock in the deal room must not reveal."""
    from apps.earn import blockrewards as br

    w = _worker(tmp_path, monkeypatch)
    revealed: list[str] = []

    async def fake_reveal(client, contract):
        revealed.append(contract)

    monkeypatch.setattr(w, "reveal", fake_reveal)

    msgs = [
        {"seq": 1, "from": "did:key:zOther", "sig": None,
         "text": f'tclk1 {{"type":"lock","rail":"paper","ref":"{CONTRACT}","contract":"{CONTRACT}"}}'},
    ]

    async def fake_read_room(client, room, since, wait=0):
        return msgs, 1

    monkeypatch.setattr(br, "read_room", fake_read_room)
    w.pending[CONTRACT] = {"preimage": "0x1", "phase": "delivered", "ref": OFFER_ID,
                           "room": "mb-p-tclk-x", "payer": PAYER}

    import asyncio

    asyncio.run(w.scan_deal_rooms(None))
    assert revealed == []  # unsigned lock -> no reveal

    # ... while a properly signed lock from the expected payer does reveal
    msgs[0] = {"seq": 1, "from": PAYER, "sig": "c2ln",
               "text": f'tclk1 {{"type":"lock","rail":"paper","ref":"{CONTRACT}","contract":"{CONTRACT}"}}'}

    async def fake_reveal2(client, contract):
        revealed.append(contract)

    monkeypatch.setattr(w, "reveal", fake_reveal2)
    asyncio.run(w.scan_deal_rooms(None))
    assert revealed == [CONTRACT]
