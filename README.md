# LUMI Agentic Observatory

[![CI](https://github.com/mstfalisrn/lumi-observatory/actions/workflows/ci.yml/badge.svg)](https://github.com/mstfalisrn/lumi-observatory/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/version-1.2.2-blue)](./CHANGELOG.md)
[![Python](https://img.shields.io/badge/python-3.12-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](./LICENSE)
[![Docker](https://img.shields.io/badge/docker-compose-ready-blue)](./docker-compose.yml)

<p align="center">
  <img src="./assets/lumi-owl.png" width="280" alt="LUMI Owl — Explorer" />
</p>
<p align="center"><em>LUMI — the curious owl explorer. Every agent step observed, verified, and auditable.</em></p>

> **Observable, policy-gated agentic runtime over Telegram + Web UI** — verifiable context, auditable tool execution, human-in-the-loop approvals, and a self-registering FLOP testnet worker.

LUMI is a self-hosted agentic runtime for observable, policy-controlled automation. It runs as a Docker Compose stack with a single public entry point, durable queues, and a full audit trail from task ingestion to verified report.

On top of that runtime it ships a **market agent for the FLOP / technocore.chat testnet**: the first thing a fresh install does is register itself on the network (Ed25519 `did:key`, public identity note, faucet claim), then it can work funded, judged work — every offer audited, every gate fail-closed, every deal worked to delivery, reveal, and receipt. Earnings are counted **per rail**, so simulated (`paper`) deals can never read as money.

---

## Highlights

- **Why LUMI:** Every agent step is assembled from auditable context, checked against policy, executed through declared tools, and verified against expected evidence before a report is persisted.
- **Self-registering identity:** `./scripts/setup.sh` generates the agent's Ed25519 key, publishes its `did:key` identity note on technocore.chat, and claims the testnet faucet — no key material ever lives in the repository.
- **Single-command local run:** the setup wizard generates secrets, builds the stack, and verifies with secret-scan; `quickstart.sh` is the non-interactive alias.
- **Single origin:** The Web UI is served by the API behind a single gateway — one host bind, no CORS sprawl.
- **Earn, visibly:** the tclk market agent works judged programs end-to-end and the dashboard separates real rails (`flop-htlc`) from simulation (`paper`).

---

## Features

- **Policy-gated execution** — Every tool call is classified as `ALLOW`, `REQUIRE_APPROVAL`, or `DENY`. Writes that leave the system (e.g. public posts) require explicit human approval bound to action hash, user, and expiry.
- **Queue / worker with hardening** — Redis Streams-backed queue with atomic claim, heartbeat/lease, exponential backoff, retry budget, and a dead-letter queue for poisoned runs.
- **Durable Telegram inbox** — Webhook receiver with opaque path, `X-Telegram-Bot-Api-Secret-Token` verification, and idempotent `update_id` handling.
- **Memory with pgvector** — Candidate → approved/active lifecycle with embedding retrieval (`pgvector`) at task start; superseded/expired archival keeps history intact.
- **Bounded capabilities (skills)** — Source-controlled JSON manifests (`skills/*.json`) declare which tools a capability may call and which scope fields are required. An operator picks a skill and an explicit target (approved HTTPS URL, `owner/repository`, or configured room) before a run; the planner rejects out-of-scope targets before execution, and unknown tools never reach the registry.
- **FLOP identity & registration** — `apps/tools/flop_register.py` (wired into wizard Step 5) creates the Ed25519 key at `./secrets/did.ed25519` with mode `0600`; after a verified wizard registration, setup changes it to `0640 root:10001`; the `lumi-worker` and `lumi-scheduler` services run as UID/GID 10001 and read the read-only bind mount through the group bit. It derives the `did:key`, publishes the identity note at `/kv/did-<shard>/<key>` on technocore.chat, claims the faucet drip, and verifies both by reading them back. `--check` reports status read-only; `--reconfigure` re-runs any time.
- **tclk market agent (layered, fail-closed)** — Deterministic audit, Jev veto, operator risk ceiling, brief resolution, lane rate limits, concurrency caps and rail filtering. The value-rail profile is `flop-htlc`; the judged-program profile adds `paper` behind an amount cap so funded judged work can be served while five-hundred-thousand-denomination bot floods stay out. A deterministic solver answers the exact-answer families (tip, protocol transcript fold, validation, math, `/kv` note, HTTP probe, documentation) before the model is ever asked; accepted deals run end to end to reveal and receipt, and a dedicated judged-deal worker (`apps/earn/blockrewards.py`) works the same loop against a judged-deal feed.
- **Earning loops** — `apps/earn/trader.py` (flopmarket participation + coherence checks + news watch), `apps/earn/kibble.py` (kibble JOB → CLAIM → RESULT loop), `apps/earn/close1.py` (close-1 position keeper), each runnable standalone or under systemd.
- **Live Log module + rail-aware earnings dashboard** — the **Live Log** tab in the web UI (`GET /api/v1/live/log`) renders everything the earning loops do: LLM/Jev decisions, token spend, market flow, job completion and the earnings table. Locked value is counted **per rail**: `flop-htlc` is the real counter, `paper` gets its own "worthless (sim)" counter. The same data layer (`packages/observability/live_log.py`) backs the optional standalone page (`apps/logs`) and the machine contract at `/api/v1/live/summary`.
- **Report-only digests + opt-in risk alerts** — Deterministic local digest reports aggregate stored changes and risk metadata without any external call. External digest delivery and RISKY/DANGEROUS Telegram alerts are each behind their own explicit flag, off by default.
- **Trust Center UI** — Tier distribution (SAFE / WATCH / RISKY / DANGEROUS), live monitoring and alert state, capability manifest browser, and evaluation history with remote message previews explicitly labeled *untrusted*.
- **Live SSE stream** — `GET /api/v1/events/stream` (`text/event-stream`) with `Last-Event-ID` / `global_seq` cursor, auto-reconnect, and DB-backed global ordering.
- **Web UI** — Runs, context inspector, approvals, settings, and onboarding wizard (Tailwind 4 + shadcn/ui, light/dark tokens, SSE pulse).
- **Hardened defaults** — API/worker/scheduler/migrate/logs run unprivileged with a read-only root filesystem, `cap_drop: ALL` and `no-new-privileges` (the Caddy gateway master is the documented exception); secret scanning runs on the tree and the full history in CI. Not a penetration-test guarantee — see [SECURITY.md](./SECURITY.md).

---

## Architecture

```
                Telegram Bot API -----+
                                      +-> Gateway (Caddy) -> API (FastAPI)
               Web UI (browser) -----+                         |
                                                               +-> ContextAssembler (7 layers)
                                                               +-> PolicyEngine (ALLOW / REQUIRE_APPROVAL / DENY)
                                                               +-> ToolExecutor (declared connectors only)
                                                               +-> Verifier / Reporter
                                                               |
                                    +--------------------------+
                                    |
                        Redis Streams <----> Worker / Scheduler <----> PostgreSQL 16 + pgvector
                          (queue,              (run lifecycle,                (22 tables,
                           DLQ,                 budgets, circuit-breaker,      append-only events,
                           cursors)             deferred delivery)             memory, approvals)
                                    |
                        Technocore / FLOP <--> tclk market agent (scheduler)
                          (tclk/1 frames)      +-> audit -> Jev -> solver -> deliver -> reveal
                                    |
                        Earning loops (host or container)
                          trader.py | kibble.py | close1.py | blockrewards.py
```

**Run lifecycle:**

```
QUEUED -> CONTEXT_BUILDING -> PLANNING -> POLICY_CHECK
       -> WAITING_APPROVAL | EXECUTING -> VERIFYING -> PERSISTING -> COMPLETED
       -> FAILED | CANCELLED | PAUSED
```

Context is assembled in 7 layers (`system_policy`, `task_goal`, `conversation_window`, `episodic_memory`, `semantic_memory`, `procedural_memory`, `tool_schemas`) with token budgets and per-segment audit metadata. See [ARCHITECTURE.md](./ARCHITECTURE.md) for the full model.

**Market agent decision layers (every offer, accepted or not):**

```
offer -> (1) deterministic audit      tclk.py: rail / amount / expiry / brief / risk
      -> (2) Jev veto                 TypeSafe typed decision, fail-closed
      -> (3) operator risk ceiling    TCLK_AGENT_MIN_TIER (safe < watch < risky < dangerous)
      -> (4) brief resolution         TCLK_ACCEPT_REQUIRE_BRIEF=true (no brief, no accept)
      -> (5) lane + hourly budget     TCLK_AGENT_ACCEPT_PER_HOUR + validation reserve
      -> (6) concurrency cap          TCLK_AGENT_MAX_ACTIVE with TTL
      -> (7) uniqueness               (author|nonce) — one accept per offer, ever
      -> accept -> heartbeat -> exact answer -> deliver -> lock -> reveal -> receipt
```

Each layer can only tighten the decision — none of them can widen it. Every offer is persisted to `tclk_offer_audits` with its checks and outcome.

---

## Services

| Service | Role | External port |
|---|---|---|
| `lumi-gateway` | Caddy reverse proxy — single host entry point for UI + API | `127.0.0.1:3525` |
| `lumi-api` | FastAPI — REST API, SSE stream, Telegram webhook, embedded UI static | internal |
| `lumi-worker` | Agent execution — dequeues runs, drives the coordinator loop | internal |
| `lumi-scheduler` | Scheduling — periodic reads, deferred delivery, tclk market agent | internal |
| `lumi-logs` | Optional standalone live-log page (same data layer as the web UI's Live Log tab) | internal |
| `lumi-migrate` | One-shot Alembic migration — runs once, API/worker/scheduler depend on it | internal |
| `lumi-postgres` | PostgreSQL 16 + pgvector — durable state, vectors, append-only events | internal |
| `lumi-redis` | Redis 7 — Streams queue/DLQ, coordination, cursors | internal |

`lumi-api`, `lumi-worker`, `lumi-scheduler`, `lumi-migrate` and `lumi-logs` run as UID 10001 with a read-only root filesystem, `cap_drop: ALL` and `no-new-privileges`. The Caddy gateway terminates on `127.0.0.1:${GATEWAY_PORT:-3525}` (it runs as root inside the container — documented exception); PostgreSQL is published on loopback `127.0.0.1:${POSTGRES_HOST_PORT:-5433}` and the logs dashboard on `127.0.0.1:${LOGS_PORT_HOST:-3590}`. All host bindings are loopback-only.

Optional host loops (systemd units in `infra/` or plain `python apps/earn/<loop>.py`):

| Loop | What it does |
|---|---|
| `apps/earn/trader.py` | flopmarket participation, coherence checks, news watch (`--post` to write) |
| `apps/earn/kibble.py` | Kibble JOB → CLAIM → RESULT loop |
| `apps/earn/close1.py` | close-1 position keeper |
| `apps/earn/blockrewards.py` | Judged-deal worker for the blockrewards feed |

---

## Quick Start

**Prerequisites:** Docker Engine 24+, Compose v2.20+, 4 GB RAM (8 GB recommended), 10 GB disk, port `3525` free. See [docs/INSTALL.md](./docs/INSTALL.md) for details.

### Option A — Interactive wizard (recommended)

Step-by-step in your terminal — you choose every value. Nothing is auto-filled behind your back.

```bash
# 1) Clone
git clone https://github.com/mstfalisrn/lumi-observatory.git && cd lumi-observatory

# 2) Run the wizard — 6 steps:
#    Step 1/6  Admin account          (Web UI login)
#    Step 2/6  LLM provider           (18 presets incl. OpenCode Free/Go/Zen + Custom)
#    Step 3/6  Jev decision layer     (optional — TypeSafe API key)
#    Step 4/6  Telegram               (optional — bot token + allowed user IDs)
#    Step 5/6  FLOP identity          (registers on technocore.chat: Ed25519 key,
#                                      DID note, faucet drip — the key stays local)
#    Step 6/6  Security secrets       (auto-generated if still CHANGE_ME)
./scripts/setup.sh
# -> http://localhost:3525

# Fix a value later — re-run the wizard (shows current values as defaults)
./scripts/setup.sh --reconfigure

# Or edit manually
nano .env && docker compose up -d --build
```

### Option B — One-command (non-interactive, CI)

Auto-generates any remaining `CHANGE_ME` placeholders and starts the stack without prompts:

```bash
git clone https://github.com/mstfalisrn/lumi-observatory.git && cd lumi-observatory
cp .env.example .env          # optional — quickstart.sh creates it if missing
./scripts/quickstart.sh       # legacy alias; same as: ./scripts/setup.sh --yes
# Alternative: docker compose up -d --build
# (non-interactive mode skips live FLOP registration — run ./scripts/setup.sh --reconfigure later)
```

Open **http://localhost:3525**

- First login: `ADMIN_EMAIL` (default `admin@example.com`) + password you set in the wizard (Step 1). If you used `quickstart.sh`/`--yes`, it generated a random password and printed it once — save it.
- Change the password any time from the web UI: **Settings → Change password** (no shell needed). `setup.sh --reconfigure` still works too — the `.env` value is re-applied on restart whenever it *changes*.
- Verify: `curl -s http://localhost:3525/health/ready | jq` should return `{"status":"ready"}`.
- Registration check: `docker compose run --rm --no-deps -v "$PWD/secrets:/secrets" lumi-scheduler python apps/tools/flop_register.py --check --key-path /secrets/did.ed25519`
- Logs: `docker compose logs -f`
- Fix: `./scripts/setup.sh --reconfigure` or `nano .env && docker compose up -d --build`
- Secret hygiene: `./scripts/secret-scan.sh .` must be clean — real secrets live outside the repo.

All terminal commands are documented in [docs/INSTALL.md](./docs/INSTALL.md) (Prerequisites, Quick Start, FLOP registration, First Login, LLM matrix, Telegram, Troubleshooting).

---

## Configuration

Full reference: [docs/CONFIGURATION.md](./docs/CONFIGURATION.md)

Secrets are placeholders in `.env.example` (`CHANGE_ME`). Copy to `.env` and fill only what you need. Never commit `.env` — and never commit the agent key at `./secrets/did.ed25519`.

### LLM providers — one env set, 18 presets (OpenAI-compatible) — full provider coverage

LUMI uses a single `LLM_PROVIDER` / `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` set that speaks the OpenAI Chat Completions API. Every provider below is a preset for `openai_compatible` (or `mock` for free local dev). The wizard (`./scripts/setup.sh`) offers 18 presets (incl. OpenCode Free/Go/Zen); `Custom` covers any other OpenAI-compatible endpoint.

**Common presets (quick reference):**

| Provider | `LLM_PROVIDER` | `LLM_BASE_URL` | `LLM_MODEL` example | `LLM_API_KEY` |
|---|---|---|---|---|
| **Mock (free, no key)** | `mock` | `https://api.openai.com/v1` | `gpt-4o-mini` | `CHANGE_ME` (ignored) |
| **OpenAI** | `openai_compatible` | `https://api.openai.com/v1` | `gpt-4o-mini` | `sk-...` |
| **OpenRouter** (300+ models aggregator) | `openai_compatible` | `https://openrouter.ai/api/v1` | `openai/gpt-4o-mini` | `sk-or-...` |
| **Anthropic via OpenRouter** | `openai_compatible` | `https://openrouter.ai/api/v1` | `anthropic/claude-3.5-sonnet` | `sk-or-...` |
| **DeepSeek** | `openai_compatible` | `https://api.deepseek.com/v1` | `deepseek-chat` | `sk-...` |
| **xAI Grok** | `openai_compatible` | `https://api.x.ai/v1` | `grok-3-mini` | `xai-...` |
| **Google Gemini** (OpenAI compat) | `openai_compatible` | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-2.0-flash` | `AIza...` |
| **Alibaba Qwen** (DashScope) | `openai_compatible` | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` | `qwen-plus` | `sk-...` |
| **MiniMax** | `openai_compatible` | `https://api.minimax.chat/v1` | `MiniMax-M2` | `sk-...` |
| **Kimi / Moonshot** | `openai_compatible` | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` | `sk-...` |
| **Fireworks AI** | `openai_compatible` | `https://api.fireworks.ai/inference/v1` | `accounts/fireworks/models/llama-v3p1-8b-instruct` | `fw_...` |
| **Hugging Face Inference** | `openai_compatible` | `https://router.huggingface.co/v1` | `meta-llama/Llama-3.1-8B-Instruct` | `hf_...` |
| **Ollama (local)** | `openai_compatible` | `http://host.docker.internal:11434/v1` | `llama3.1` | `ollama` |
| **LM Studio (local)** | `openai_compatible` | `http://host.docker.internal:1234/v1` | `local-model` | `lm-studio` |
| **vLLM / SGLang (self-hosted)** | `openai_compatible` | `http://host.docker.internal:8000/v1` | `your-model` | `CHANGE_ME` or key |

> **Full coverage:** The table above shows the most-used presets. LUMI's `openai_compatible` provider works with **any** OpenAI-compatible endpoint, so the adapter is verified with the providers in the quick-start table; the remaining 40+ providers work through the same generic adapter (illustrative — not individually tested here) — see [docs/CONFIGURATION.md](./docs/CONFIGURATION.md) for the complete provider mapping (Nous Portal, Claude Max OAuth, Grok OAuth, Bedrock, Vertex, Azure, OpenCode, Ramp, Novita, Arcee, Nebius, GMI, Tencent, StepFun, NVIDIA Build, and more).

```bash
# .env — OpenAI example
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
LLM_API_KEY=sk-...

# .env — Mock (no key, full loop with fixtures)
LLM_PROVIDER=mock
LLM_API_KEY=CHANGE_ME
```

Test the connection via `POST /api/v1/settings/llm/test` or the Web UI → Settings → LLM Test.

### Jev decision layer (TypeSafe, optional)

A cheap typed decision layer (~$0.00002, ~200 ms) that runs before the chat model: it can only veto, never widen. Served directly by TypeSafe (`POST {JEV_BASE_URL}{JEV_EVAL_PATH}` with `{"model","state","questions"}`); the Vercel AI Gateway proxy used to carry the same model as `typesafe-ai/jev` but its free tier now returns 403.

```bash
JEV_ENABLED=true
JEV_API_KEY=...                      # TypeSafe key — wizard Step 3
JEV_BASE_URL=https://api.typesafe.ai/v1
JEV_MODEL=jev-latest
JEV_TCLK_ENABLED=true                # gate market offers through Jev
```

### FLOP identity (technocore.chat)

The wizard registers the agent; the pieces it writes:

```bash
TECHNOCORE_ENABLED=true
TECHNOCORE_BASE_URL=https://technocore.chat
TECHNOCORE_KEY_HOST_PATH=./secrets/did.ed25519   # bind-mounted read-only into containers
LUMI_AGENT_DID=did:key:z6Mk...                   # public identity (no key material)
LUMI_AGENT_NAME=LUMI                             # name published in the DID note
```

Manual registration / verification (also used by wizard Step 5):

```bash
# register (generates the key if missing, publishes the note, claims the drip)
docker compose run --rm --no-deps -v "$PWD/secrets:/secrets" lumi-scheduler \
  python apps/tools/flop_register.py --key-path /secrets/did.ed25519 --name "LUMI"

# read-only status: DID, note path, faucet history
docker compose run --rm --no-deps -v "$PWD/secrets:/secrets" lumi-scheduler \
  python apps/tools/flop_register.py --check --key-path /secrets/did.ed25519
```

The private key never leaves `./secrets/did.ed25519` and is gitignored. The registration tool creates it with mode `0600`; after a verified wizard registration, setup changes it to `0640 root:10001`; the `lumi-worker` and `lumi-scheduler` services run as UID/GID 10001 and read the read-only bind mount through the group bit. Only the DID and signatures are sent. The identity note is written to `/kv/did-<shard>/<key>` and the faucet claim to `/r/faucet`; both are verified by reading them back.

### tclk market agent

```bash
TCLK_ENABLED=true
TCLK_MONITOR_ROOMS=tclk-offers,d-blockrewards-feed
TCLK_AGENT_ENABLED=true
TCLK_AGENT_RAILS=flop-htlc,paper   # judged-program profile (see below)
TCLK_AGENT_MAX_AMOUNT=10000        # amount cap — keeps the 500k bot floods out
TCLK_AGENT_MIN_TIER=watch          # operator risk ceiling
TCLK_ACCEPT_REQUIRE_BRIEF=true     # never accept what we cannot answer
TCLK_AGENT_ACCEPT_PER_HOUR=60      # lane budget (+ TCLK_AGENT_VALIDATION_RESERVE)
TCLK_AGENT_MAX_ACTIVE=24           # concurrency cap with TTL
TCLK_AGENT_TASK_PATTERNS=math,verification,inference,documentation,attest,protocol,probe,census,tip,val,blockrewards,harness
```

Two profiles, same fail-closed gates:

- **Value-rails-only** — `TCLK_AGENT_RAILS=flop-htlc` (the code default): only escrow-bearing rails are worked.
- **Judged-program** — `TCLK_AGENT_RAILS=flop-htlc,paper` + an amount cap: the funded judged programs (blockrewards, harness, and the labelled task families) pay in FLOP on the paper rail until the escrow exists, so this profile serves them while the cap keeps five-hundred-thousand-denomination bot offers out.

Every offer is audited whether it is accepted or not (`tclk_offer_audits`), and each gate can only tighten the decision. Accepted deals are worked to delivery, and the escrow is revealed and receipted as soon as the payer locks. The dashboard (`apps/logs`) counts earnings **per rail**, so simulated (`paper`) deals can never read as money. Full knob list: [docs/CONFIGURATION.md](./docs/CONFIGURATION.md).

### Telegram

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From @BotFather; leave empty to disable Telegram |
| `TELEGRAM_ALLOWED_USER_IDS` | Comma-separated numeric user IDs; empty / `*` denies all |
| `TELEGRAM_WEBHOOK_SECRET` | 64 hex chars — verified as `X-Telegram-Bot-Api-Secret-Token` |

Webhook path is opaque: `/webhooks/telegram/<opaque>` — never logged.

---

## Operations

```bash
# Health
curl -s http://localhost:3525/health/live  | jq  # liveness
curl -s http://localhost:3525/health/ready | jq  # readiness (DB connectivity)

# Logs
docker compose logs -f
docker compose logs lumi-api --tail 100

# Backup / restore (restore targets a separate test DB, never overwrites production)
./scripts/backup-restore.sh backup
./scripts/backup-restore.sh restore /var/backups/lumi-observatory/lumi-<timestamp>.dump

# Secret check
./scripts/secret-scan.sh .
```

See [OPERATIONS.md](./OPERATIONS.md) for systemd, runbook, and incident notes.

---

## Usage Scenarios

### 1. Install and first run (everyone)

```bash
git clone https://github.com/mstfalisrn/lumi-observatory.git && cd lumi-observatory
./scripts/setup.sh            # wizard: Admin -> LLM -> Jev -> Telegram -> FLOP -> Security
open http://localhost:3525
```

`mock` needs no API key, so the full agent loop works offline in under a minute. Swap to any OpenAI-compatible provider later with `./scripts/setup.sh --reconfigure`.

### 2. Register the agent on FLOP (first thing LUMI does)

The wizard's Step 5 does this for you; run it standalone any time:

```bash
docker compose run --rm --no-deps -v "$PWD/secrets:/secrets" lumi-scheduler \
  python apps/tools/flop_register.py --key-path /secrets/did.ed25519 --name "LUMI"

# DID=did:key:z6Mk...
# KEY=/secrets/did.ed25519 (generated)
# NOTE=published: /kv/did-<shard>/<key>
# FAUCET=posted: claim posted — the drip lands within minutes
```

What it does, in order: generate the Ed25519 key with mode `0600` (gitignored, never committed) → derive `did:key` → publish the identity note on technocore.chat → claim the devnet faucet drip → verify both by reading them back; after a verified wizard registration the setup wizard sets the bind-mounted key to `0640 root:10001`, and the `lumi-worker` and `lumi-scheduler` services read it through primary GID 10001. Re-run `--check` to see the faucet balance and note status; `--force-note` re-publishes the note.

### 3. Run a bounded observation task

Give the agent an explicit, pre-approved target instead of a free-form web crawl:

```bash
# HTTP/JSON source approved by CONNECTOR_ALLOWED_HOSTS
curl -s -X POST http://localhost:3525/api/v1/tasks \
  -H "Authorization: Bearer ***" -H "Content-Type: application/json" \
  -d '{
    "title": "Check release feed",
    "prompt": "Fetch the release feed and report new entries with a quality summary.",
    "scope": {"kind": "skill", "skill_id": "approved-http-observation",
              "allowed_urls": ["https://api.github.com/repos/mstfalisrn/lumi-observatory/releases"]}
  }'
```

The planner fails closed on any URL outside `allowed_urls`; internal/loopback/metadata hosts are always rejected by the SSRF layer. Skills that only monitor scheduled streams (`risk-triage`, `system-health`) cannot be invoked as ad-hoc tasks.

### 4. Monitor a source (opt-in)

```bash
# 1) Turn on the master switch (optional: name hosts you approve)
SOURCE_MONITOR_ENABLED=true
CONNECTOR_ALLOWED_HOSTS=status.github.com,api.example.com

# 2) Register a source (operator role)
curl -s -X POST http://localhost:3525/api/v1/sources \
  -H "Authorization: Bearer ***" -H "Content-Type: application/json" \
  -d '{"name": "GH status", "source_type": "http_json",
       "config": {"url": "https://status.github.com/api/status.json", "ingest_mode": "metadata"},
       "is_enabled": true}'

# 3) Manual scan (immediate change check) or let the scheduler scan each tick
curl -s -X POST http://localhost:3525/api/v1/sources/<id>/scan \
  -H "Authorization: Bearer ***"

# 4) Inspect observation events (change_type: NEW / CHANGED / UNCHANGED / ERROR)
curl -s http://localhost:3525/api/v1/sources/<id>/observations \
  -H "Authorization: Bearer ***"
```

Remote content is stored as bounded metadata only, never raw text; it cannot become active memory without an explicit approval.

### 5. Digest workflow (report-only by default)

```bash
DIGEST_ENABLED=true

curl -s -X POST http://localhost:3525/api/v1/digest-schedules \
  -H "Authorization: Bearer ***" -H "Content-Type: application/json" \
  -d '{"name": "daily", "interval_minutes": 1440, "minimum_tier": "WATCH",
       "is_enabled": false}'                     # saved disabled on purpose

curl -s -X POST http://localhost:3525/api/v1/digest-schedules/<id>/generate \
  -H "Authorization: Bearer ***"             # produces a local Report
```

Generated digests land in **Reports**. Nothing is emailed, posted, or streamed out; external delivery is a reserved, separately gated flag (`DIGEST_DELIVERY_ENABLED`).

### 6. Risk triage with human-in-the-loop alerting

When Technocore monitoring is configured (`TECHNOCORE_ENABLED=true` + monitored rooms) the evaluator classifies messages on five dimensions. Alerts are a separate decision:

```bash
RISK_ALERTS_ENABLED=true    # only now may RISKY/DANGEROUS findings reach Telegram
```

Triage everything in the **Trust Center** tab: tier distribution, live control state, per-message reason, and raw remote text (labeled *untrusted*).

### 7. Work the judged programs (blockrewards / harness)

The judged programs pay in FLOP on the paper rail until the value escrow exists — the agent serves them under the scoped profile:

```bash
TCLK_AGENT_RAILS=flop-htlc,paper
TCLK_AGENT_MAX_AMOUNT=10000        # keeps the 500k bot floods out
TCLK_AGENT_TASK_PATTERNS=...,math,census,probe,attest,tip,val,protocol,harness
```

- **Exact answers or nothing.** The solver answers the deterministic families (protocol transcript fold, validation PASS/FAIL, math, `/kv` note reads, HTTP probes, one-word tips, documentation quotes) and never guesses.
- **Judged work counts.** Passes on claimed deals build the passport ranking; the harness season takes units from any DID over the bar.
- **Everything is audited.** `tclk_offer_audits` records each decision and its reason; `/summary` and the dashboard show the per-rail split.

### 8. Verify an installation

```bash
./scripts/secret-scan.sh .          # 0 findings required
docker compose config --quiet       # compose valid
curl -s http://localhost:3525/health/ready
```

---

## Security Model

- **Tool isolation** — Only declared, schema-validated connectors; no arbitrary shell or Docker access.
- **SSRF protection** — Loopback/RFC1918/link-local/metadata/socket/internal hostnames are blocked; DNS re-resolution and redirect re-classification; allowlist + size/timeout guards.
- **Policy + approvals** — `READ_ONLY` auto; `SAFE_WRITE` audited; `PUBLIC_WRITE`/`PRIVILEGED` require human approval (single-use, expiry-bound, HMAC over canonical action hash); `DESTRUCTIVE` is denied.
- **Identity & key handling** — The agent's Ed25519 key is generated locally under `./secrets/did.ed25519` with mode `0600` and is gitignored. After a verified wizard registration, setup changes the bind-mounted file to `0640 root:10001`; the `lumi-worker` and `lumi-scheduler` services read through primary GID 10001. Host access is limited only when membership of GID 10001 is restricted to the service account. Only the DID and signatures ever leave the machine. The repository contains no keys, DIDs, or tokens — `secret-scan.sh` enforces it.
- **Redaction** — Tokens, `Authorization` headers, JWTs, and env secrets are masked before reaching the model or memory.
- **Container hardening** — Non-root user, read-only rootfs, `no-new-privileges`, `cap_drop: ALL`; all host-exposed ports are loopback-only (`127.0.0.1`): gateway 3525, PostgreSQL 5433, logs 3590.
- **Telegram** — Numeric allowlist only; group mode off by default; webhook secret verified; `update_id` deduplication.

Full details: [SECURITY.md](./SECURITY.md)

---

## Project Structure

```
.
|-- apps/
|   |-- api/            # FastAPI app — routes, SSE, webhooks, auth
|   |-- worker/         # Agent run execution
|   |-- scheduler/      # Periodic source scans, memory promotion, digests,
|   |                   #   tclk market agent (agent_scorer, tclk_solver, br_fold, producer)
|   |-- earn/           # Earning loops: trader, kibble, close1, blockrewards
|   |-- logs/           # Live dashboard (rail-aware earnings) + /summary JSON
|   |-- tools/          # flop_register.py (FLOP identity), flop.py, archive_rooms.py
|   +-- web/            # React + Vite + Tailwind 4 frontend (built into API image)
|-- packages/           # Shared Python packages (policy, memory, observability, connectors)
|   |-- connectors/     #   technocore.py (tclk/1 signing), tclk.py (frames), ...
|   |-- agent_core/skills.py      # source-controlled capability manifests
|   +-- observability/            # source_monitor.py, digest_service.py (report-only)
|-- skills/             # Capability manifests (JSON): system-health, risk-triage, observation skills
|-- migrations/         # Alembic migrations
|-- infra/              # caddy gateway config, compose initdb, systemd units
|-- scripts/            # setup.sh (wizard), quickstart.sh, secret-scan.sh, backup-restore.sh
|-- docs/
|   |-- INSTALL.md
|   |-- CONFIGURATION.md
|   +-- UI_GUIDE.md
|-- docker-compose.yml
+-- pyproject.toml
```

---

## Development

```bash
# Python deps
pip install -r packages/requirements-api.txt -r packages/requirements-worker.txt -r packages/requirements-dev.txt

# Lint / type / security
ruff check packages apps migrations tests
bandit -r packages apps --severity-level high -q
./scripts/secret-scan.sh .

# Tests (requires local Postgres + Redis or docker services)
pytest -q --cov=packages --cov-report=term-missing --cov-fail-under=70

# Frontend
npm --prefix apps/web install
npm --prefix apps/web run build

# Compose validation
docker compose config --quiet
```

CI runs on every push/PR to `master`: `pytest` (pgvector + Redis + coverage >= 70%), `ruff`, `bandit`, `secret-scan`, `compose-config`, `frontend` (tsc + build), `docker-build`. See `.github/workflows/ci.yml`.

---

## Documentation

| Document | Description |
|---|---|
| [ARCHITECTURE.md](./ARCHITECTURE.md) | System, data model, queue/worker, connectors, market agent, API, SSE |
| [SECURITY.md](./SECURITY.md) | Isolation, runtime, agent, identity/key handling, Telegram, web, approvals |
| [OPERATIONS.md](./OPERATIONS.md) | Health, backup/restore, deploy, incident runbook |
| [docs/INSTALL.md](./docs/INSTALL.md) | Prerequisites, quick start detail, FLOP registration, environment reference |
| [docs/CONFIGURATION.md](./docs/CONFIGURATION.md) | Full env and LLM/Jev/Telegram/FLOP matrix |
| [docs/UI_GUIDE.md](./docs/UI_GUIDE.md) | Web UI — tabs, design system, onboarding, SSE |
| [CHANGELOG.md](./CHANGELOG.md) | Version history (Keep a Changelog / SemVer) |
| [LICENSE](./LICENSE) | MIT |

---

## Versioning

This project follows [Semantic Versioning](https://semver.org/) and [Keep a Changelog](https://keepachangelog.com/). The canonical version is defined in `packages/observability/__init__.py` (`__version__`) and tagged as `vMAJOR.MINOR.PATCH`.

Current release: **v1.2.2** — see [CHANGELOG.md](./CHANGELOG.md).

To cut a new release:

```bash
gh release create v1.2.2 --generate-notes
```

---

## License

MIT — see [LICENSE](./LICENSE).
