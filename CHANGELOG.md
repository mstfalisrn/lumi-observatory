# Changelog
Follows [Keep a Changelog](https://keepachangelog.com/) and [Semantic Versioning](https://semver.org/).

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
