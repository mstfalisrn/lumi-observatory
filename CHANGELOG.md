# Changelog
Follows [Keep a Changelog](https://keepachangelog.com/) and [Semantic Versioning](https://semver.org/).

## [Unreleased]
### Added
- tclk exact-answer solver families (`apps/scheduler/tclk_solver.py`): `tip` (the single word the brief asks for), `validation` (verdict + the exact discrepancy), and `protocol` — a transcript fold that reports the final deal state plus any rejected frame, ported from the reference `@flop-labs/tclk` machine/frames/locks (`apps/scheduler/br_fold.py`), fail-closed and offline-testable
- Blockrewards worker (`apps/earn/blockrewards.py`): follows the judged-deal feed, caches offer frames from the board (backfill + live cursor), answers each brief deterministically, then runs the deal end to end — accept, heartbeat, delivery, reveal, receipt — with a retry queue, per-offer dedupe, and a deal-room lock watch; funded payout offers are watched on the same loop
- Logs dashboard: earnings are counted per rail — the headline is `flop-htlc` only, the paper (simulation) count sits beside it, so simulated deals can no longer read as money (`/saglik`: `kilitli_flop_htlc` / `kilitli_paper`)
- Bounded capability manifests (`skills/*.json` + `packages/agent_core/skills.py`): source-controlled skill definitions that constrain allowed tools and required operator scope; command center + API reject invalid or out-of-scope skill runs at submission time
- Planner preflight hardening: out-of-scope HTTP targets are rejected before execution (replaces generic worker failures with actionable `failure_code` values: `tool_error:<tool>:<type>`, `policy_denied:<tool>`, `wall_time_exceeded`, `verification_failed`)
- Proactive source observation (`packages/observability/source_monitor.py`): opt-in sources for `http_json` / `github_repo` / `internal_health` / `technocore_room` with content-hash change detection, error backoff, and bounded metadata-only observation records; manual scan + status API
- Report-only digest workflows (`packages/observability/digest_service.py`): deterministic local digest schedules aggregate stored changes and risk metadata into `Report` rows; external delivery disabled by default
- Trust Center UI tab and Sources management UI (add/enable/disable/scan/events), digest controls, capability manifest browser
- Configuration flags: `SOURCE_MONITOR_ENABLED`, `SOURCE_MEMORY_CANDIDATES_ENABLED`, `DIGEST_ENABLED`, `RISK_ALERTS_ENABLED` (all off by default) forwarded through docker-compose
- Production fail-closed SSRF validation on `POST /api/v1/settings/llm/test`

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
