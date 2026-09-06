# LUMI — Tool executor (only registered, schema-validated tools; no arbitrary shell/docker)
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from connectors.github import GithubRepoConnector
from connectors.http_json import HttpJsonConnector
from connectors.internal_health import InternalHealthConnector
from connectors.technocore import TechnocoreConnector


class ToolRegistry:
    def __init__(self) -> None:
        self._fns: dict[str, Callable[..., Awaitable[Any]]] = {}
        self._schemas: dict[str, dict] = {}
        self.technocore: Any = None  # TechnocoreConnector (attached for status)

    def register(self, name: str, fn, schema: dict) -> None:
        self._fns[name] = fn
        self._schemas[name] = schema

    def has(self, name: str) -> bool:
        return name in self._fns

    def schema(self, name: str) -> dict:
        return self._schemas.get(name, {})

    async def call(self, name: str, **kw) -> Any:
        if name not in self._fns:
            raise KeyError(f"unregistered tool: {name}")
        # runtime JSON-schema validation (required + type)
        schema = self._schemas.get(name, {})
        params = schema.get("parameters") or {}
        required = params.get("required") or []
        props = params.get("properties") or {}
        for r in required:
            if r not in kw:
                raise ValueError(f"tool {name}: missing required argument: {r}")
        for k, v in list(kw.items()):
            if k not in props and props:
                raise ValueError(f"tool {name}: unknown argument: {k}")
            exp = props.get(k, {}).get("type")
            if exp == "string" and not isinstance(v, str):
                raise ValueError(f"tool {name}: {k} must be string")
            if exp == "integer" and not isinstance(v, int):
                raise ValueError(f"tool {name}: {k} must be integer")
            if exp == "object" and not isinstance(v, dict):
                raise ValueError(f"tool {name}: {k} must be object")
        return await self._fns[name](**kw)


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, task: dict | None = None) -> None:
        self.registry = registry
        self.task = task or {}

    async def execute(self, tool: str, **kw) -> Any:
        return await self.registry.call(tool, **kw)


def build_default_registry(
    *,
    http_hosts: set[str] | None = None,
    technocore_key_path: str = "",
    technocore_base_url: str = "",
) -> ToolRegistry:
    reg = ToolRegistry()
    http = HttpJsonConnector(allowed_hosts=http_hosts)
    gh = GithubRepoConnector()
    health = InternalHealthConnector()
    # Technocore is optional — only initialize when explicitly enabled
    from observability.config import settings as _settings
    tc = None
    if _settings.TECHNOCORE_ENABLED and (_settings.TECHNOCORE_BASE_URL or technocore_base_url):
        effective_url = technocore_base_url or _settings.TECHNOCORE_BASE_URL
        if effective_url:
            tc = TechnocoreConnector(effective_url, ed25519_key_path=technocore_key_path)
            try:
                tc.load_key(technocore_key_path)
            except Exception:
                tc._signing_key = None
                tc._did_pub = None
    reg.technocore = tc

    reg.register(
        "http_json_read",
        lambda url: http.get_json(url),
        {"name": "http_json_read", "description": "Reads allowed HTTP/JSON resource (SSRF protected)",
         "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    )
    reg.register(
        "github_repo_read",
        lambda repo: gh.repo_activity(repo),
        {"name": "github_repo_read", "description": "Reads public GitHub repo activity",
         "parameters": {"type": "object", "properties": {"repo": {"type": "string"}}, "required": ["repo"]}},
    )
    reg.register(
        "internal_health",
        lambda: health.check(),
        {"name": "internal_health", "description": "LUMI container health information",
         "parameters": {"type": "object", "properties": {}}},
    )
    async def _tc_read(room: str = "", since: int = 0):  # type: ignore[no-untyped-def]
        if tc is None:
            raise RuntimeError("Technocore disabled (set TECHNOCORE_ENABLED=true)")
        return await tc.read_room(room, since)

    async def _tc_write(room: str, payload: dict, idempotency_key: str = ""):  # type: ignore[no-untyped-def]
        if tc is None:
            raise RuntimeError("Technocore disabled (set TECHNOCORE_ENABLED=true)")
        return await tc.signed_post(room, payload, idempotency_key=idempotency_key)

    reg.register(
        "technocore_read",
        _tc_read,
        {"name": "technocore_read", "description": "Technocore room/event reader (optional, requires TECHNOCORE_ENABLED)",
         "parameters": {"type": "object", "properties": {"room": {"type": "string"}, "since": {"type": "integer"}}}},
    )
    reg.register(
        "technocore_signed_write",
        _tc_write,
        {"name": "technocore_signed_write", "description": "DID-signed Technocore publish (optional, approval required)",
         "parameters": {"type": "object", "properties": {
             "room": {"type": "string"}, "payload": {"type": "object"}, "idempotency_key": {"type": "string"}},
             "required": ["room", "payload"]}},
    )
    return reg