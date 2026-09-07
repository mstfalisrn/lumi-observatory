# Changelog
Follows [Keep a Changelog](https://keepachangelog.com/) and [Semantic Versioning](https://semver.org/).

## [Unreleased]
### Added
- Bounded capability manifests (`skills/*.json` + `packages/agent_core/skills.py`): source-controlled skill definitions that constrain allowed tools and required operator scope; command center + API reject invalid or out-of-scope skill runs at submission time
- Planner preflight hardening: out-of-scope HTTP targets are rejected before execution (replaces generic worker failures with actionable `failure_code` values: `tool_error:<tool>:<type>`, `policy_denied:<tool>`, `wall_time_exceeded`, `verification_failed`)
- Proactive source observation (`packages/observability/source_monitor.py`): opt-in sources for `http_json` / `github_repo` / `internal_health` / `technocore_room` with content-hash change detection, error backoff, and bounded metadata-only observation records; manual scan + status API
- Report-only digest workflows (`packages/observability/digest_service.py`): deterministic local digest schedules aggregate stored changes and risk metadata into `Report` rows; external delivery disabled by default
- Trust Center UI tab and Sources management UI (add/enable/disable/scan/events), digest controls, capability manifest browser
- Configuration flags: `SOURCE_MONITOR_ENABLED`, `SOURCE_MEMORY_CANDIDATES_ENABLED`, `DIGEST_ENABLED`, `RISK_ALERTS_ENABLED` (all off by default) forwarded through docker-compose
- Production fail-closed SSRF validation on `POST /api/v1/settings/llm/test`

### Changed
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
