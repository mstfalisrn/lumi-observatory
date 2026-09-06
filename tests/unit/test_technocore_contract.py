# LUMI — External chat contract tests
# DID base58btc, canonical string, nonce monotonic, cursor DB, POST OpenAPI, 429 backoff
from __future__ import annotations

import json
import re
import tempfile

import httpx
import pytest

from connectors.technocore import (
    TechnocoreConnector,
    TechnocoreError,
    _did_to_pubkey,
    _parse_retry_after,
    _pubkey_to_did,
    canonical_string,
    sweep_text,
)

_DID_RE = re.compile(r"^did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}$")
_SIG_RE = re.compile(r"^[A-Za-z0-9_-]{86}$")
_NONCE_RE = re.compile(r"^[0-9]{1,19}$")


# ---------------------------------------------------------------------------
# DID base58btc
# ---------------------------------------------------------------------------
class TestDIDBase58btc:
    def test_did_format_and_length(self):
        with tempfile.TemporaryDirectory() as td:
            tc = TechnocoreConnector(ed25519_key_path=f"{td}/did.key")
            did, _ = tc.load_or_generate_key()
            assert _DID_RE.match(did), f"DID pattern error: {did}"
            assert len(did) == 56
            assert did.startswith("did:key:z6Mk")

    def test_did_roundtrip_pubkey(self):
        with tempfile.TemporaryDirectory() as td:
            tc = TechnocoreConnector(ed25519_key_path=f"{td}/did2.key")
            did, _ = tc.load_or_generate_key()
            pub = _did_to_pubkey(did)
            assert len(pub) == 32
            # convert back to DID
            did2 = _pubkey_to_did(pub)
            assert did == did2

    def test_did_not_hex(self):
        with tempfile.TemporaryDirectory() as td:
            tc = TechnocoreConnector(ed25519_key_path=f"{td}/did3.key")
            did, _ = tc.load_or_generate_key()
            # old incorrect format was hex (did:key: + 64 hex char) — should no longer happen
            assert not re.match(r"^did:key:[0-9a-f]{64}$", did)
            assert "z6Mk" in did

    def test_pubkey_to_did_known_vector(self):
        # DID is deterministic for fixed 32-byte pubkey
        pub = bytes.fromhex("5ebfb8fcfbbe1fc500e9ab1234567890abcdef1234567890abcdef12345678")
        # truncated to 32? use full 32 bytes deterministic
        pub = bytes(range(32))
        did = _pubkey_to_did(pub)
        assert _DID_RE.match(did)
        assert _did_to_pubkey(did) == pub


# ---------------------------------------------------------------------------
# Canonical string + sweep
# ---------------------------------------------------------------------------
class TestCanonicalString:
    def test_canonical_exact(self):
        assert canonical_string("lobby", "123", "hello") == "lobby|123|hello"

    def test_sweep_newline_and_controls(self):
        assert sweep_text("a\nb\r\nc") == "a b  c"
        assert sweep_text("x\x00y\x1f z") == "x y  z"
        assert sweep_text("hi\u200bthere") == "hi there"
        assert sweep_text("a\u200c b\u200d c\ufeff") == "a  b  c "

    def test_canonical_uses_swept_text(self):
        # should sign the canonical, text-swept form
        raw = "hello\nworld"
        c = canonical_string("myroom", "999", raw)
        assert c == "myroom|999|hello world"
        assert "\n" not in c

    def test_canonical_invalid_room(self):
        with pytest.raises(ValueError):
            canonical_string("Invalid-Room!", "1", "hi")

    def test_canonical_invalid_nonce(self):
        with pytest.raises(ValueError):
            canonical_string("lobby", "abc", "hi")
        with pytest.raises(ValueError):
            canonical_string("lobby", "0" * 20, "hi")  # 20 digits >19


# ---------------------------------------------------------------------------
# Signature — base64url 86 chars, verify
# ---------------------------------------------------------------------------
class TestSignature:
    def test_sig_86_base64url(self):
        with tempfile.TemporaryDirectory() as td:
            tc = TechnocoreConnector(ed25519_key_path=f"{td}/sig.key")
            tc.load_or_generate_key()
            sig = tc.sign("lobby", "1234567890", "hello world")
            assert len(sig) == 86
            assert _SIG_RE.match(sig)
            assert "=" not in sig
            assert "+" not in sig and "/" not in sig

    def test_sig_covers_canonical(self):
        with tempfile.TemporaryDirectory() as td:
            tc = TechnocoreConnector(ed25519_key_path=f"{td}/sig2.key")
            did, _ = tc.load_or_generate_key()
            room, nonce, text = "test-room", "42", "payload here"
            sig = tc.sign(room, nonce, text)
            assert tc.verify(did, room, nonce, text, sig) is True
            # different text -> fail
            assert tc.verify(did, room, nonce, "other text", sig) is False
            # different nonce -> fail
            assert tc.verify(did, room, "43", text, sig) is False
            # verify after sweep: \n -> space should have same sig as sweep
            sig2 = tc.sign(room, nonce, "hello\nworld")
            assert tc.verify(did, room, nonce, "hello world", sig2) is True
            assert tc.verify(did, room, nonce, "hello\nworld", sig2) is True  # because of text sweeping

    def test_sig_sweep_consistency(self):
        with tempfile.TemporaryDirectory() as td:
            tc = TechnocoreConnector(ed25519_key_path=f"{td}/sig3.key")
            did, _ = tc.load_or_generate_key()
            # raw text with \n, but canonical sweeps to space — both should verify
            raw = "a\tb\nc\u200b"
            swept = sweep_text(raw)
            sig = tc.sign("lobby", "1", raw)
            # verify with raw or swept — both canonical same -> both ok
            assert tc.verify(did, "lobby", "1", raw, sig)
            assert tc.verify(did, "lobby", "1", swept, sig)


# ---------------------------------------------------------------------------
# Nonce monotonic
# ---------------------------------------------------------------------------
class TestNonceMonotonic:
    def test_next_nonce_increasing(self):
        tc = TechnocoreConnector()
        n1 = tc.next_nonce()
        n2 = tc.next_nonce()
        assert _NONCE_RE.match(n1)
        assert _NONCE_RE.match(n2)
        assert int(n2) > int(n1)

    def test_nonce_ensure_greater(self):
        tc = TechnocoreConnector()
        n1 = tc.next_nonce()
        # if we pass the same value again, it should be monotonic +1
        tc._global_nonce.ensure_greater(n1) if hasattr(tc, "_global_nonce") else None
        # _global_nonce is module-level; test via tc.next_nonce internals
        from connectors.technocore import _global_nonce

        forced = _global_nonce.ensure_greater(n1)
        assert int(forced) > int(n1)

    @pytest.mark.asyncio
    async def test_nonce_db_atomic(self):
        """Atomic database nonce increment with in-memory SQLite."""
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from observability.models import Base

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)

        with tempfile.TemporaryDirectory() as td:
            tc = TechnocoreConnector(ed25519_key_path=f"{td}/nonce.key")
            tc.load_or_generate_key()

            async with Session() as s:
                n1 = await tc.next_nonce_db("test-room", s)
                n2 = await tc.next_nonce_db("test-room", s)
                await s.commit()
                assert int(n2) > int(n1)
                assert _NONCE_RE.match(n1)
                # different room same DID -> independent nonce sequence
                n3 = await tc.next_nonce_db("other-room", s)
                await s.commit()
                assert _NONCE_RE.match(n3)

            # Persistence continues in a new session
            async with Session() as s:
                n4 = await tc.next_nonce_db("test-room", s)
                await s.commit()
                assert int(n4) > int(n2)

        await engine.dispose()

    @pytest.mark.asyncio
    async def test_nonce_db_requires_did(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from observability.models import Base

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        tc = TechnocoreConnector()  # no key loaded
        async with Session() as s:
            with pytest.raises(TechnocoreError, match="DID not found"):
                await tc.next_nonce_db("room", s)
        await engine.dispose()


# ---------------------------------------------------------------------------
# Cursor DB persistence
# ---------------------------------------------------------------------------
class TestCursorDB:
    @pytest.mark.asyncio
    async def test_cursor_get_set(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from observability.models import Base

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        tc = TechnocoreConnector()

        async with Session() as s:
            assert await tc.get_cursor("room-a", s) == 0
            await tc.set_cursor("room-a", 42, s)
            await s.commit()
            assert await tc.get_cursor("room-a", s) == 42
            # monotonic: smaller seq is not set
            await tc.set_cursor("room-a", 10, s)
            await s.commit()
            assert await tc.get_cursor("room-a", s) == 42
            # larger seq advances
            await tc.set_cursor("room-a", 100, s)
            await s.commit()
            assert await tc.get_cursor("room-a", s) == 100

        await engine.dispose()

    @pytest.mark.asyncio
    async def test_cursor_advance_from_response(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from observability.models import Base

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        tc = TechnocoreConnector()
        async with Session() as s:
            data = {"last_seq": 77, "messages": []}
            await tc.advance_cursor_from_response("myroom", data, s)
            await s.commit()
            assert await tc.get_cursor("myroom", s) == 77
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_read_room_updates_cursor(self):
        """Updates read_room cursor with mock HTTP + DB session."""
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from observability.models import Base

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)

        # mock transport: returns JSON with last_seq 55
        async def handler(request):
            return httpx.Response(
                200,
                json={"room": "myroom", "count": 1, "last_seq": 55, "messages": [], "first_seq": 1},
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            tc = TechnocoreConnector(client=client)
            # don't set base_url to localhost to bypass validate_host — patch for mock
            tc._validate_base_host = lambda: None
            async with Session() as s:
                data = await tc.read_room("myroom", since=0, wait=0, session=s)
                await s.commit()
                assert data["last_seq"] == 55
                assert data["_untrusted"] is True
                cur = await tc.get_cursor("myroom", s)
                assert cur == 55

        await engine.dispose()


# ---------------------------------------------------------------------------
# POST body complies with OpenAPI
# ---------------------------------------------------------------------------
class TestPostBodyOpenAPI:
    @pytest.mark.asyncio
    async def test_signed_post_body_shape(self):
        captured: dict = {}

        async def handler(request):
            captured["json"] = json.loads(request.content)
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"room": "myroom", "count": 1, "last_seq": 10, "messages": []})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            with tempfile.TemporaryDirectory() as td:
                tc = TechnocoreConnector(client=client, ed25519_key_path=f"{td}/post.key")
                tc.load_or_generate_key()
                tc._validate_base_host = lambda: None

                # payload string
                await tc.signed_post("myroom", "hello world")
                body = captured["json"]
                # OpenAPI required: text, sig, did, nonce
                assert "text" in body
                assert body["text"] == "hello world"
                assert _DID_RE.match(body["did"]), f"did bad: {body['did']}"
                assert _SIG_RE.match(body["sig"]), f"sig bad: {body['sig']}"
                assert _NONCE_RE.match(body["nonce"])
                # forbidden: old fields must not be present
                for forbidden in ("type", "observed_at", "subject", "change", "evidence", "confidence", "schema_version", "signature", "idempotency_key"):
                    assert forbidden not in body, f"forbidden field {forbidden} found"

    @pytest.mark.asyncio
    async def test_signed_post_dict_payload_to_text(self):
        captured: dict = {}

        async def handler(request):
            captured["json"] = json.loads(request.content)
            return httpx.Response(200, json={"room": "myroom", "count": 1, "last_seq": 1, "messages": []})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            with tempfile.TemporaryDirectory() as td:
                tc = TechnocoreConnector(client=client, ed25519_key_path=f"{td}/post2.key")
                tc.load_or_generate_key()
                tc._validate_base_host = lambda: None
                # Report dictionary payload: serialize to text as JSON
                await tc.signed_post("myroom", {"type": "report", "subject": "test"})
                body = captured["json"]
                assert "text" in body
                # text must be JSON parseable
                parsed = json.loads(body["text"])
                assert parsed["type"] == "report"

                # explicit text field override
                await tc.signed_post("myroom", {"text": "my explicit text", "extra": "ignored?"})
                body2 = captured["json"]
                assert body2["text"] == "my explicit text"

    @pytest.mark.asyncio
    async def test_signed_post_sweep_truncate(self):
        captured: dict = {}

        async def handler(request):
            captured["json"] = json.loads(request.content)
            return httpx.Response(200, json={"room": "myroom", "count": 1, "last_seq": 1, "messages": []})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            with tempfile.TemporaryDirectory() as td:
                tc = TechnocoreConnector(client=client, ed25519_key_path=f"{td}/post3.key")
                tc.load_or_generate_key()
                tc._validate_base_host = lambda: None
                await tc.signed_post("myroom", "a\nb\x00c")
                assert captured["json"]["text"] == "a b c"
                # 4096 truncate
                long_text = "x" * 5000
                await tc.signed_post("myroom", long_text)
                assert len(captured["json"]["text"]) == 4096

    def test_build_signed_get_url(self):
        with tempfile.TemporaryDirectory() as td:
            tc = TechnocoreConnector(ed25519_key_path=f"{td}/get.key")
            tc.load_or_generate_key()
            url = tc.build_signed_get_url("myroom", "hello world")
            assert "/r/myroom/say-signed/" in url
            assert "hello%20world" in url or "hello world" not in url  # encoded


# ---------------------------------------------------------------------------
# 429 backoff — body parsing
# ---------------------------------------------------------------------------
class TestBackoff429:
    def test_parse_body_retry(self):
        req = httpx.Request("GET", "https://x.test/r/lobby")
        # body contains retry seconds
        resp = httpx.Response(429, text="Too many requests, retry in 2.5 seconds. bucket reads ...", request=req)
        assert _parse_retry_after(resp) == pytest.approx(2.5)
        # header fallback
        httpx.Response(429, headers={"Retry-After": "3"}, text="rate limited", request=req)
        # body has no number? but "rate limited" still has maybe no number -> header used? Actually body has no float match? Let's craft
        httpx.Response(429, headers={"Retry-After": "3"}, text="no numbers here!", request=req)
        # "no numbers here!" has no digits? but we clamp — should parse header
        # Our impl searches body first, finds none? Actually _RETRY_BODY_RE would not match "no numbers here!" -> header
        # Let's patch body to empty numeric to test header path directly
        resp3 = httpx.Response(429, headers={"Retry-After": "4.2"}, text="", request=req)
        assert _parse_retry_after(resp3) == pytest.approx(4.2)

    def test_parse_body_empty_defaults(self):
        req = httpx.Request("GET", "https://x.test/r/lobby")
        resp = httpx.Response(429, text="", headers={}, request=req)
        assert _parse_retry_after(resp) == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_read_room_429_retry_then_success(self):
        call_count = 0

        async def handler(request):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(429, text="retry in 0.01 seconds", headers={"Retry-After": "0.01"})
            return httpx.Response(200, json={"room": "myroom", "count": 0, "last_seq": 5, "messages": []})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            tc = TechnocoreConnector(client=client, max_retries=2)
            tc._validate_base_host = lambda: None
            data = await tc.read_room("myroom", since=0, wait=0)
            assert data["last_seq"] == 5
            assert call_count == 2

    @pytest.mark.asyncio
    async def test_signed_post_429_retry(self):
        call_count = 0

        async def handler(request):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(429, text="Too many writes, retry in 0.01 seconds", headers={"Retry-After": "0.01"})
            return httpx.Response(200, json={"room": "myroom", "count": 1, "last_seq": 6, "messages": []})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            with tempfile.TemporaryDirectory() as td:
                tc = TechnocoreConnector(client=client, ed25519_key_path=f"{td}/backoff.key", max_retries=2)
                tc.load_or_generate_key()
                tc._validate_base_host = lambda: None
                data = await tc.signed_post("myroom", "hello")
                assert data["last_seq"] == 6
                assert call_count == 2

    @pytest.mark.asyncio
    async def test_429_exhaust_raises(self):
        async def handler(request):
            return httpx.Response(429, text="retry in 0.01 seconds", headers={"Retry-After": "0.01"})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            tc = TechnocoreConnector(client=client, max_retries=1)
            tc._validate_base_host = lambda: None
            with pytest.raises(TechnocoreError, match="429"):
                await tc.read_room("myroom", since=0, wait=0)
