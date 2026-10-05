# LUMI — local authentication + RBAC + rate limiting
# No Cloudflare Access (by design). Instead:
#   - session JWT (HS256, signed with JWT_SECRET)
#   - PBKDF2-HMAC-SHA256 password hash (stdlib, dependency-free)
#   - role-based access (admin > operator > viewer)
from __future__ import annotations

import hashlib
import hmac
import os
import time
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from observability.config import settings

# ---------------------------------------------------------------------------
# Password hash (PBKDF2-HMAC-SHA256)
# ---------------------------------------------------------------------------
_ITERATIONS = 240_000

bearer_scheme = HTTPBearer(auto_error=False)

ROLE_ORDER = {"viewer": 0, "operator": 1, "admin": 2}


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _algo, iters, salt_hex, dk_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Session JWT
# ---------------------------------------------------------------------------
def create_session_token(
    user_id: str, role: str, expires_seconds: int = 12 * 3600, token_version: int = 0
) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": user_id,
        "role": role,
        "ver": int(token_version),
        "iat": now,
        "exp": now + timedelta(seconds=expires_seconds),
        "jti": uuid.uuid4().hex,
        "iss": "lumi-observatory",
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")


def decode_session_token(token: str) -> dict:
    # signature + exp + iss are verified; invalid token raises
    return jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"], issuer="lumi-observatory")


async def resolve_session(token: str) -> dict:
    """Decode a session token and verify it against LIVE account state.

    A signed JWT alone is not a session: the account must still exist and be
    active, and the token's `ver` must match the account's current
    `token_version` — so logout, password change, role edits and deactivation
    take effect on the next request instead of at token expiry. Raises 401.
    """
    try:
        payload = decode_session_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "session expired") from None
    except Exception:
        raise HTTPException(401, "invalid session token") from None
    return await _verify_live_session(payload)


async def _verify_live_session(payload: dict) -> dict:
    from observability.db import async_session_factory
    from observability.models import User

    sub = str(payload.get("sub") or "")
    try:
        user_uuid = uuid.UUID(sub)
    except (TypeError, ValueError):
        raise HTTPException(401, "invalid session token") from None
    async with async_session_factory() as s:
        user = (await s.execute(select(User).where(User.id == user_uuid))).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(401, "session revoked")
    if int(payload.get("ver", 0)) != int(user.token_version or 0):
        raise HTTPException(401, "session revoked")
    return {
        "user_id": str(user.id),
        "role": user.role,
        "username": user.username,
        "display_name": user.display_name,
    }


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------
def _resolve_user_id_from_email(email: str) -> str | None:
    # email → user id resolution: looks up username=email from users table.
    # This function requires async DB; called within a dependency.
    return None  # placeholder; resolved from DB in get_current_user


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict:
    # Bearer-only: no cookie fallback, no ?token query (URL/log leakage).
    # The SSE endpoint resolves the same Authorization header via resolve_session.
    token = creds.credentials if creds and creds.credentials else None
    if not token:
        raise HTTPException(401, "authentication required")
    return await resolve_session(token)


def require_role(min_role: str):
    async def _dep(user: dict = Depends(get_current_user)) -> dict:
        if ROLE_ORDER.get(user.get("role"), -1) < ROLE_ORDER.get(min_role, 0):
            raise HTTPException(403, f"insufficient role: {min_role} required")
        return user
    return _dep


# ---------------------------------------------------------------------------
# Rate limiting (Redis INCR + TTL, in-memory fallback)
# ---------------------------------------------------------------------------
class RateLimiter:
    def __init__(self) -> None:
        self._redis = None
        self._redis_tried = False
        self._mem: dict[str, list[float]] = {}

    async def _get_redis(self):
        if self._redis is None and not self._redis_tried:
            self._redis_tried = True
            try:
                import redis.asyncio as aioredis
                self._redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            except Exception:
                self._redis = None
        return self._redis

    async def check(self, key: str, limit: int, window_seconds: int) -> bool:
        """True = allowed, False = limit exceeded."""
        r = await self._get_redis()
        now = time.time()
        if r is not None:
            try:
                pipe = r.pipeline()
                pipe.incr(key)
                pipe.expire(key, window_seconds)
                count, _ = await pipe.execute()
                return int(count) <= limit
            except Exception:
                pass  # in-memory fallback when Redis is unavailable
        # in-memory fallback (sufficient for single process)
        ts = [t for t in self._mem.get(key, []) if now - t < window_seconds]
        if len(ts) >= limit:
            self._mem[key] = ts
            return False
        ts.append(now)
        self._mem[key] = ts
        return True


rate_limiter = RateLimiter()
