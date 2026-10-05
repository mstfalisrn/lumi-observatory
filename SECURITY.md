# Security Model — LUMI

This document describes what the code and the shipped deployment actually do.
It is a contract, not a marketing claim: where a control has a known exception,
the exception is written down here.

## Isolation

- Repository root is self-contained; no host-specific absolute paths are required to run.
- Secrets live outside the repository: a gitignored `.env` (0600) in the deploy directory, `./secrets/` (also gitignored), or `SECRETS_FILE` if pointed elsewhere. They are never committed and never baked into images; `scripts/secret-scan.sh` enforces this on the tree and on the full history (`--history`, also run in CI).
- No host root, Docker socket, or operator directory is mounted into LUMI containers. Builder/operator infrastructure is separate.
- The operator's Telegram token and model provider keys are not reused; LUMI uses its own bot and provider credentials.

## Runtime containers (what is actually hardened)

| Service | User | Root filesystem | Caps | Host port |
|---|---|---|---|---|
| `lumi-api`, `lumi-worker`, `lumi-scheduler`, `lumi-migrate` | 10001 | read-only (`tmpfs /tmp`) | `cap_drop: ALL` + `no-new-privileges` | none |
| `lumi-logs` | 10001 (image-level) | read-only (`tmpfs /tmp`) | `cap_drop: ALL` + `no-new-privileges` | loopback `127.0.0.1:3590` |
| `lumi-gateway` (nginx) | **root inside the container** (documented exception — the master binds :80 and writes its pid/cache) | writable | `cap_drop: ALL` + `no-new-privileges` | loopback `127.0.0.1:3525` |
| `lumi-postgres` | official image default | writable (data volume) | image defaults | loopback `127.0.0.1:5433` |
| `lumi-redis` | official image default | writable | image defaults | none |

- Every published host port is bound to **127.0.0.1 only**; the Cloudflare Tunnel reaches them from the same host.
- `lumi-logs` connects with the SELECT-only `lumi_reader` role when `LOGS_DATABASE_URL` is set (create it with `scripts/reader-role.sh`); its data routes require `LOGS_AUTH_TOKEN` once the service is published through a tunnel, and secret-like strings are redacted in the shared data layer before any page/JSON renders them.

## Authentication (web console)

- **Local authentication** — there is no Cloudflare Access dependency and no `Cf-Access-Jwt-Assertion` verification. The origin authenticates requests itself.
- Passwords: PBKDF2-HMAC-SHA256 (240k iterations). Sessions: HS256 JWTs signed with `JWT_SECRET`.
- The session token is stored in **`sessionStorage`** and sent as `Authorization: Bearer …`. It is not a cookie and not `HttpOnly`; XSS in the console would expose it — the console ships no third-party scripts, and untrusted content is rendered as text.
- **Revocation is real**: every token carries the account's `token_version`; logout, password change, env-driven password reset, role change and deactivation take effect on the next request, not at token expiry.
- **Rate limits**: global per-IP middleware limit plus a dedicated login limiter (per IP and per account). Request bodies are capped by actual received bytes, including chunked bodies without `Content-Length`.
- Failed logins are rate-limited, not audited per attempt; privileged actions (logout, password change, approvals) write audit events.

## Agent security

- **Tools:** only registered, schema-validated tools; no arbitrary shell or Docker execution.
- **SSRF:** scheme allowlist, hostname allowlist, IP-class blocking (loopback/RFC1918/link-local/metadata/multicast/reserved/CGNAT) applied after DNS resolution and after every redirect. The **validated IP is the address the socket actually connects to** (`PinnedNetworkBackend`); a second DNS answer cannot move the connection. Response size and time are capped.
- **Policy:** `READ_ONLY` auto-approved; `SAFE_WRITE` audited; `PUBLIC_WRITE` / `PRIVILEGED` / `DESTRUCTIVE` require single-use, time-bound human approval bound to an action hash. The Jev decision layer can only tighten a decision, never loosen it.
- **Redaction:** tokens, `Authorization` headers, JWTs and environment secrets are masked before reaching the model, memory, logs or API responses.
- **Untrusted content:** external messages are marked `UNTRUSTED` and are never executed or used as instructions.

## External writes (honest inventory)

- Agent-initiated external writes (posts, payments) go through the approval policy above.
- **Automation that writes without per-action approval, each behind its own opt-in flag, off by default:** setup-wizard registration publishes the public DID note and posts a faucet claim (Step 5 of the wizard); market/earning loops (`TCLK_*`, earn workers) place offers, accept deals and post frames on the external venue when explicitly enabled; source observation is read-only; digest delivery and risk alerts have separate flags. There is no blanket "nothing is ever automatic" guarantee — the flags above are the boundary.

## Identity & key material

- The agent's Ed25519 key is generated locally (`apps/tools/flop_register.py`), stored at `TECHNOCORE_KEY_HOST_PATH` (default `./secrets/did.ed25519`) as **0640 `root:10001`** — plain 0600 would lock the container user out of the bind mount. It is gitignored; the repository contains no keys, DIDs or tokens.
- The key file is bind-mounted **read-only** into the containers that sign frames; only the public DID and per-message signatures leave the machine.
- Registration publishes only public data (DID note, faucet claim) and reports a structured `OUTCOME=`; the wizard treats the run as successful only when the note is verified by reading it back.
- Market signing uses monotonic nonces per room; a replayed frame is rejected by the venue and never re-sent.

## Telegram

- Only numbers in `TELEGRAM_ALLOWED_USER_IDS` are authorized; wildcard/all is forbidden.
- Group chats are disabled by default; the webhook secret header is verified on every request; tokens are never logged.
- Updates are idempotent (`update_id`); approval callbacks are bound to user + action + hash + expiry.

## Testing — what was and was not done

- Unit/integration suites (330+ tests), ruff, bandit (high), TypeScript build and a history-aware secret scan run locally and in CI.
- The SSRF, policy, redaction, traversal, session-revocation, body-cap and verified-lock behaviors have explicit regression tests.
- **These are not penetration tests.** No external third-party pentest is claimed; coverage is partial (see CI artifacts for the current number).
- Found a problem? Open an issue (security-sensitive details: keep them out of public issues and the maintainers will provide a channel).
