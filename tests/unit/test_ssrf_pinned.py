# LUMI — G05: the validated IP is the actual TCP destination (pinned transport)
import asyncio

import httpcore
import pytest

from connectors import ssrf


def test_pinned_backend_connects_to_validated_ip(monkeypatch):
    """The socket target is the SSRF-validated IP, never a fresh DNS answer."""
    seen: dict = {}

    async def fake_connect(self, host, port, timeout=None, local_address=None, socket_options=None):
        seen["host"] = host
        seen["port"] = port
        return "stream"

    monkeypatch.setattr(ssrf, "resolve_all", lambda host, **kw: ["93.184.216.34", "93.184.216.35"])
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", fake_connect)

    backend = ssrf.PinnedNetworkBackend()
    stream = asyncio.run(backend.connect_tcp("example.com", 443))
    assert stream == "stream"
    assert seen["host"] == "93.184.216.34"  # pinned IP, not the hostname
    assert seen["port"] == 443


def test_pinned_backend_refuses_when_only_blocked_addresses(monkeypatch):
    """A rebound DNS answer (loopback/private) cannot be connected to — fail closed."""
    monkeypatch.setattr(ssrf, "resolve_all", lambda host, **kw: ["127.0.0.1", "10.0.0.5"])
    backend = ssrf.PinnedNetworkBackend()
    with pytest.raises(ssrf.SSRFError):
        asyncio.run(backend.connect_tcp("rebind.example", 80))


def test_http_json_connector_uses_pinned_backend():
    from connectors.http_json import HttpJsonConnector

    c = HttpJsonConnector()
    try:
        backend = c._client._transport._pool._network_backend
        assert isinstance(backend, ssrf.PinnedNetworkBackend)
    finally:
        asyncio.run(c.aclose())
