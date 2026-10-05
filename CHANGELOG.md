# Changelog
Follows [Keep a Changelog](https://keepachangelog.com/) and [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.2.0] - 2026-10-05
### Security
- Earnings semantics are explicit: the summary keys are `locks_flop_htlc` / `lock_contracts_flop_htlc` / `locks_other_rails` with a frame-count note — the UI and the standalone page label them as observations, never balances
- External-audit follow-up: `/assets/{path}` containment (G01), dedicated login rate limiter (G02), verified-lock-gated reveal in the blockrewards worker (G03), dependency upgrades driven by `pip-audit` (G04: Starlette/`python-multipart`/cryptography/PyJWT/PyNaCl/orjson/python-dotenv/pytest fixed lines), the SSRF-validated IP is now the actual TCP destination (G05), session revocation via `users.token_version` with logout/password-change/deactivation taking effect immediately (G06), a byte-cap middleware that covers chunked bodies without Content-Length (G07), token-gated + redacting + least-privilege logs dashboard on a read-only non-root container (G08), and a history-aware secret scanner with token-level filtering, archive extraction and fail-closed reads (G09)
- Restart recovery: the blockrewards worker persists non-secret pending records and re-derives the claim preimage, so a delivered deal survives a restart and still reveals when the lock arrives (F01)
- Registration reports a structured `OUTCOME=` and only claims success on a verified identity note; the wizard keys off the exit code, not the presence of a `DID=` line (F03)
- `secret-scan.sh --history` walks every blob of every ref; CI runs it on a full clone
- `.dockerignore` blocks env backups/variants exactly like `.gitignore`

### Added
- tclk exact-answer solver families (`apps/scheduler/tclk_solver.py`): `tip` (the single word the brief asks for), `validation` (verdict + the exact discrepancy), and `protocol` — a transcript fold that reports the final deal state plus any rejected frame, ported from the reference `@flop-labs/tclk` machine/frames/locks (`apps/scheduler/br_fold.py`), fail-closed and offline-testable
- Blockrewards worker (`apps/earn/blockrewards.py`): follows the judged-deal feed, caches offer frames from the board (backfill + live cursor), answers each brief deterministically, then runs the deal end to end — accept, heartbeat, delivery, reveal, receipt — with a retry queue, per-offer dedupe, and a deal-room lock watch; funded payout offers are watched on the same loop
- Live dashboard: earnings are counted per rail — the headline is `flop-htlc` only, the paper (simulation) count sits beside it, so simulated deals can no longer read as money (`/summary`: `locked_flop_htlc` / `locked_paper`)
- Bounded capability manifests (`skills/*.json` + `packages/agent_core/skills.py`): source-controlled skill definitions that constrain allowed tools and required operator scope; command center + API reject invalid or out-of-scope skill runs at submission time
- Planner preflight hardening: out-of-scope HTTP targets are rejected before execution (replaces generic worker failures with actionable `failure_code` values: `tool_error:<tool>:<type>`, `policy_denied:<tool>`, `wall_time_exceeded`, `verification_failed`)
- Proactive source observation (`packages/observability/source_monitor.py`): opt-in sources for `http_json` / `github_repo` / `internal_health` / `technocore_room` with content-hash change detection, error backoff, and bounded metadata-only observation records; manual scan + status API
- Report-only digest workflows (`packages/observability/digest_service.py`): deterministic local digest schedules aggregate stored changes and risk metadata into `Report` rows; external delivery disabled by default
- Trust Center UI tab and Sources management UI (add/enable/disable/scan/events), digest controls, capability manifest browser
- Configuration flags: `SOURCE_MONITOR_ENABLED`, `SOURCE_MEMORY_CANDIDATES_ENABLED`, `DIGEST_ENABLED`, `RISK_ALERTS_ENABLED` (all off by default) forwarded through docker-compose
- Production fail-closed SSRF validation on `POST /api/v1/settings/llm/test`

- FLOP identity bootstrap (`apps/tools/flop_register.py`): generates the Ed25519 key (0600, never committed), derives `did:key`, publishes the identity note at `/kv/did-<shard>/<key>`, claims the faucet drip, and verifies both by reading them back — `--check` (read-only), `--force-note`, `--no-faucet`; wired into the setup wizard as Step 5
- Setup wizard now walks six steps: Admin -> LLM (18 presets) -> Jev (TypeSafe, optional) -> Telegram (optional) -> FLOP registration -> Security secrets; the scheduler image ships `apps/tools/` so registration runs in a one-off container
- `LUMI_AGENT_DID` / `LUMI_AGENT_NAME` / `TECHNOCORE_KEY_HOST_PATH` env knobs; the program watch reads the DID from `.env` and skips subject-scoped feeds when it is unset (no hardcoded identities anywhere in the repo — tests use synthetic DIDs)

- Live Log module in the web UI: the standalone `apps/logs` page (summary cards, LLM/Jev logs, token spend, market flow, job completion, earnings) now renders as a **Live Log** tab, auto-refreshing every 20 s, backed by `GET /api/v1/live/log` + `GET /api/v1/live/summary`
- `packages/observability/live_log.py`: single read-only data layer shared by the web UI module, the API endpoints and the standalone page (SELECT only)
- Web UI: fixed the Trust Center/Command Center crash — `/api/v1/skills` returns `{skills: [...]}` and the UI treated it as a bare array (`O.find is not a function` blanked the whole app)

- Web UI password change (**Settings → Change password** → `POST /api/v1/auth/change-password`): verifies the current password, requires 8+ characters, rotates the hash and writes an audit event; a password set from the UI persists across restarts
- `app_state` key/value table (migration `e7a1b2c3d4e5`): `ADMIN_PASSWORD_HASH` is applied from the environment when it CHANGES (first boot, `setup.sh --reconfigure`) instead of on every boot, so a UI-set password is never silently reverted
- Fixed: `setup.sh` now escapes `$` as `$$` when writing `.env` values — a raw pbkdf2 hash was being mangled by docker-compose interpolation, so a password typed in the wizard could never log in

- Security (external audit follow-up): `/assets/{path}` is now containment-checked (resolved path must stay inside the assets root — blocks `../` and symlink escapes, G01); `/auth/login` carries its own per-IP + per-account rate limiter (G02); the blockrewards worker only reveals on a venue-verified lock — signed lane + claimable rail + ref bound to the deal + expected payer (G03)
- `setup.sh`: admin password reaches Python via the environment, never interpolated into source (a password with quotes or `$` can no longer break the wizard, F02)
- Removed personal/mail tooling from the public tree (`send_docx_mail.py`, `make_summary_docx.py`, personal summary DOCX) and genericized hardcoded server paths in earn workers + systemd samples

### Changed
- Jev is served from TypeSafe directly (`JEV_BASE_URL=https://api.typesafe.ai/v1`, `JEV_EVAL_PATH=/systemone`, `JEV_MODEL=jev-latest`); the `boolean` question type is normalized to `noul` (the API rejects `boolean` with 400) and per-call cost is estimated from token counts
- tclk market work is gated to value-bearing rails: `TCLK_AGENT_RAILS` now ships as `flop-htlc` only, so `paper` (simulation) and `x402` offers are skipped at the audit gate — paper deals cannot consume accept quota or concurrency slots
- Migration `d5e6f7a8b9c0` adds `source_observations`, `digest_schedules` and source lifecycle columns (`down_revision: a9c8d7e6f5b4`)
- Worker tool-result summaries are redacted before persistence

## [1.0.0] - 2026-08-31
### Added
- Agentic loop (Planner → Coordinator → ToolExecutor → Verifier → Reporter), budgets, circuit breaker
- Policy engine (ALLOW / REQUIRE_APPROVAL / DENY), approval flow with HMAC + expiry
- Queue/worker (Redis Streams), atomic claim, retry + DLQ, scheduler deferred delivery
- Telegram durable inbox (webhook + idempotent update_id)
- Memory (candidate → approved/active) with pgvector embeddings
- SSE live stream (/api/v1/events/stream) with global cursor
- Web UI (runs, context inspector, approvals, settings) — Tailwind 4 + shadcn
- Technocore signed write (DID) gated by approval
- Docker Compose stack (gateway/api/worker/scheduler/postgres+redis), non-root/read-only
- CI gates: pytest ≥70%, ruff, bandit, secret-scan, compose validate
