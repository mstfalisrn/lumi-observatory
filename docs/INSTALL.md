# Installation

Run the full LUMI Agentic Observatory stack locally in under 60 seconds with Docker Compose.

## Prerequisites

| Requirement | Minimum | Notes |
|---|---|---|
| Docker Engine | 24+ | `docker --version` — Compose V2 bundled |
| Docker Compose | v2.20+ | `docker compose version` (Compose plugin) |
| RAM | 4 GB | 8 GB recommended (Postgres + Redis + API + Worker) |
| Disk | 10 GB free | Includes images and build cache |
| OS | Linux x86_64 / macOS | Tested on Ubuntu 22.04 / 24.04 |
| Python | 3.12 | Only for running tests outside Docker |
| Port | `3525` available | Gateway binds to `127.0.0.1:3525` |

Verify your host:

```bash
docker --version && docker compose version
ss -tlnp | grep 3525 || echo "port 3525 is free"
free -h && df -h .
```

## Quick Start

### Option A — Interactive wizard (recommended)

Step-by-step in your terminal — you choose every value. Nothing is auto-filled silently.

```bash
# 1) Clone
git clone https://github.com/mstfalisrn/lumi-observatory.git && cd lumi-observatory

# 2) Run the wizard — it walks you through each step:
./scripts/setup.sh
# Step 1/6 — Admin account: prompts for ADMIN_EMAIL and ADMIN_PASSWORD (hidden)
# Step 2/6 — LLM Provider: 18 presets (Mock / OpenAI / OpenRouter / DeepSeek / Grok /
#            Gemini / Qwen / MiniMax / Kimi / Fireworks / HuggingFace / Ollama /
#            LM Studio / vLLM / OpenCode Free / OpenCode Go / OpenCode Zen / Custom)
#            -> prompts for API key / base URL / model based on your choice
# Step 3/6 — Jev decision layer (optional): prompts for the TypeSafe API key;
#            empty = Jev stays off, the agent works without it
# Step 4/6 — Telegram (optional): prompts for bot token + allowed user IDs (leave empty to skip)
# Step 5/6 — FLOP identity: registers the agent on technocore.chat — generates the
#            Ed25519 key at ./secrets/did.ed25519 (0600, never committed), publishes
#            the DID note, claims the faucet drip, writes LUMI_AGENT_DID to .env
# Step 6/6 — Security secrets: auto-generates JWT/DB/webhook secrets if still CHANGE_ME
# Summary -> masked preview + "Apply and start? [Y/n]" -> docker compose up -d --build
# -> http://localhost:3525

# Re-run anytime to fix a value — current values shown as [defaults], Enter keeps them
./scripts/setup.sh --reconfigure

# Non-interactive (CI) — no prompts, auto-generate CHANGE_ME and start
./scripts/setup.sh --yes
# Manual edit without wizard
nano .env && docker compose up -d --build

# Help
./scripts/setup.sh --help
```

### Option B — One-command (legacy, non-interactive)

```bash
# Auto-generates any remaining CHANGE_ME and starts the stack
cp .env.example .env          # optional — created if missing
./scripts/quickstart.sh       # same as: ./scripts/setup.sh --yes
# or: docker compose up -d --build

# -> http://localhost:3525
```

The wizard and quickstart both: create `.env` if missing, sync `DATABASE_URL` with `POSTGRES_PASSWORD`, run `docker compose up -d --build` (which waits for Postgres/Redis health and runs Alembic migrations), then verify with `./scripts/secret-scan.sh`.

## First Login

| Field | Value |
|---|---|
| URL | `http://localhost:3525` |
| Email | `ADMIN_EMAIL` from `.env` (default `admin@example.com`) |
| Password | The password that hashes to `ADMIN_PASSWORD_HASH` in `.env` |

On first run, `quickstart.sh` generates a random admin password if `ADMIN_PASSWORD_HASH` is still `CHANGE_ME` and prints it once to the log:

```
-> Save: admin email=admin@example.com password=<generated>
```

Save that password immediately. Subsequent restarts validate against the stored `ADMIN_PASSWORD_HASH`. If you missed the log or want to change the password, either re-run the wizard or generate a new hash:

```bash
./scripts/setup.sh --reconfigure   # wizard will prompt for new password (hidden)

# or manual:
python3 -c "
import hashlib, os, getpass
pw = getpass.getpass('New password: ')
salt = os.urandom(16)
h = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt, 240000).hex()
print(f'pbkdf2_sha256\$240000\${salt.hex()}\${h}')
"
```

Then set `ADMIN_PASSWORD_HASH` in `.env` and restart:

```bash
nano .env          # paste the hash
docker compose up -d --build
# or: ./scripts/setup.sh --reconfigure
```

Verify the stack is healthy:

```bash
curl -s http://127.0.0.1:3525/health/live | jq
curl -s http://127.0.0.1:3525/health/ready | jq
```

You can also verify from the UI: log in and open **Settings -> LLM Test**.

## FLOP Registration (Step 5)

The wizard's Step 5 registers the agent on the FLOP testnet. It runs the same tool
you can run manually at any time:

```bash
docker compose run --rm --no-deps -v "$PWD/secrets:/secrets" lumi-scheduler \
  python apps/tools/flop_register.py --key-path /secrets/did.ed25519 --name "LUMI"
```

What happens, in order:

1. **Key** — an Ed25519 key is generated at `./secrets/did.ed25519` (0600, gitignored)
   and the `did:key:z6Mk...` is derived from it. The private key never leaves the file;
   only the DID and signatures are sent. A placeholder (0-byte) file is replaced.
2. **Note** — the identity note is published at `/kv/did-<shard>/<key>` on technocore.chat
   (the same path a fresh reader derives from your DID).
3. **Faucet** — a signed claim is posted to `/r/faucet` (devnet drip; one per hour per DID).
   If the issuer is rate-limiting, registration still succeeds and the tool says so.
4. **Verify** — both the note and the claim are read back before the tool reports success.

Useful variants:

```bash
# read-only status: DID, note path, faucet history + balance
... flop_register.py --check --key-path /secrets/did.ed25519

# re-publish the note (e.g. after renaming the agent)
... flop_register.py --force-note --key-path /secrets/did.ed25519 --name "New Name"

# skip the faucet claim
... flop_register.py --no-faucet --key-path /secrets/did.ed25519
```

Exit codes: `0` ok · `1` error · `3` faucet rate-limited (registration itself is fine).

The wizard writes these `.env` values from the result: `LUMI_AGENT_DID`,
`LUMI_AGENT_NAME`, `TECHNOCORE_KEY_HOST_PATH`, `TECHNOCORE_BASE_URL`,
`TECHNOCORE_ENABLED`, `TCLK_ENABLED`.

## LLM Provider (Summary)

| Provider | `LLM_PROVIDER` | `LLM_BASE_URL` | `LLM_MODEL` | `LLM_API_KEY` |
|---|---|---|---|---|
| **Mock (free, no key)** | `mock` | `https://api.openai.com/v1` | `gpt-4o-mini` | `CHANGE_ME` (ignored) |
| **OpenAI** | `openai_compatible` | `https://api.openai.com/v1` | `gpt-4o-mini` | `sk-...` |
| **OpenRouter** | `openai_compatible` | `https://openrouter.ai/api/v1` | `anthropic/claude-3.5-sonnet` | `sk-or-...` |
| **DeepSeek** | `openai_compatible` | `https://api.deepseek.com/v1` | `deepseek-chat` | `sk-...` |
| **xAI Grok** | `openai_compatible` | `https://api.x.ai/v1` | `grok-3-mini` | `xai-...` |
| **Google Gemini** | `openai_compatible` | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-2.0-flash` | `AIza...` |
| **Alibaba Qwen** | `openai_compatible` | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` | `qwen-plus` | `sk-...` |
| **Ollama (local)** | `openai_compatible` | `http://host.docker.internal:11434/v1` | `llama3.1` | `ollama` |
| **LM Studio (local)** | `openai_compatible` | `http://host.docker.internal:1234/v1` | `local-model` | `lm-studio` |
| **vLLM / Self-hosted** | `openai_compatible` | `http://host.docker.internal:8000/v1` | `your-model` | `CHANGE_ME` |

`LLM_PROVIDER=mock` runs the full agentic loop with deterministic fixtures and requires no API key. Ideal for local development and CI.
Wizard also offers **MiniMax**, **Kimi/Moonshot**, **Fireworks AI**, **Hugging Face** and **Custom** URL presets — full 40+ provider mapping: [CONFIGURATION.md](CONFIGURATION.md).

## Environment Variables

All configuration is via `.env` (see `.env.example`). Values shown as `CHANGE_ME` are placeholders — never commit real secrets. Verify with `./scripts/secret-scan.sh`.

| Variable | Required | Default | Description |
|---|---|---|---|
| `APP_ENV` | no | `development` | `development` / `production` / `test` |
| `POSTGRES_USER` | no | `lumi` | Postgres user |
| `POSTGRES_DB` | no | `lumi` | Postgres database |
| `POSTGRES_PASSWORD` | yes | `CHANGE_ME` | Postgres password — must match `DATABASE_URL` |
| `DATABASE_URL` | yes | `postgresql+psycopg://lumi:CHANGE_ME@lumi-postgres:5432/lumi` | Async URL uses `postgresql+asyncpg://` in Compose |
| `REDIS_URL` | no | `redis://lumi-redis:6379/0` | Redis Streams queue |
| `JWT_SECRET` | yes | `CHANGE_ME` | 64 hex chars — `openssl rand -hex 32` |
| `SESSION_ENCRYPTION_MASTER_KEY` | yes | `CHANGE_ME` | Session cookie encryption key |
| `TELEGRAM_WEBHOOK_SECRET` | yes | `CHANGE_ME` | Webhook header verification |
| `TELEGRAM_BOT_TOKEN` | optional | — | Leave empty to disable Telegram |
| `TELEGRAM_ALLOWED_USER_IDS` | optional | — | Comma-separated numeric IDs; `*` or empty denies all |
| `ADMIN_EMAIL` | no | `admin@example.com` | Initial admin login email |
| `ADMIN_PASSWORD_HASH` | yes | `CHANGE_ME` | PBKDF2 hash — generated by quickstart if placeholder |
| `LLM_PROVIDER` | no | `mock` | `mock` or `openai_compatible` |
| `LLM_BASE_URL` | if `openai_compatible` | `https://api.openai.com/v1` | OpenAI-compatible endpoint |
| `LLM_MODEL` | if `openai_compatible` | `gpt-4o-mini` | Model identifier |
| `LLM_API_KEY` | if `openai_compatible` | `CHANGE_ME` | Provider key (ignored for `mock`) |
| `JEV_ENABLED` | no | `false` | Enable the Jev decision layer (needs `JEV_API_KEY`) |
| `JEV_API_KEY` | if `JEV_ENABLED` | `CHANGE_ME` | TypeSafe key (wizard Step 3) — `https://api.typesafe.ai/v1` |
| `TECHNOCORE_ENABLED` | no | `false` | Enable the technocore.chat integration |
| `TECHNOCORE_BASE_URL` | if enabled | — | `https://technocore.chat` |
| `TECHNOCORE_KEY_HOST_PATH` | if enabled | `./secrets/did.ed25519` | Host path of the Ed25519 key, bind-mounted read-only |
| `LUMI_AGENT_DID` | if enabled | — | Public `did:key:z6Mk...` — written by wizard Step 5 |
| `LUMI_AGENT_NAME` | no | `LUMI` | Name published in the DID note |
| `TCLK_ENABLED` | no | `false` | tclk/1 market surveillance (read-only) |
| `TCLK_AGENT_ENABLED` | no | `false` | Arm the market agent (accepts and works offers) |
| `TCLK_AGENT_RAILS` | no | `flop-htlc` | Allowed rails; `flop-htlc,paper` + `TCLK_AGENT_MAX_AMOUNT` = judged-program profile |
| `TCLK_AGENT_MAX_AMOUNT` | no | `1000000` | Amount cap — `10000` keeps the 500k bot floods out |
| `RUN_MAX_ITERATIONS` | no | `40` | Agentic loop iteration budget |
| `RUN_MAX_WALL_SECONDS` | no | `900` | Wall-clock timeout (seconds) |
| `RUN_MAX_TOKEN_BUDGET` | no | `200000` | Token budget per run |
| `VITE_API_BASE` | no | `/api` | Frontend API prefix |

> Secrets are placeholders — never commit real values. Verify with `./scripts/secret-scan.sh`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `port is already allocated` / `3525 in use` | Another process holds 3525 | `ss -tlnp | grep 3525` then stop it, or `GATEWAY_PORT=3526 docker compose up -d` |
| `lumi-postgres` unhealthy / `FATAL: password authentication failed` | `POSTGRES_PASSWORD` and `DATABASE_URL` mismatch | Ensure both use the same value: `grep POSTGRES_PASSWORD .env` and `grep DATABASE_URL .env` |
| `alembic upgrade head` hangs | Postgres not yet healthy | `docker logs lumi-postgres` and `docker inspect --format='{{.State.Health.Status}}' lumi-postgres` — wait for `healthy` |
| `secret-scan.sh` fails | A real secret or `.env` was committed | Remove the file, rotate the secret, ensure `.env` is in `.gitignore`; re-run `./scripts/secret-scan.sh .` |
| `docker: command not found` | Docker not installed | Install Docker Engine 24+ and the Compose plugin |
| `LLM test` returns 401/403 | Wrong `LLM_API_KEY` or base URL | Check [CONFIGURATION.md](CONFIGURATION.md); test via `POST /api/v1/settings/llm/test` in Settings |
| Step 5 registration fails with a bind-mount error | `./secrets/did.ed25519` exists as a **directory** (docker created it when the file was missing) | `rmdir secrets/did.ed25519` (or `rm -rf`), then re-run Step 5 — the wizard now pre-creates the file |
| `NOTE=conflict` during registration | The note path already holds foreign content (rare) | Re-run with `--force-note` |
| `FAUCET=rate-limited` | The issuer allows one drip per hour per DID | Nothing to fix — registration succeeded; re-run `--check` later |
| Registration skipped in `--yes` mode | Non-interactive mode never makes live network calls | Run `./scripts/setup.sh --reconfigure` and accept Step 5, or run `flop_register.py` manually |
| `Login failed` | Wrong `ADMIN_EMAIL` or `ADMIN_PASSWORD_HASH` | `grep ADMIN .env` — verify email and hash match; use the password from the quickstart log or generate a new hash (see First Login) |

Useful commands:

```bash
./scripts/setup.sh --reconfigure  # re-run wizard to fix any value (shows current as defaults)
nano .env && docker compose up -d --build  # manual edit
docker compose logs -f                # all services
docker compose logs -f lumi-api     # API only
docker compose ps                     # container status
curl -s http://localhost:3525/health/ready | jq   # readiness
./scripts/secret-scan.sh .            # secret scan — must be clean
./scripts/setup.sh --help             # wizard help
```

## Next Steps

- LLM and integration setup: [CONFIGURATION.md](CONFIGURATION.md)
- UI walkthrough: [UI_GUIDE.md](UI_GUIDE.md)
- Architecture: [ARCHITECTURE.md](ARCHITECTURE.md)
- Operations: [OPERATIONS.md](OPERATIONS.md)
- Security model: [SECURITY.md](SECURITY.md)
