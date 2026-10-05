# LUMI — test bootstrap: deterministic session-signing key.
#
# The suite must not depend on the developer's shell or a stray .env: tests
# that mint/decode session tokens need a non-empty JWT_SECRET, because PyJWT
# >= 2.15 raises InvalidKeyError on an empty HMAC key (older versions signed
# with "" silently). Pin a test-only value before any test module imports
# observability.config. setdefault keeps an explicit developer value intact.
import os

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-used-in-production-0123456789abcdef")
