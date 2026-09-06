# LUMI Agentic Observatory

[![CI](https://github.com/your-owner/lumi-observatory/actions/workflows/ci.yml/badge.svg)](https://github.com/your-owner/lumi-observatory/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/version-1.0.0-blue)](./CHANGELOG.md)
[![Python](https://img.shields.io/badge/python-3.12-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](./LICENSE)
[![Docker](https://img.shields.io/badge/docker-compose-ready-blue)](./docker-compose.yml)

<p align="center">
  <img src="./assets/lumi-owl.png" width="280" alt="LUMI Owl — Explorer" />
</p>
<p align="center"><em>LUMI — the curious owl explorer. Every agent step observed, verified, and auditable.</em></p>

> **Observable, policy-gated agentic runtime over Telegram + Web UI** -- verifiable context, auditable tool execution, and human-in-the-loop approvals.

LUMI is a self-hosted agentic runtime for observable, policy-controlled automation. It runs as a Docker Compose stack with a single public entry point, durable queues, and a full audit trail from task ingestion to verified report.

---

## Highlights

- **Why LUMI:** Every agent step is assembled from auditable context, checked against policy, executed through declared tools, and verified against expected evidence before a report is persisted.
- **Single-command local run:** `quickstart.sh` generates secrets, runs migrations, and starts the stack idempotently.
- **Single origin:** The Web UI is served by the API behind a single gateway -- one host bind, no CORS sprawl.

---

## Features

- **Policy-gated execution** -- Every tool call is classified as `ALLOW`, `REQUIRE_APPROVAL`, or `DENY`. Writes that leave the system (e.g. public posts) require explicit human approval bound to action hash, user, and expiry.
- **Queue / worker with hardening** -- Redis Streams-backed queue with atomic claim, heartbeat/lease, exponential backoff, retry budget, and a dead-letter queue for poisoned runs.
- **Durable Telegram inbox** -- Webhook receiver with opaque path, `X-Telegram-Bot-Api-Secret-Token` verification, and idempotent `update_id` handling.
- **Memory with pgvector** -- Candidate -> approved/active lifecycle with embedding retrieval (`pgvector`) at task start; superseded/expired archival keeps history intact.
- **Live SSE stream** -- `GET /api/v1/events/stream` (`text/event-stream`) with `Last-Event-ID` / `global_seq` cursor, auto-reconnect, and DB-backed global ordering.
- **Web UI** -- Runs, context inspector, approvals, settings, and onboarding wizard (Tailwind 4 + shadcn/ui, light/dark tokens, SSE pulse).
- **Production-ready hygiene** -- Non-root, read-only containers, `cap_drop: ALL`, isolated networks, secret scanning, and CI gates.

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
```

**Run lifecycle:**

```
QUEUED -> CONTEXT_BUILDING -> PLANNING -> POLICY_CHECK
       -> WAITING_APPROVAL | EXECUTING -> VERIFYING -> PERSISTING -> COMPLETED
       -> FAILED | CANCELLED | PAUSED
```

Context is assembled in 7 layers (`system_policy`, `task_goal`, `conversation_window`, `episodic_memory`, `semantic_memory`, `procedural_memory`, `tool_schemas`) with token budgets and per-segment audit metadata. See [ARCHITECTURE.md](./ARCHITECTURE.md) for the full model.

---

## Services

| Service | Role | External port |
|---|---|---|
| `lumi-gateway` | Caddy reverse proxy -- single host entry point for UI + API | `127.0.0.1:3525` |
| `lumi-api` | FastAPI -- REST API, SSE stream, Telegram webhook, embedded UI static | internal |
| `lumi-worker` | Agent execution -- dequeues runs, drives the coordinator loop | internal |
| `lumi-scheduler` | Scheduling -- periodic reads, deferred delivery (`not_before`), maintenance | internal |
| `lumi-migrate` | One-shot Alembic migration -- runs once, API/worker/scheduler depend on it | internal |
| `lumi-postgres` | PostgreSQL 16 + pgvector -- durable state, vectors, append-only events | internal |
| `lumi-redis` | Redis 7 -- Streams queue/DLQ, coordination, cursors | internal |

All runtime containers run as non-root, read-only filesystem, `cap_drop: ALL`, `no-new-privileges`. Only `lumi-gateway` is bound to the host (`127.0.0.1:${GATEWAY_PORT:-3525}` — override with `GATEWAY_PORT` env).

---

## Quick Start (60 seconds)

**Prerequisites:** Docker Engine 24+, Compose v2.20+, 4 GB RAM (8 GB recommended), 10 GB disk, port `3525` free. See [docs/INSTALL.md](./docs/INSTALL.md) for details.

### Option A — Interactive wizard (recommended)

Step-by-step in your terminal — you choose every value. Nothing is auto-filled behind your back.

```bash
# 1) Clone
git clone https://github.com/your-owner/lumi-observatory.git && cd lumi-observatory

# 2) Run the wizard — walks you through Admin -> LLM -> Telegram -> Security
./scripts/setup.sh
# The wizard: shows a checkbox menu (whiptail radiolist — SPACE to select, arrows to move).
# Pick LLM from 18 presets (Mock / OpenAI / OpenRouter / DeepSeek / Grok / Gemini / Qwen /
#  MiniMax / Kimi / Fireworks / HuggingFace / Ollama / LM Studio / vLLM /
#  OpenCode Free (keyless) / OpenCode Go / OpenCode Zen / Custom)
# and prompts for the matching API key/URL/model, asks for Telegram token (optional),
# auto-generates JWT/DB secrets if still CHANGE_ME, shows a masked summary, then starts the stack.
# -> http://localhost:3525

# Fix a value later — re-run the wizard (shows current values as defaults)
./scripts/setup.sh --reconfigure

# Or edit manually
nano .env && docker compose up -d --build
```

### Option B — One-command (non-interactive, CI)

Auto-generates any remaining `CHANGE_ME` placeholders and starts the stack without prompts:

```bash
git clone https://github.com/your-owner/lumi-observatory.git && cd lumi-observatory
cp .env.example .env          # optional — quickstart.sh creates it if missing
./scripts/quickstart.sh       # legacy alias; same as: ./scripts/setup.sh --yes
# Alternative: docker compose up -d --build
```

Open **http://localhost:3525**

- First login: `ADMIN_EMAIL` (default `admin@example.com`) + password you set in the wizard (Step 1). If you used `quickstart.sh`/`--yes`, it generated a random password and printed it once — save it.
- Verify: `curl -s http://localhost:3525/health/ready | jq` should return `{"status":"ready"}`.
- Logs: `docker compose logs -f`
- Fix: `./scripts/setup.sh --reconfigure` or `nano .env && docker compose up -d --build`
- Secret hygiene: `./scripts/secret-scan.sh .` must be clean -- real secrets live outside the repo.

All terminal commands are documented in [docs/INSTALL.md](./docs/INSTALL.md) (Prerequisites, Quick Start, First Login, LLM matrix, Telegram, Troubleshooting).

---

## Configuration

Full reference: [docs/CONFIGURATION.md](./docs/CONFIGURATION.md)

Secrets are placeholders in `.env.example` (`CHANGE_ME`). Copy to `.env` and fill only what you need. Never commit `.env`.

### LLM providers -- one env set, 18 presets (OpenAI-compatible) -- full provider coverage

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

> **Full coverage:** The table above shows the most-used presets. LUMI's `openai_compatible` provider works with **any** OpenAI-compatible endpoint, so all 40+ providers are supported -- see [docs/CONFIGURATION.md](./docs/CONFIGURATION.md) for the complete provider mapping (Nous Portal, Claude Max OAuth, Grok OAuth, Bedrock, Vertex, Azure, OpenCode, Ramp, Novita, Arcee, Nebius, GMI, Tencent, StepFun, NVIDIA Build, and more).

```bash
# .env -- OpenAI example
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
LLM_API_KEY=sk-...

# .env -- DeepSeek example
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
LLM_API_KEY=sk-...

# .env -- OpenRouter (Anthropic via OpenRouter)
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_MODEL=anthropic/claude-3.5-sonnet
LLM_API_KEY=sk-or-...

# .env -- Ollama local
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://host.docker.internal:11434/v1
LLM_MODEL=llama3.1
LLM_API_KEY=ollama

# .env -- Mock (no key, full loop with fixtures)
LLM_PROVIDER=mock
LLM_API_KEY=CHANGE_ME
```

Test the connection via `POST /api/v1/settings/llm/test` or the Web UI -> Settings -> LLM Test.

### Telegram

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From @BotFather; leave empty to disable Telegram |
| `TELEGRAM_ALLOWED_USER_IDS` | Comma-separated numeric user IDs; empty / `*` denies all |
| `TELEGRAM_WEBHOOK_SECRET` | 64 hex chars -- verified as `X-Telegram-Bot-Api-Secret-Token` |

Webhook path is opaque: `/webhooks/telegram/<opaque>` -- never logged.

---

## Operations

```bash
# Health
curl -s http://localhost:3525/health/live  | jq  # liveness
curl -s http://localhost:3525/health/ready | jq  # readiness (DB + deps)

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

## Security Model

- **Tool isolation** -- Only declared, schema-validated connectors; no arbitrary shell or Docker access.
- **SSRF protection** -- Loopback/RFC1918/link-local/metadata/socket/internal hostnames are blocked; DNS re-resolution and redirect re-classification; allowlist + size/timeout guards.
- **Policy + approvals** -- `READ_ONLY` auto; `SAFE_WRITE` audited; `PUBLIC_WRITE`/`PRIVILEGED` require human approval (single-use, expiry-bound, HMAC over canonical action hash); `DESTRUCTIVE` is denied.
- **Redaction** -- Tokens, `Authorization` headers, JWTs, and env secrets are masked before reaching the model or memory.
- **Container hardening** -- Non-root user, read-only rootfs, `no-new-privileges`, `cap_drop: ALL`; only `127.0.0.1:3525` is host-exposed.
- **Telegram** -- Numeric allowlist only; group mode off by default; webhook secret verified; `update_id` deduplication.

Full details: [SECURITY.md](./SECURITY.md)

---

## Project Structure

```
.
|-- apps/
|   |-- api/            # FastAPI app -- routes, SSE, webhooks, auth
|   |-- worker/         # Agent run execution
|   |-- scheduler/      # Periodic / deferred jobs
|   |-- migrate/        # Alembic one-shot runner
|   +-- web/            # React + Vite + Tailwind 4 frontend (built into API image)
|-- packages/           # Shared Python packages (policy, memory, observability, connectors)
|-- migrations/         # Alembic migrations
|-- infra/
|   |-- caddy/          # Gateway config
|   +-- compose/        # initdb
|-- scripts/            # quickstart.sh, secret-scan.sh, backup-restore.sh
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
| [ARCHITECTURE.md](./ARCHITECTURE.md) | System, data model (22 tables), queue/worker, connectors, API, SSE |
| [SECURITY.md](./SECURITY.md) | Isolation, runtime, agent, Telegram, web, and approval security |
| [OPERATIONS.md](./OPERATIONS.md) | Health, backup/restore, deploy, incident runbook |
| [docs/INSTALL.md](./docs/INSTALL.md) | Prerequisites, quick start detail, environment reference |
| [docs/CONFIGURATION.md](./docs/CONFIGURATION.md) | Full env and LLM/Telegram matrix |
| [docs/UI_GUIDE.md](./docs/UI_GUIDE.md) | Web UI -- 11 tabs, design system, onboarding, SSE |
| [CHANGELOG.md](./CHANGELOG.md) | Version history (Keep a Changelog / SemVer) |
| [LICENSE](./LICENSE) | MIT |

---

## Versioning

This project follows [Semantic Versioning](https://semver.org/) and [Keep a Changelog](https://keepachangelog.com/). The canonical version is defined in `packages/observability/__init__.py` (`__version__`) and tagged as `vMAJOR.MINOR.PATCH`.

Current release: **v1.0.0** -- see [CHANGELOG.md](./CHANGELOG.md).

To cut a new release:

```bash
gh release create v1.0.0 --generate-notes
```

---

## License

MIT -- see [LICENSE](./LICENSE).

