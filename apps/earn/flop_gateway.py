#!/usr/bin/env python3
"""FLOP inference gateway client — the compute-purchase leg of the spend lane.

``api.flop.network`` publishes an OpenAI-compatible data plane (version 2.0.0). Buying
compute there is the concrete form of a compute-channel spend: the gateway opens the
channel server-side, bills the account and returns the inference plus a receipt.

Controls:
  FLOP_GATEWAY_BASE      default https://api.flop.network (host must be allowlisted)
  FLOP_GATEWAY_KEY_FILE  default /root/secrets/lumi-observatory/flop-gateway.key
  FLOP_GATEWAY_KEY       inline key (wins over the file); never logged
  FLOP_GATEWAY_MODEL     default qwen/qwen2.5-0.5b-instruct
  FLOP_GATEWAY_TIMEOUT   default 60 seconds

Every failure carries a machine-readable reason. Nothing here prints or returns a key.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_BASE = "https://api.flop.network"
ALLOWED_HOSTS = frozenset({"api.flop.network"})
DEFAULT_KEY_FILE = "/root/secrets/lumi-observatory/flop-gateway.key"
DEFAULT_MODEL = "qwen/qwen2.5-0.5b-instruct"
PRICE_PER_1K_TOKENS = 0.006  # devnet list price, prompt and completion alike
USER_AGENT = "lumi-agent/1.0"


def _base() -> str:
    return os.environ.get("FLOP_GATEWAY_BASE", DEFAULT_BASE).rstrip("/")


def _host_ok(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    return host in ALLOWED_HOSTS


def load_key() -> str | None:
    inline = os.environ.get("FLOP_GATEWAY_KEY", "").strip()
    if inline:
        return inline
    path = Path(os.environ.get("FLOP_GATEWAY_KEY_FILE", DEFAULT_KEY_FILE))
    try:
        key = path.read_text().strip()
    except OSError:
        return None
    return key or None


def available() -> bool:
    return bool(load_key()) and _host_ok(_base())


def _request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    base = _base()
    if not _host_ok(base):
        return 0, {"error": "host_not_allowed", "message": f"{base} is not an allowlisted gateway"}
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    key = load_key()
    if key:
        headers["Authorization"] = "Bearer " + key
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, method=method, headers=headers)  # noqa: S310  # nosec B310 — allowlisted host
    timeout = float(os.environ.get("FLOP_GATEWAY_TIMEOUT", "60"))
    try:
        with urllib.request.urlopen(req, body, timeout=timeout) as resp:  # noqa: S310  # nosec B310
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw or "{}")
        except ValueError:
            return exc.code, {"error": "http_error", "message": raw[:400]}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, {"error": "transport_error", "message": str(exc)[:200]}


def classify(code: int, body: dict) -> str | None:
    """Map a gateway failure onto a reason the spend lane can act on."""
    if not code:
        return "transport_error"
    if code == 200:
        return None
    err = body.get("error")
    err_code = err.get("code") if isinstance(err, dict) else None
    message = ""
    if isinstance(err, dict):
        message = str(err.get("message", ""))
    elif isinstance(err, str):
        message = str(body.get("message", "")) or err
    lowered = message.lower()
    if "openchannel" in lowered or "open_channel" in lowered:
        return "channel_unfunded"
    if "not currently advertised" in lowered or err_code == "model_not_found":
        return "no_supply"
    if code in (401, 403):
        return "key_rejected"
    if "enrollment is temporarily unavailable" in lowered:
        return "enrollment_unavailable"
    if code == 429:
        return "rate_limited"
    if code >= 500:
        return "provider_error"
    return "request_rejected"


def cost_flop(prompt_tokens: int, completion_tokens: int) -> float:
    return round((prompt_tokens + completion_tokens) / 1000 * PRICE_PER_1K_TOKENS, 6)


def status() -> dict:
    """Gateway identity and quota. Read-only and cheap — safe before every spend."""
    if not load_key():
        return {"ok": False, "reason": "client_unavailable", "detail": "no gateway key"}
    code, body = _request("GET", "/v1/keys/me")
    reason = classify(code, body)
    if reason:
        return {"ok": False, "reason": reason, "http": code, "body": body}
    return {
        "ok": True,
        "credential": body.get("credential"),
        "remaining_quota": body.get("remaining_quota"),
        "expires_at": body.get("expires_at"),
        "model_allowlist": body.get("model_allowlist", []),
        "model": os.environ.get("FLOP_GATEWAY_MODEL", DEFAULT_MODEL),
    }


def complete(prompt: str, max_tokens: int = 16, model: str | None = None) -> dict:
    """One chat completion. Returns the text, the token usage and the billed cost."""
    if not prompt.strip():
        return {"ok": False, "reason": "empty_prompt"}
    target = model or os.environ.get("FLOP_GATEWAY_MODEL", DEFAULT_MODEL)
    payload = {
        "model": target,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max(1, int(max_tokens)),
    }
    code, body = _request("POST", "/v1/chat/completions", payload)
    reason = classify(code, body)
    if reason:
        return {"ok": False, "reason": reason, "http": code, "body": body}
    choices = body.get("choices") or []
    usage = body.get("usage") or {}
    text = ""
    if choices and isinstance(choices[0], dict):
        text = (choices[0].get("message") or {}).get("content", "") or ""
    return {
        "ok": True,
        "text": text,
        "usage": usage,
        "cost_flop": cost_flop(int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)),
        "request_id": body.get("id"),
        "model": body.get("model", target),
    }


def models() -> dict:
    """What FLOP currently advertises. Empty means no provider is serving right now."""
    code, body = _request("GET", "/v1/models")
    reason = classify(code, body)
    if reason:
        return {"ok": False, "reason": reason, "http": code, "body": body}
    items = body.get("data") if isinstance(body.get("data"), list) else []
    return {
        "ok": True,
        "count": len(items),
        "ids": [m.get("id") for m in items if isinstance(m, dict)],
    }


def receipts(limit: int = 20) -> dict:
    """Settled spend receipts for this key — the evidence a payable is real."""
    code, body = _request("GET", "/v1/keys/me/receipts")
    reason = classify(code, body)
    if reason:
        return {"ok": False, "reason": reason, "http": code, "body": body}
    items = body.get("data") if isinstance(body.get("data"), list) else body.get("receipts", [])
    items = items if isinstance(items, list) else []
    return {"ok": True, "count": len(items), "items": items[-limit:]}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="FLOP inference gateway client")
    parser.add_argument("--status", action="store_true", help="key, quota and allowlist")
    parser.add_argument("--ping", action="store_true", help="run a 4-token completion")
    parser.add_argument("--receipts", action="store_true", help="list settled receipts")
    args = parser.parse_args(argv)

    if args.status:
        print(json.dumps(status(), indent=2))
        return 0
    if args.ping:
        res = complete("Reply with the single word: ready", max_tokens=4)
        print(json.dumps(res, indent=2))
        return 0 if res.get("ok") else 3
    if args.receipts:
        res = receipts()
        print(json.dumps(res, indent=2))
        return 0 if res.get("ok") else 3

    parser.print_help()
    return 1


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
