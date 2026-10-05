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
                                        |
                    technocore.chat <-> tclk market agent (scheduler)
                                        +-> audit -> Jev -> solver -> deliver -> reveal
                                        |
                    Earning loops (host or container): trader / kibble / close1 / blockrewards
                                        |
                    lumi-logs: rail-aware earnings dashboard + /summary JSON
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

## Data Model — 34 Tables
`users`, `telegram_identities`, `telegram_updates`, `agent_profiles`, `agent_evaluations`, `tasks`, `runs`, `run_events` (append-only), `plans`, `tool_calls`, `action_executions`, `approvals`, `context_snapshots`, `context_segments`, `memory_items`, `memory_relations`, `sources`, `source_observations`, `digest_schedules`, `evidence_items`, `reports`, `publication_attempts`, `outbox_messages`, `technocore_cursors`, `technocore_nonces`, `prompt_versions`, `policy_versions`, `audit_events`, `llm_usage`, and the market tables: `tclk_frames`, `tclk_offer_audits`, `tclk_verdicts`, `program_scores`, `room_archives`.

Timestamps are stored as UTC in the database; the UI displays them in the configured local timezone.

## Queue / Worker
- Redis list `lumi:queue`. A worker claims a run, executes it via the coordinator, and persists results and events to the database.
- Each tool is executed at most once per iteration; the iteration count equals the tool count (D1 correction).

## Connectors (MVP)
`technocore_read`, `technocore_signed_write` (DID + approval-gated), `github_repo_read`, `http_json_read` (SSRF-protected), `internal_health`.

## Identity & Registration
`apps/tools/flop_register.py` bootstraps the agent identity: generates the Ed25519 key at `TECHNOCORE_KEY_HOST_PATH` (default `./secrets/did.ed25519`, 0600, never committed), derives `did:key:z6Mk...`, publishes the identity note at `/kv/did-<shard>/<key>` on technocore.chat, claims the devnet faucet drip at `/r/faucet`, and verifies both by reading them back. Signing lives in `packages/connectors/technocore.py` (`canonical_string`, `_pubkey_to_did`, monotonic nonces); the private key never leaves the key file. The wizard runs the same tool as Step 5 and writes `LUMI_AGENT_DID` / `LUMI_AGENT_NAME` / `TECHNOCORE_KEY_HOST_PATH` into `.env`.

## Market Agent (tclk)
The scheduler works the tclk/1 offer market over technocore.chat. Every offer runs through layered gates that can only tighten the decision (never widen it):

1. **Deterministic audit** — `packages/connectors/tclk.py::audit_offer`: frame kind/signature, rail intersection, payee-side refusal, amount cap, expiry, difficulty ceiling, capability keywords.
2. **Jev veto** — TypeSafe typed decision, fail-closed.
3. **Operator risk ceiling** — `TCLK_AGENT_MIN_TIER`.
4. **Brief resolution** — no resolvable brief, no accept (`TCLK_ACCEPT_REQUIRE_BRIEF`).
5. **Lane rate limit** — rolling hourly cap plus validation reserve.
6. **Concurrency** — `TCLK_AGENT_MAX_ACTIVE` deals in flight with TTL.
7. **Uniqueness** — one accept per `(author, nonce)`, ever.

Accepted deals run end to end: accept → escrow secret derived from the offer id (survives restarts) → heartbeat → exact answer → delivery → on lock, reveal and receipt. The deterministic solver (`apps/scheduler/tclk_solver.py` + `br_fold.py`) answers the exact-answer families — tip, protocol transcript fold, validation, math, `/kv` note, HTTP probe, documentation — and never guesses; the producer model (`tclk_producer.py`) covers the rest. Every decision is persisted to `tclk_offer_audits` with its checks and outcome. Rails are the money question: the default is `flop-htlc`; the judged-program profile (`flop-htlc,paper` + amount cap) serves the funded judged programs that pay in FLOP on the paper rail until the value escrow exists.

## Live Log (shared data layer)
`packages/observability/live_log.py` holds the read-only queries behind the live
log — LLM/Jev decisions (`agent_evaluations`, `tclk_offer_audits`), LLM token
spend (`llm_usage`), market flow, job completion and earnings frames. Three
consumers share it: the web UI's **Live Log** tab (`GET /api/v1/live/log`),
the machine contract `GET /api/v1/live/summary` (same shape as the standalone
`/summary`), and the optional standalone page in `apps/logs` (`/`, `/summary`,
`/raw`). SELECT only; it writes to no table.

## Earning Loops
Standalone loops in `apps/earn/`, runnable as host services or containers:

| Loop | Role |
|---|---|
| `trader.py` | flopmarket participation, coherence checks, news watch (`--post` to write) |
| `kibble.py` | Kibble JOB → CLAIM → RESULT loop |
| `close1.py` | close-1 position keeper |
| `blockrewards.py` | judged-deal worker (feed cursor, offer cache, retry queue, deal-room lock watch) |

The logs dashboard (`apps/logs`) renders rail-aware earnings and the `/summary` JSON: `locked_flop_htlc` is the headline, `locked_paper` sits beside it as the simulation it is.

## API Endpoints
- `GET /health/live` — liveness
- `GET /health/ready` — readiness (DB connectivity)
- `GET /api/v1/tasks`, `GET /api/v1/runs`, `GET /api/v1/runs/{id}/events`, `POST /api/v1/approvals`, `GET /api/v1/memory`, `GET /api/v1/sources`, `GET /api/v1/reports`, `GET /api/v1/technocore`, `GET /api/v1/settings/non-secret`, `GET /api/v1/events/stream`
- `POST /webhooks/telegram/<opaque>` — Telegram webhook (opaque path)

## SSE
`GET /api/v1/events/stream` publishes recent run events as Server-Sent Events. The gateway is configured with `flush_interval -1` to stream without buffering.
