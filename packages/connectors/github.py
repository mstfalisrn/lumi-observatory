# LUMI — GitHub public repo connector (SSRF-protected)
from __future__ import annotations

import asyncio
import json

import httpx

from connectors.ssrf import validate_host

_ALLOWED_CT = {"application/json", "application/vnd.github+json", "application/vnd.github.v3+json"}

class GithubRepoConnector:
    def __init__(self, max_bytes: int = 2_000_000, redirects: int = 3, max_retries: int = 2) -> None:
        self.max_bytes = max_bytes
        self.max_retries = max_retries
        self._client = httpx.AsyncClient(
            timeout=25.0, follow_redirects=False, max_redirects=redirects
        )
        self._closed = False

    async def _get_json_stream(self, url: str, *, headers: dict | None = None, params: dict | None = None) -> dict | list:
        """Fetch JSON with streaming, a byte limit, a Content-Type allowlist, and retry/backoff."""
        validate_host("api.github.com")
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                async with self._client.stream("GET", url, headers=headers, params=params) as resp:
                    # rate-limit 429 / 403 handling
                    if (resp.status_code in (429, 403) and "retry-after" in resp.headers) or "x-ratelimit-remaining" in resp.headers:
                        if resp.headers.get("x-ratelimit-remaining") == "0" or resp.status_code == 429:
                            retry_after = resp.headers.get("retry-after", "2")
                            try:
                                delay = float(retry_after)
                            except ValueError:
                                delay = 2.0
                            if attempt < self.max_retries:
                                await asyncio.sleep(min(delay, 30))
                                continue
                    # Content-Type allowlist
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if ctype and ctype not in _ALLOWED_CT and "json" not in ctype:
                        raise RuntimeError(f"Content-Type outside allowlist: {ctype}")
                    # Check Content-Length early
                    clen = resp.headers.get("content-length")
                    if clen is not None:
                        try:
                            if int(clen) > self.max_bytes:
                                raise RuntimeError("response size limit exceeded (content-length)")
                        except ValueError:
                            pass
                    resp.raise_for_status()
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes(chunk_size=8192):
                        total += len(chunk)
                        if total > self.max_bytes:
                            raise RuntimeError("response size limit exceeded (streaming)")
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    return json.loads(body) if body else {}
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_exc = e
                if attempt < self.max_retries:
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    continue
                raise
            except RuntimeError:
                raise
        if last_exc:
            raise last_exc
        raise RuntimeError("github fetch failed")

    async def repo_activity(self, repo: str) -> dict:
        # repo: "owner/name"
        validate_host("api.github.com")  # SSRF security check
        parts = repo.strip("/").split("/")
        if len(parts) < 2:
            raise ValueError("repo must be in 'owner/name' format")
        owner, name = parts[0], parts[1]
        url = f"https://api.github.com/repos/{owner}/{name}"
        data = await self._get_json_stream(url, headers={"Accept": "application/vnd.github+json"})
        assert isinstance(data, dict)
        return {
            "full_name": data.get("full_name"),
            "pushed_at": data.get("pushed_at"),
            "updated_at": data.get("updated_at"),
            "open_issues": data.get("open_issues_count"),
            "default_branch": data.get("default_branch"),
            "html_url": data.get("html_url"),
        }

    async def recent_releases(self, repo: str, per_page: int = 5) -> list[dict]:
        validate_host("api.github.com")
        owner, name = repo.strip("/").split("/")[:2]
        url = f"https://api.github.com/repos/{owner}/{name}/releases"
        data = await self._get_json_stream(
            url, params={"per_page": per_page},
            headers={"Accept": "application/vnd.github+json"},
        )
        assert isinstance(data, list)
        return [
            {"tag_name": r.get("tag_name"), "published_at": r.get("published_at"),
             "name": r.get("name")}
            for r in data
        ]

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
