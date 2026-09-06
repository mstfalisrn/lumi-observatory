# Architecture — LUMI Agentic Observatory

## Overview
LUMI is an observable, policy-gated agentic runtime that runs as an isolated Docker Compose stack. The builder/operator infrastructure is external and does not sit on the runtime data path.

```
Telegram Bot API ----\
                      > lumi-gateway -> lumi-api -> RunCoordinator
Web UI / Cloudflare -/                            |
                                                  +-> ContextAssembler
                                                  +-> PolicyEngine
                                                  +-> ToolExecutor (connectors)
                                                  +-> Verifier / Reporter
                            Redis <-> worker / scheduler <-> PostgreSQL + pgvector
```

## Agent Runtime (Task Lifecycle)
Task state machine (canonical):

```
QUEUED -> CONTEXT_BUILDING -> PLANNING -> POLICY_CHECK
       -> WAITING_APPROVAL | EXECUTING
       -> VERIFYING -> PERSISTING -> COMPLETED
       -> FAILED | CANCELLED | PAUSED
```

Components:

- **RunCoordinator** — state machine, budget / timeout / iteration limits, circuit breaker, kill switch.
- **Planner** — structured plan with expected evidence (template scoped to task).
- **ContextAssembler** — layered context assembly, token budget, auditable metadata.
- **PolicyEngine** — ALLOW / REQUIRE_APPROVAL / DENY.
- **ToolExecutor** — registered and schema-validated tools only; no arbitrary shell or Docker execution.
- **Verifier** — evidence and acceptance-criteria checks.
- **MemoryService** — lifecycle: candidate -> approved/active -> superseded/expired.
- **Reporter** — human-readable summary plus machine-readable evidence package.

## Context Layers (Context Inspector)
Ordered layers:

1. system_policy 2. task_goal 3. conversation_window 4. episodic_memory
5. semantic_memory 6. procedural_memory 7. tool_schemas (+ output reserve)

Each segment carries: `segment_type`, `source_id`, `title`, `token_count`, `relevance`, `freshness`, `confidence`, `included_reason`, `contains_untrusted`, `redaction_count`.

## Data Model — 22 Tables
`users`, `telegram_identities`, `agent_profiles`, `tasks`, `runs`, `run_events` (append-only), `plans`, `tool_calls`, `approvals`, `context_snapshots`, `context_segments`, `memory_items`, `memory_relations`, `sources`, `source_observations`, `evidence_items`, `reports`, `publication_attempts`, `technocore_cursors`, `prompt_versions`, `policy_versions`, `audit_events`.

Timestamps are stored as UTC in the database; the UI displays them in the configured local timezone.

## Queue / Worker
- Redis list `lumi:queue`. A worker claims a run, executes it via the coordinator, and persists results and events to the database.
- Each tool is executed at most once per iteration; the iteration count equals the tool count (D1 correction).

## Connectors (MVP)
`technocore_read`, `technocore_signed_write` (DID + approval-gated), `github_repo_read`, `http_json_read` (SSRF-protected), `internal_health`.

## API Endpoints
- `GET /health/live` — liveness
- `GET /health/ready` — readiness (DB connectivity)
- `GET /api/v1/tasks`, `GET /api/v1/runs`, `GET /api/v1/runs/{id}/events`, `POST /api/v1/approvals`, `GET /api/v1/memory`, `GET /api/v1/sources`, `GET /api/v1/reports`, `GET /api/v1/technocore`, `GET /api/v1/settings/non-secret`, `GET /api/v1/events/stream`
- `POST /webhooks/telegram/<opaque>` — Telegram webhook (opaque path)

## SSE
`GET /api/v1/events/stream` publishes recent run events as Server-Sent Events. The gateway is configured with `flush_interval -1` to stream without buffering.
