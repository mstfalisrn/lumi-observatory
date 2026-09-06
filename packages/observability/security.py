# LUMI — privacy/redaction helpers
# Secrets, token, authorization header, cookie, token pattern and runtime env values
# redacted before entering model/semantic memory.
# Phase 4: DLP hardened — private key, AWS, high entropy, env literal blocking
from __future__ import annotations

import os
import re

# ——— Static patterns ———
_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Authorization / Bearer / api-key
    (re.compile(r"(?i)(authorization|bearer|api[_-]?key)\s*[:=]\s*(bearer\s+)?(\S+)"), r"\1=<REDACTED>"),
    # Telegram bot token: <digits>:<hex>
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"), "<TG_TOKEN_REDACTED>"),
    # JWT
    (re.compile(r"\bey[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "<JWT_REDACTED>"),
    # Genel secret uzun token
    (re.compile(r"\b(sk|pk|ghp|gho)_[A-Za-z0-9]{20,}\b"), "<SECRET_REDACTED>"),
    # --set env / ENV=
    (re.compile(r"(?i)(TELEGRAM_BOT_TOKEN|LLM_API_KEY|JWT_SECRET|[A-Z_]*PASSWORD)=(\S+)"), r"\1=<REDACTED>"),
    # Private key
    (re.compile(r"-----BEGIN (?:RSA )?PRIVATE KEY-----"), "<PRIVATE_KEY_REDACTED>"),
    # AWS Access Key / Secret
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<AWS_KEY_REDACTED>"),
    (re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*(\S+)"), r"aws_secret_access_key=<REDACTED>"),
    # Generic high-entropy password assignment
    (re.compile(r"(?i)(password|passwd|pwd)\s*[:=]\s*['\"]?([^\s'\";]{8,})['\"]?"), r"\1=<REDACTED>"),
    # Database URL with password
    (re.compile(r"(?i)(postgresql|postgres|mysql|mongodb)(://[^:]+:)([^@]+)(@)"), r"\1\2<REDACTED>\4"),
]

# Harici girdilerden redakte edilecek genel regex'ler (hex/base64-ish)
_SECRET_VALUE_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b[0-9A-Fa-f]{32,}\b"),
    re.compile(r"\b[A-Za-z0-9+/]{32,}={0,2}\b"),
]

# Runtime env values are added here — only once
_loaded_env_secrets: set[str] = set()
# Which env keys are secret (narrow list — not fail-closed, reduces false positives)
_SECRET_KEY_HINTS = ("PASSWORD", "SECRET", "TOKEN", "API_KEY", "ENCRYPTION", "MASTER_KEY")
_PLACEHOLDERS = {"CHANGE_ME", "REPLACE_ME", "dev-only-change-me", "dev-webhook-secret", "dev-only-32-byte-master-key-0000000000", ""}


def _is_secret_key(key: str) -> bool:
    k = key.upper()
    return any(h in k for h in _SECRET_KEY_HINTS)


def load_secrets_from_env(environ: dict[str, str] | None = None) -> int:
    """Add current environment values to the redaction set. Return: number of new values added.

    - Only secret-hint keys and long (>=12) values that are not filesystem paths.
    - Placeholder/CHANGE_ME values are skipped.
    - The same value is not added twice (idempotent).
    - Thread-unsafe but idempotent; calling once at startup is sufficient.

    """
    env = environ if environ is not None else dict(os.environ)
    added = 0
    for k, v in env.items():
        if not v or not isinstance(v, str):
            continue
        if v in _PLACEHOLDERS:
            continue
        if len(v) < 12:
            continue
        if v.startswith("/"):
            continue
        if not _is_secret_key(k):
            continue
        # skip if it contains placeholder substring
        if "CHANGE_ME" in v or "REPLACE_ME" in v:
            continue
        if v in _loaded_env_secrets:
            continue
        # redact the value literally
        try:
            pat = re.compile(re.escape(v))
        except re.error:
            continue
        _loaded_env_secrets.add(v)
        _SECRET_VALUE_PATTERNS.append(pat)
        _PATTERNS.append((pat, "<ENV_REDACTED>"))
        added += 1
    return added


# On module import, load current env once (so runtime secrets are redacted immediately)
# If it fails, pass silently — environ can be mocked in tests
try:
    load_secrets_from_env()
except Exception:
    pass


def redact(text: str) -> str:
    """Masks obvious secret/token/header patterns."""
    if not text:
        return text
    out = text
    for pattern, repl in _PATTERNS:
        try:
            out = pattern.sub(repl, out)
        except Exception:
            continue
    # extra: high entropy patterns second pass
    for pat in _SECRET_VALUE_PATTERNS:
        try:
            # ENV_REDACTED already covers generic long hex/base64-like strings; retain this pattern for compatibility.
            # Note: only hex 32+ is not already in _PATTERNS, so apply here
            # Literal env values are already in _PATTERNS, don't repeat
            if pat.pattern.startswith("\\b[0-9A-Fa-f]") or pat.pattern.startswith("\\b[A-Za-z0-9+/]"):
                out = pat.sub("<SECRET_REDACTED>", out)
        except Exception:
            continue
    return out


def contains_secret(text: str) -> bool:
    """DLP: does text contain secret value? (quick check for block/flag)."""
    if not text:
        return False
    # if redacted version differs, there was a secret
    return redact(text) != text


def scrub_and_flag(text: str) -> tuple[str, bool]:
    """DLP helper: redact and return whether there was a secret."""
    if not text:
        return text, False
    scrubbed = redact(text)
    had_secret = scrubbed != text
    return scrubbed, had_secret


class Redactor:
    def __init__(self) -> None:
        self._extra: list[tuple[re.Pattern, str]] = []

    def add_literal(self, value: str) -> None:
        if value and len(value) >= 4:
            self._extra.append((re.compile(re.escape(value)), "<SECRET_REDACTED>"))

    def scrub(self, text: str) -> str:
        out = redact(text)
        for pat, repl in self._extra:
            out = pat.sub(repl, out)
        return out

    def contains_secret(self, text: str) -> bool:
        return contains_secret(text) or any(pat.search(text) for pat, _ in self._extra)

    def scrub_and_flag(self, text: str) -> tuple[str, bool]:
        scrubbed = self.scrub(text)
        return scrubbed, scrubbed != text
