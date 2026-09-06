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
def create_session_token(user_id: str, role: str, expires_seconds: int = 12 * 3600) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": user_id,
        "role": role,
        "iat": now,
        "exp": now + timedelta(seconds=expires_seconds),
        "jti": uuid.uuid4().hex,
        "iss": "lumi-observatory",
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")


def decode_session_token(token: str) -> dict:
    # signature + exp + iss are verified; invalid token raises
    return jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"], issuer="lumi-observatory")


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
    token = creds.credentials if creds and creds.credentials else None
    # SSE manual auth: Bearer-only; due to EventSource native header restriction
    # /api/v1/events/stream endpoint manually resolves the Authorization header (decode_session_token).
    # Cookie fallback intentionally absent — query ?token and cookie leakage/URL log risk
    # removed to prevent; web fetch sends Authorization. This dependency
    # no cookie fallback attempt is made for this.
    if not token:
        # no cookie fallback — SSE Bearer-only; this is a general dependency, there is a separate manual resolution for SSE
        pass
    if not token:
        raise HTTPException(401, "authentication required")
    try:
        payload = decode_session_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "session expired") from None
    except Exception:
        raise HTTPException(401, "invalid session token") from None
    return {"user_id": payload.get("sub"), "role": payload.get("role", "viewer")}


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
