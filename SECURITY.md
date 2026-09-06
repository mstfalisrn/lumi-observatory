# Security Model — LUMI

## Isolation (Immutable)
- Repository root is self-contained; no host-specific absolute paths are required to run.
- Secrets live in an external secrets store outside the repository (not committed, 0600 permissions). Never copy them into the repo or images.
- External builder/operator infrastructure (containers, volumes, configs, skills, control plane) is **isolated** and not mounted into LUMI containers.
- The operator's Telegram token and model provider keys are not reused by LUMI; LUMI uses a separate bot and provider credentials.
- No host root, Docker socket, or operator directory is mounted into LUMI containers.
- The LUMI runtime keeps running even if the operator control plane is stopped (separate Docker stack and process supervision).

## Runtime Security
- All services run as non-root (UID 10001), with read-only root filesystem, `cap_drop: ALL`, and `no-new-privileges:true`.
- PostgreSQL and Redis do not expose host ports (internal Docker network only).
- Only `lumi-gateway` binds to the host, on `127.0.0.1:3525`.

## Agent Security
- **Tools:** only registered and schema-validated tools; arbitrary shell or Docker execution is not allowed.
- **SSRF:** loopback / RFC1918 / link-local / metadata / socket / internal hostname blocked; DNS resolution plus IP re-classification after every redirect; URL allowlist with size and timeout limits.
- **Policy:** `READ_ONLY` auto-approved; `SAFE_WRITE` audited; `PUBLIC_WRITE` / `PRIVILEGED` / `DESTRUCTIVE` require human approval. Approvals are single-use, time-bound, and bound to an action hash.
- **Redaction:** tokens, `Authorization` headers, JWTs, and environment secrets are masked before reaching the model or memory.
- **Untrusted content:** messages from external sources (e.g., Technocore) are always marked `UNTRUSTED` — they cannot inject commands or trigger tools.

## Telegram
- Only `TELEGRAM_ALLOWED_USER_IDS` (numeric allowlist) is authorized; wildcard / allow-all is forbidden.
- Group chats are disabled by default. The webhook secret token is verified on every request. Tokens are never logged.
- Idempotent handling via `update_id`; approval callbacks use a token bound to user + action + hash + expiry.

## Web
- In production, the origin sits behind Cloudflare Access; the origin verifies the `Cf-Access-Jwt-Assertion` header.
- Secure / HttpOnly / SameSite cookies, Content Security Policy, rate limiting, and login auditing.
- No auth tokens or secrets are written to `localStorage`.

## Approval Flow (Public Publishing)
Writes to external public networks (e.g., Technocore) are only performed after the user explicitly confirms with `PUBLIC-POST-APPROVED`, signed with a DID key.

## Penetration Tests (Verified)
- SSRF unit tests (loopback / RFC1918 / metadata / redirect)
- Policy tests: public write / privileged require approval; destructive is denied
- Redaction unit tests (Bearer / Telegram token / JWT)
- `secret-scan.sh` repository scan is clean
- Backup and restore verified without touching production data
- Port scan: only `127.0.0.1:3525` is exposed on the host
