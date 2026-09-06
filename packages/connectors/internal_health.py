# LUMI — internal health connector (hardening)
# Only Docker service DNS is used; localhost is only for the same container.
from __future__ import annotations

import httpx


class InternalHealthConnector:
    """Only LUMI container health information; no access to other services."""

    def __init__(self, base_urls: dict[str, str] | None = None) -> None:
        # Docker service DNS (inside compose network) — not localhost
        # If inside the same container, 127.0.0.1 can be used; otherwise use service name
        self._base_urls = base_urls or {
            "api": "http://lumi-api:8000/health/live",
            "worker": "http://lumi-worker:8001/health/live",
            "scheduler": "http://lumi-scheduler:8002/health/live",
        }
        # internal allow for health endpoints — not SSRF bypass, internal network
        self._client = httpx.AsyncClient(timeout=5.0, follow_redirects=False)
        self._closed = False

    async def check(self) -> dict:
        # DB/redis health verified via api /health/ready
        services: dict[str, str | None] = {
            **self._base_urls,
            "postgres": None,
            "redis": None,
        }
        result: dict = {}
        for name, url in services.items():
            if url is None:
                result[name] = {"reachable": False, "note": "verified via api /health/ready"}
                continue
            try:
                r = await self._client.get(url, timeout=3.0)
                result[name] = {"reachable": r.status_code == 200, "http": r.status_code}
            except Exception as e:
                result[name] = {"reachable": False, "error": type(e).__name__}
        return result

    async def check_local(self) -> dict:
        """Localhost health check from the same container (self-check only)."""
        local_urls = {
            "self": "http://127.0.0.1:8000/health/live",
        }
        result: dict = {}
        for name, url in local_urls.items():
            try:
                r = await self._client.get(url, timeout=3.0)
                result[name] = {"reachable": r.status_code == 200, "http": r.status_code}
            except Exception as e:
                result[name] = {"reachable": False, "error": type(e).__name__}
        return result

    async def aclose(self) -> None:
        if not self._closed:
            await self._client.aclose()
            self._closed = True

    async def close(self) -> None:
        await self.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.aclose()
