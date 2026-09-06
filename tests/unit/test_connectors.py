# LUMI — PHASE 12 connector tests (http_json, internal_health, github)
import json

import httpx
import pytest

from connectors.github import GithubRepoConnector
from connectors.http_json import HttpJsonConnector
from connectors.internal_health import InternalHealthConnector


class _FakeResp:
    def __init__(self, status_code=200, headers=None, body=b"", chunks=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body
        self._chunks = chunks

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    async def aiter_bytes(self, chunk_size=8192):
        if self._chunks is not None:
            for c in self._chunks:
                yield c
        elif self._body:
            yield self._body


class _FakeStream:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        pass


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp

    def stream(self, method, url, **kw):
        return _FakeStream(self._resp)

    async def get(self, url, **kw):
        return self._resp

    async def aclose(self):
        pass


@pytest.fixture(autouse=True)
def _no_ssrf(monkeypatch):
    # SSRF check is tested separately (test_ssrf); no-op in connector test
    monkeypatch.setattr("connectors.http_json.validate_url", lambda *a, **k: None)
    monkeypatch.setattr("connectors.http_json.resolve_redirect_url", lambda *a, **k: a[1])
    monkeypatch.setattr("connectors.github.validate_host", lambda *a, **k: None)


# ---------- http_json ----------
@pytest.mark.asyncio
async def test_http_json_success():
    c = HttpJsonConnector(allowed_hosts={"example.com"})
    c._client = _FakeClient(_FakeResp(200, {"content-type": "application/json"},
                                      body=json.dumps({"a": 1}).encode()))
    assert await c.get_json("https://example.com/x") == {"a": 1}


@pytest.mark.asyncio
async def test_http_json_content_length_limit():
    c = HttpJsonConnector(allowed_hosts={"example.com"}, max_bytes=10)
    c._client = _FakeClient(_FakeResp(200, {"content-type": "application/json",
                                            "content-length": "999999"}))
    with pytest.raises(RuntimeError):
        await c.get_json("https://example.com/x")


@pytest.mark.asyncio
async def test_http_json_content_type_rejected():
    c = HttpJsonConnector(allowed_hosts={"example.com"})
    c._client = _FakeClient(_FakeResp(200, {"content-type": "text/html"}, body=b"<html>"))
    with pytest.raises(RuntimeError):
        await c.get_json("https://example.com/x")


@pytest.mark.asyncio
async def test_http_json_parse_error():
    c = HttpJsonConnector(allowed_hosts={"example.com"})
    c._client = _FakeClient(_FakeResp(200, {"content-type": "application/json"}, body=b"not json"))
    with pytest.raises(RuntimeError):
        await c.get_json("https://example.com/x")


@pytest.mark.asyncio
async def test_http_json_empty_body():
    c = HttpJsonConnector(allowed_hosts={"example.com"})
    c._client = _FakeClient(_FakeResp(200, {"content-type": "application/json"}, body=b""))
    assert await c.get_json("https://example.com/x") == {}


# ---------- internal_health ----------
@pytest.mark.asyncio
async def test_internal_health_check():
    c = InternalHealthConnector()
    c._client = _FakeClient(_FakeResp(200))
    r = await c.check()
    assert r["api"]["reachable"] is True
    assert r["postgres"]["reachable"] is False  # None url → via api


@pytest.mark.asyncio
async def test_internal_health_check_failure():
    class _Boom:
        async def get(self, url, **kw):
            raise RuntimeError("down")

        async def aclose(self):
            pass

    c = InternalHealthConnector()
    c._client = _Boom()
    r = await c.check()
    assert r["api"]["reachable"] is False
    assert r["api"]["error"] == "RuntimeError"


@pytest.mark.asyncio
async def test_internal_health_check_local():
    c = InternalHealthConnector()
    c._client = _FakeClient(_FakeResp(200))
    r = await c.check_local()
    assert r["self"]["reachable"] is True


# ---------- github ----------
@pytest.mark.asyncio
async def test_github_repo_activity():
    c = GithubRepoConnector()
    c._client = _FakeClient(_FakeResp(200, {"content-type": "application/vnd.github+json"},
                                      body=json.dumps({"full_name": "o/r", "pushed_at": "x",
                                                       "default_branch": "main"}).encode()))
    r = await c.repo_activity("o/r")
    assert r["full_name"] == "o/r"
    assert r["default_branch"] == "main"


@pytest.mark.asyncio
async def test_github_repo_activity_invalid():
    c = GithubRepoConnector()
    with pytest.raises(ValueError):
        await c.repo_activity("tekparca")


@pytest.mark.asyncio
async def test_github_recent_releases():
    c = GithubRepoConnector()
    c._client = _FakeClient(_FakeResp(200, {"content-type": "application/vnd.github+json"},
                                      body=json.dumps([{"tag_name": "v1", "published_at": "x"}]).encode()))
    r = await c.recent_releases("o/r")
    assert r[0]["tag_name"] == "v1"


class _SequenceClient:
    """Client returning sequential responses (for retry/429 tests)."""

    def __init__(self, responses):
        self._responses = list(responses)

    def stream(self, method, url, **kw):
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return _FakeStream(resp)

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_github_rate_limit_429_retry(monkeypatch):
    async def _nosleep(_):
        return None

    monkeypatch.setattr("connectors.github.asyncio.sleep", _nosleep)
    c = GithubRepoConnector()
    r429 = _FakeResp(429, {"retry-after": "2", "x-ratelimit-remaining": "0"}, body=b"")
    rok = _FakeResp(200, {"content-type": "application/vnd.github+json"},
                    body=json.dumps({"full_name": "o/r", "default_branch": "main"}).encode())
    c._client = _SequenceClient([r429, rok])
    r = await c.repo_activity("o/r")
    assert r["full_name"] == "o/r"


@pytest.mark.asyncio
async def test_github_transport_error_retry(monkeypatch):
    async def _nosleep(_):
        return None

    monkeypatch.setattr("connectors.github.asyncio.sleep", _nosleep)
    c = GithubRepoConnector()
    err = httpx.TransportError("connection reset")
    rok = _FakeResp(200, {"content-type": "application/vnd.github+json"},
                    body=json.dumps({"full_name": "o/r", "default_branch": "main"}).encode())
    c._client = _SequenceClient([err, rok])
    r = await c.repo_activity("o/r")
    assert r["full_name"] == "o/r"


@pytest.mark.asyncio
async def test_github_streaming_size_limit():
    c = GithubRepoConnector(max_bytes=10)
    c._client = _FakeClient(_FakeResp(200, {"content-type": "application/vnd.github+json"},
                                      chunks=[b"12345678901234567890"]))
    with pytest.raises(RuntimeError):
        await c.repo_activity("o/r")


@pytest.mark.asyncio
async def test_github_close_idempotent():
    c = GithubRepoConnector()
    c._client = _FakeClient(_FakeResp(200))
    await c.aclose()
    await c.aclose()  # idempotent
    await c.close()
    async with c as _c:
        assert _c is c
