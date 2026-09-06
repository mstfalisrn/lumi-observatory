# LUMI — HTTP/JSON connector (SSRF-protected)
from __future__ import annotations

import json

import httpx

from connectors.ssrf import resolve_redirect_url, validate_url

_ALLOWED_CONTENT_TYPES = {"application/json", "application/vnd.api+json", "application/ld+json"}
_TEXT_FALLBACK_TYPES = {"text/plain", "text/markdown", "text/x-markdown", "application/octet-stream"}


class HttpJsonConnector:
    def __init__(self, allowed_hosts: set[str] | None = None,
                 max_bytes: int = 1_048_576, max_redirects: int = 3) -> None:
        self.allowed_hosts = allowed_hosts
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self._client = httpx.AsyncClient(
            timeout=20.0, follow_redirects=False, max_redirects=max_redirects
        )
        self._closed = False

    async def get_json(self, url: str) -> dict:
        validate_url(url, self.allowed_hosts)
        current = url
        redirects = 0
        while True:
            # re-validate after each redirect (DNS pin + allowlist)
            validate_url(current, self.allowed_hosts)
            # apply byte limit during download with streaming
            async with self._client.stream("GET", current) as resp:
                # redirect handling
                if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
                    redirects += 1
                    if redirects > self.max_redirects:
                        raise RuntimeError("too many redirects")
                    location = resp.headers["location"]
                    current = resolve_redirect_url(current, location, self.allowed_hosts)
                    continue

                # Content-Type allowlist
                ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                is_json = ctype in _ALLOWED_CONTENT_TYPES or "json" in ctype
                is_text = ctype in _TEXT_FALLBACK_TYPES

                # Content-Length early check (not sufficient alone, but early abort)
                clen = resp.headers.get("content-length")
                if clen is not None:
                    try:
                        if int(clen) > self.max_bytes:
                            raise RuntimeError("response size limit exceeded (content-length)")
                    except ValueError:
                        pass

                resp.raise_for_status()

                # Streaming byte limiti
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes(chunk_size=8192):
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise RuntimeError("response size limit exceeded (streaming)")
                    chunks.append(chunk)
                body = b"".join(chunks)
                if not body:
                    return {}
                # JSON parse — text fallback wrapped as {"text": ...}
                if not is_json and is_text:
                    try:
                        return {"text": body.decode("utf-8", errors="replace")[:5000], "content_type": ctype}
                    except Exception:
                        return {"text": body.decode("latin-1", errors="replace")[:5000], "content_type": ctype}
                # Non-JSON, non-text content-type must be rejected
                if not is_json and not is_text:
                    raise RuntimeError(f"Content-Type outside allowlist: {ctype}")
                try:
                    return json.loads(body)
                except json.JSONDecodeError as e:
                    if is_text:
                        try:
                            return {"text": body.decode("utf-8", errors="replace")[:5000], "content_type": ctype}
                        except Exception:
                            pass
                    raise RuntimeError(f"JSON parse error: {e}") from e

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
