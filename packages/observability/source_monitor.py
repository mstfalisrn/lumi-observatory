"""Safe, content-addressed source observation.

This module is intentionally deterministic: it uses the existing bounded
connectors, stores metadata rather than arbitrary remote text, and makes only
unverified low-confidence memory candidates. It never publishes externally.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlparse, urlunparse

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from connectors.github import GithubRepoConnector
from connectors.http_json import HttpJsonConnector
from connectors.internal_health import InternalHealthConnector
from connectors.technocore import TechnocoreConnector
from memory.service import MemoryService
from observability import models
from observability.config import settings
from observability.security import redact

_SOURCE_TYPES = {item.value for item in models.SourceType}
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_ROOM_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_SENSITIVE_QUERY_KEYS = {"api_key", "apikey", "auth", "authorization", "key", "password", "secret", "token"}


class SourceMonitorError(ValueError):
    """A non-sensitive source configuration or connector error code."""


@dataclass(frozen=True)
class ObservationResult:
    source_id: str
    source_name: str
    observed_at: str
    change_type: str
    changed: bool
    content_hash: str = ""
    observation_id: str | None = None
    report_id: str | None = None
    evidence_id: str | None = None
    memory_candidate_id: str | None = None
    error_code: str | None = None

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


def _configured_hosts() -> set[str]:
    return {part.strip().lower() for part in settings.CONNECTOR_ALLOWED_HOSTS.split(",") if part.strip()}


def _is_blocked_ip(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved
    )


def validate_source_config(source_type: str, config: dict[str, Any] | None) -> dict[str, Any]:
    """Validate and normalize only the small, safe configuration surface."""
    normalized_type = str(source_type or "").strip().lower()
    if normalized_type not in _SOURCE_TYPES:
        raise SourceMonitorError("unsupported_source_type")
    raw = config if isinstance(config, dict) else {}

    if normalized_type == models.SourceType.HTTP_JSON.value:
        allowed_keys = {"url", "ingest_mode", "memory_ttl_seconds"}
        if set(raw) - allowed_keys:
            raise SourceMonitorError("unsupported_http_source_config")
        url = str(raw.get("url") or "").strip()
        parsed = urlparse(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise SourceMonitorError("invalid_http_source_url")
        host = parsed.hostname.lower()
        if _is_blocked_ip(host):
            raise SourceMonitorError("blocked_http_source_host")
        hosts = _configured_hosts()
        if not hosts or host not in hosts:
            raise SourceMonitorError("http_source_host_not_allowed")
        try:
            port = parsed.port
        except ValueError as exc:
            raise SourceMonitorError("invalid_http_source_url") from exc
        if port not in (None, 80, 443):
            raise SourceMonitorError("http_source_port_not_allowed")
        for key, _value in parse_qsl(parsed.query, keep_blank_values=True):
            if key.strip().lower() in _SENSITIVE_QUERY_KEYS:
                raise SourceMonitorError("sensitive_http_query_not_allowed")
        mode = str(raw.get("ingest_mode") or "metadata").strip().lower()
        if mode not in {"metadata", "curated_text"}:
            raise SourceMonitorError("invalid_ingest_mode")
        out: dict[str, Any] = {"url": url, "ingest_mode": mode}
        ttl = raw.get("memory_ttl_seconds")
        if ttl is not None:
            if not isinstance(ttl, int) or isinstance(ttl, bool) or not 3600 <= ttl <= 30 * 24 * 3600:
                raise SourceMonitorError("invalid_memory_ttl")
            out["memory_ttl_seconds"] = ttl
        return out

    if normalized_type == models.SourceType.GITHUB_REPO.value:
        if set(raw) - {"repo", "memory_ttl_seconds"}:
            raise SourceMonitorError("unsupported_github_source_config")
        repo = str(raw.get("repo") or "").strip()
        if not _REPO_RE.fullmatch(repo):
            raise SourceMonitorError("invalid_github_repo")
        return _with_optional_ttl({"repo": repo}, raw)

    if normalized_type == models.SourceType.TECHNOCORE_ROOM.value:
        if set(raw) - {"room", "memory_ttl_seconds"}:
            raise SourceMonitorError("unsupported_technocore_source_config")
        room = str(raw.get("room") or "").strip()
        if not _ROOM_RE.fullmatch(room):
            raise SourceMonitorError("invalid_technocore_room")
        return _with_optional_ttl({"room": room}, raw)

    if normalized_type == models.SourceType.INTERNAL_HEALTH.value:
        if raw:
            raise SourceMonitorError("internal_health_does_not_accept_config")
        return {}

    raise SourceMonitorError("unsupported_source_type")


def _with_optional_ttl(base: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    ttl = raw.get("memory_ttl_seconds")
    if ttl is None:
        return base
    if not isinstance(ttl, int) or isinstance(ttl, bool) or not 3600 <= ttl <= 30 * 24 * 3600:
        raise SourceMonitorError("invalid_memory_ttl")
    return {**base, "memory_ttl_seconds": ttl}


def safe_source_reference(source_type: str, config: dict[str, Any]) -> str:
    """Return a display/audit reference without query strings or credentials."""
    if source_type == models.SourceType.HTTP_JSON.value:
        parsed = urlparse(str(config.get("url") or ""))
        host = parsed.hostname or ""
        netloc = host if parsed.port in (None, 80, 443) else f"{host}:{parsed.port}"
        return urlunparse((parsed.scheme, netloc, parsed.path or "/", "", "", ""))
    if source_type == models.SourceType.GITHUB_REPO.value:
        return f"https://github.com/{config.get('repo', '')}"
    if source_type == models.SourceType.TECHNOCORE_ROOM.value:
        return f"technocore://{config.get('room', '')}"
    return "internal://health"


def _metadata_for_payload(source_type: str, payload: Any) -> dict[str, Any]:
    """Keep remote data untrusted and compact: store shape/allowlisted metadata, not instructions."""
    if source_type == models.SourceType.INTERNAL_HEALTH.value and isinstance(payload, dict):
        services: dict[str, Any] = {}
        for key, value in sorted(payload.items())[:12]:
            if isinstance(value, dict):
                services[str(key)[:80]] = {"reachable": bool(value.get("reachable")), "http": value.get("http")}
        return {"kind": "internal_health", "services": services}

    if source_type == models.SourceType.GITHUB_REPO.value and isinstance(payload, dict):
        fields = ("full_name", "pushed_at", "updated_at", "open_issues", "default_branch", "html_url")
        return {"kind": "github_repo", "fields": {key: redact(str(payload.get(key, "")))[:256] for key in fields}}

    if source_type == models.SourceType.TECHNOCORE_ROOM.value and isinstance(payload, dict):
        messages = payload.get("messages")
        return {
            "kind": "technocore_room",
            "message_count": len(messages) if isinstance(messages, list) else int(payload.get("count") or 0),
            "last_seq": int(payload.get("last_seq") or 0),
            "untrusted": True,
        }

    if isinstance(payload, dict):
        return {
            "kind": "http_object",
            "field_count": len(payload),
            "keys": sorted(str(key)[:80] for key in payload)[:30],
            "untrusted": True,
        }
    if isinstance(payload, list):
        return {"kind": "http_list", "item_count": len(payload), "untrusted": True}
    return {"kind": type(payload).__name__, "untrusted": True}


def _canonical_hash(metadata: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(metadata, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _summary(source: models.Source, change_type: str, metadata: dict[str, Any]) -> str:
    kind = metadata.get("kind", "source")
    if kind == "github_repo":
        fields = metadata.get("fields") if isinstance(metadata.get("fields"), dict) else {}
        return (
            f"{source.name}: {change_type.lower()} GitHub observation "
            f"(updated={fields.get('updated_at', '') or 'unknown'}, issues={fields.get('open_issues', '') or 'unknown'})."
        )
    if kind == "technocore_room":
        return f"{source.name}: {change_type.lower()} room metadata (messages={metadata.get('message_count', 0)})."
    if kind == "internal_health":
        services = metadata.get("services")
        service_rows = services if isinstance(services, dict) else {}
        reachable = sum(
            1 for value in service_rows.values() if isinstance(value, dict) and bool(value.get("reachable"))
        )
        return f"{source.name}: {change_type.lower()} internal health observation ({reachable} reachable service(s))."
    return f"{source.name}: {change_type.lower()} approved HTTP source metadata ({metadata.get('field_count', 0)} field(s))."


class SourceMonitor:
    """Observe one persisted source using bounded, independently SSRF-protected connectors."""

    async def observe(self, session: AsyncSession, source: models.Source) -> ObservationResult:
        now = datetime.now(UTC)
        source_id = str(source.id)
        try:
            config = validate_source_config(source.source_type, source.config)
            payload, source_url = await self._fetch(source.source_type, config)
            metadata = _metadata_for_payload(source.source_type, payload)
            content_hash = _canonical_hash(metadata)
        except Exception as exc:
            code = (
                exc.args[0]
                if isinstance(exc, SourceMonitorError) and exc.args
                else f"source_fetch_failed:{type(exc).__name__}"
            )
            await self._record_error(source, now, str(code)[:120])
            return ObservationResult(
                source_id=source_id,
                source_name=source.name,
                observed_at=now.isoformat(),
                change_type="ERROR",
                changed=False,
                error_code=str(code)[:120],
            )

        previous_hash = source.last_content_hash or ""
        change_type = "INITIAL" if not previous_hash else "CHANGED" if previous_hash != content_hash else "UNCHANGED"
        changed = change_type != "UNCHANGED"
        source.last_accessed_at = now
        source.last_observed_at = now
        source.last_content_hash = content_hash
        source.error_series = []
        source.backoff_until = None

        max_seq = await session.scalar(
            select(func.max(models.SourceObservation.seq)).where(models.SourceObservation.source_id == source.id)
        )
        observation = models.SourceObservation(
            source_id=source.id,
            observed_at=now,
            seq=int(max_seq or 0) + 1,
            change_type=change_type,
            change={"metadata": metadata, "untrusted": source.source_type != models.SourceType.INTERNAL_HEALTH.value},
            hash=content_hash,
        )
        session.add(observation)
        await session.flush()

        report_id: str | None = None
        evidence_id: str | None = None
        memory_id: str | None = None
        if changed:
            summary = _summary(source, change_type, metadata)
            evidence = models.EvidenceItem(
                source_url=source_url,
                content_hash=content_hash,
                claim=summary,
                confidence=0.75 if source.source_type == models.SourceType.INTERNAL_HEALTH.value else 0.4,
                verified=False,
            )
            report = models.Report(
                report_type="source_change",
                subject=source.name,
                summary=summary,
                body={
                    "schema_version": 1,
                    "source_id": source_id,
                    "source_type": source.source_type,
                    "change_type": change_type,
                    "observed_at": now.isoformat(),
                    "source_url": source_url,
                    "content_hash": content_hash,
                    "metadata": metadata,
                    "untrusted": source.source_type != models.SourceType.INTERNAL_HEALTH.value,
                },
                confidence=evidence.confidence,
            )
            session.add_all([evidence, report])
            await session.flush()
            evidence_id = str(evidence.id)
            report_id = str(report.id)

            if settings.SOURCE_MEMORY_CANDIDATES_ENABLED:
                ttl = config.get("memory_ttl_seconds", settings.SOURCE_MEMORY_TTL_SECONDS)
                candidate = await MemoryService(session).create_candidate(
                    content=(
                        f"Unverified source observation for {source.name}: {summary} "
                        f"Content hash {content_hash[:16]}. Review the source-change report before activation."
                    ),
                    source=source_url,
                    confidence=0.4 if source.source_type != models.SourceType.INTERNAL_HEALTH.value else 0.75,
                    ttl_seconds=int(ttl),
                    category="source_observation",
                    observed_at=now,
                )
                memory_id = str(candidate.id)

        return ObservationResult(
            source_id=source_id,
            source_name=source.name,
            observed_at=now.isoformat(),
            change_type=change_type,
            changed=changed,
            content_hash=content_hash,
            observation_id=str(observation.id),
            report_id=report_id,
            evidence_id=evidence_id,
            memory_candidate_id=memory_id,
        )

    async def _fetch(self, source_type: str, config: dict[str, Any]) -> tuple[Any, str]:
        if source_type == models.SourceType.HTTP_JSON.value:
            async with HttpJsonConnector(allowed_hosts=_configured_hosts()) as connector:
                return await connector.get_json(str(config["url"])), safe_source_reference(source_type, config)
        if source_type == models.SourceType.GITHUB_REPO.value:
            async with GithubRepoConnector() as connector:
                return await connector.repo_activity(str(config["repo"])), safe_source_reference(source_type, config)
        if source_type == models.SourceType.INTERNAL_HEALTH.value:
            async with InternalHealthConnector() as connector:
                return await connector.check(), safe_source_reference(source_type, config)
        if source_type == models.SourceType.TECHNOCORE_ROOM.value:
            if not settings.TECHNOCORE_ENABLED or not settings.TECHNOCORE_BASE_URL:
                raise SourceMonitorError("technocore_disabled")
            connector = TechnocoreConnector(
                settings.TECHNOCORE_BASE_URL, ed25519_key_path=settings.TECHNOCORE_ED25519_KEY_PATH
            )
            try:
                return await connector.read_room(str(config["room"]), since=0, wait=2), safe_source_reference(
                    source_type, config
                )
            finally:
                await connector.aclose()
        raise SourceMonitorError("unsupported_source_type")

    async def _record_error(self, source: models.Source, now: datetime, code: str) -> None:
        series = list(source.error_series) if isinstance(source.error_series, list) else []
        series.append({"at": now.isoformat(), "code": code})
        source.error_series = series[-10:]
        source.last_accessed_at = now
        delay = min(3600, 30 * (2 ** min(len(series) - 1, 6)))
        source.backoff_until = now + timedelta(seconds=delay)
