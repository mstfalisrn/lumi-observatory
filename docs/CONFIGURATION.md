# Configuration

All runtime configuration is via environment variables in `.env` (see `.env.example`). LLM and Telegram integrations are configured here.

> Secrets are placeholders — never commit real values. Verify with `./scripts/secret-scan.sh`.

## Quick Start

```bash
# 1) Clone
git clone https://github.com/your-owner/lumi-observatory.git && cd lumi-observatory

# 2) Environment — copy the template (all values are CHANGE_ME placeholders)
cp .env.example .env
# Mock works with no key; for real providers edit LLM_API_KEY below

# 3) Start — interactive wizard (recommended)
./scripts/setup.sh
# -> http://localhost:3525

# 3b) Non-interactive (CI) — auto-generates any remaining CHANGE_ME
./scripts/quickstart.sh
# -> http://localhost:3525
```

- **Prerequisites:** Docker 24+, 4 GB RAM, 10 GB disk, port 3525 available — see [INSTALL.md](INSTALL.md#prerequisites).
- **Health check:** `curl -s http://localhost:3525/health/ready | jq`
- **Secret scan:** `./scripts/secret-scan.sh .` — must be clean before committing.

## LLM Provider Matrix

LUMI speaks the **OpenAI Chat Completions API** (`POST {base_url}/chat/completions` + `Bearer` key). Every provider below maps to `LLM_PROVIDER=openai_compatible` with a different `LLM_BASE_URL` (except `mock` which needs no key). The wizard offers 18 presets (checkbox menu via whiptail radiolist — incl. OpenCode Free/Go/Zen); `Custom` accepts any other OpenAI-compatible URL.

### Common presets (14 wizard options + Custom)

| Provider | `LLM_PROVIDER` | `LLM_BASE_URL` | `LLM_MODEL` example | `LLM_API_KEY` |
|---|---|---|---|---|
| **Mock (free)** | `mock` | `https://api.openai.com/v1` | `gpt-4o-mini` | `CHANGE_ME` (ignored) |
| **OpenAI** | `openai_compatible` | `https://api.openai.com/v1` | `gpt-4o-mini` | `sk-...` |
| **OpenRouter** (300+ models aggregator) | `openai_compatible` | `https://openrouter.ai/api/v1` | `openai/gpt-4o-mini` | `sk-or-...` |
| **Anthropic via OpenRouter** | `openai_compatible` | `https://openrouter.ai/api/v1` | `anthropic/claude-3.5-sonnet` | `sk-or-...` |
| **DeepSeek** | `openai_compatible` | `https://api.deepseek.com/v1` | `deepseek-chat` | `sk-...` |
| **xAI Grok** | `openai_compatible` | `https://api.x.ai/v1` | `grok-3-mini` | `xai-...` |
| **Google Gemini** (OpenAI compat) | `openai_compatible` | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-2.0-flash` | `AIza...` |
| **Alibaba Qwen** (DashScope Intl) | `openai_compatible` | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` | `qwen-plus` | `sk-...` |
| **MiniMax** | `openai_compatible` | `https://api.minimax.chat/v1` | `MiniMax-M2` | `sk-...` |
| **Kimi / Moonshot** | `openai_compatible` | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` | `sk-...` |
| **Fireworks AI** | `openai_compatible` | `https://api.fireworks.ai/inference/v1` | `accounts/fireworks/models/llama-v3p1-8b-instruct` | `fw_...` |
| **Hugging Face Inference** | `openai_compatible` | `https://router.huggingface.co/v1` | `meta-llama/Llama-3.1-8B-Instruct` | `hf_...` |
| **Ollama (local)** | `openai_compatible` | `http://host.docker.internal:11434/v1` | `llama3.1` | `ollama` |
| **LM Studio (local)** | `openai_compatible` | `http://host.docker.internal:1234/v1` | `local-model` | `lm-studio` |
| **vLLM / SGLang / llama.cpp** (self-hosted) | `openai_compatible` | `http://host.docker.internal:8000/v1` | `your-model` | `CHANGE_ME` or key |

> `mock` runs the entire agentic loop (Planner -> Coordinator -> ToolExecutor -> Verifier -> Reporter) with deterministic fixtures. Tests and CI use `mock` by default.

### Full provider mapping (40+ providers)

LUMI supports 40+ inference providers via `openai_compatible`, which covers every provider that exposes an OpenAI-compatible `/chat/completions` endpoint. OAuth-only providers (portal/broker) do not expose a raw API key — use their API-key alternative or proxy via OpenRouter / a custom gateway.

| Provider | LUMI mode | How to configure in LUMI |
|---|---|---|
| **Nous Portal** (OAuth, subscription) | `openai_compatible` | No direct key — get an API key from portal or proxy via OpenRouter. OAuth subscription models require a separate API key for LUMI. |
| **OpenAI Codex** (OAuth, ChatGPT plan) | `openai_compatible` | Use `OPENAI_API_KEY` (`sk-...`) from platform.openai.com instead of Codex OAuth. |
| **GitHub Copilot** (OAuth device code) | `openai_compatible` | No raw key — use OpenRouter proxy for Copilot models or any `openai_compatible` provider. |
| **GitHub Copilot ACP** (local `copilot --acp`) | — | Not applicable (ACP transport). Use Ollama/LM Studio/vLLM locally instead. |
| **Anthropic** (API key + OAuth Max) | `openai_compatible` | Prefer OpenRouter: `https://openrouter.ai/api/v1` + `anthropic/claude-3.5-sonnet`. Direct Anthropic Messages API is not OpenAI-compatible. |
| **OpenRouter** | `openai_compatible` | `https://openrouter.ai/api/v1` + `sk-or-...` (recommended aggregator for Claude, Gemini, Grok, etc.) |
| **Ramp Router** | `openai_compatible` | `https://api.router.com/v1` (or vendor URL) + `RAMP_ROUTER_API_KEY` as `LLM_API_KEY` |
| **Fireworks AI** | `openai_compatible` | `https://api.fireworks.ai/inference/v1` + `FIREWORKS_API_KEY` |
| **NovitaAI** | `openai_compatible` | `https://api.novita.ai/v3/openai` + `NOVITA_API_KEY` |
| **AI Gateway (Vercel)** | `openai_compatible` | `https://ai-gateway.vercel.sh/v1` (or your gateway URL) + `AI_GATEWAY_API_KEY` |
| **z.ai / GLM** | `openai_compatible` | `https://open.bigmodel.cn/api/paas/v4/` or `https://api.z.ai/api/paas/v4/` + `GLM_API_KEY` |
| **Kimi / Moonshot** | `openai_compatible` | `https://api.moonshot.cn/v1` + `KIMI_API_KEY` |
| **Kimi / Moonshot (China)** | `openai_compatible` | `https://api.moonshot.cn/v1` + `KIMI_CN_API_KEY` |
| **Arcee AI** | `openai_compatible` | Vendor base URL (docs) + `ARCEEAI_API_KEY` as `LLM_API_KEY` |
| **GMI Cloud** | `openai_compatible` | Vendor base URL + `GMI_API_KEY` |
| **Nebius Token Factory** | `openai_compatible` | `https://api.studio.nebius.com/v1` (or Token Factory URL) + `NEBIUS_API_KEY` |
| **Actual Computer** (local relay) | `openai_compatible` | `http://127.0.0.1:8080/v1` (local) or hosted relay URL + `ACTUAL_API_KEY` |
| **MiniMax** | `openai_compatible` | `https://api.minimax.chat/v1` + `MINIMAX_API_KEY` |
| **MiniMax China** | `openai_compatible` | `https://api.minimax.chat/v1` (CN endpoint if different) + `MINIMAX_CN_API_KEY` |
| **xAI Grok** (Responses API) | `openai_compatible` | `https://api.x.ai/v1` + `XAI_API_KEY` |
| **xAI Grok OAuth** (SuperGrok) | `openai_compatible` | OAuth has no key — use `https://api.x.ai/v1` + `XAI_API_KEY` instead. |
| **Qwen Cloud / Alibaba DashScope** | `openai_compatible` | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` (Intl) or `https://dashscope.aliyuncs.com/compatible-mode/v1` (CN) + `DASHSCOPE_API_KEY` |
| **Alibaba Coding Plan** | `openai_compatible` | DashScope compatible-mode URL + `ALIBABA_CODING_PLAN_API_KEY` |
| **Alibaba Token Plan** | `openai_compatible` | DashScope compatible-mode URL + `ALIBABA_TOKEN_PLAN_API_KEY` |
| **Kilo Code** | `openai_compatible` | Vendor base URL + `KILOCODE_API_KEY` |
| **Xiaomi MiMo** | `openai_compatible` | Vendor base URL + `XIAOMI_API_KEY` |
| **Tencent TokenHub** | `openai_compatible` | Vendor base URL + `TOKENHUB_API_KEY` |
| **Tencent TokenPlan** | `openai_compatible` | Vendor base URL + `TOKENPLAN_API_KEY` |
| **OpenCode Zen** | `openai_compatible` | Vendor base URL + `OPENCODE_ZEN_API_KEY` (or use `mock` free if no key) |
| **CommandCode** | `openai_compatible` | Vendor base URL + `COMMANDCODE_API_KEY` |
| **OpenCode Go** | `openai_compatible` | Vendor base URL + `OPENCODE_GO_API_KEY` |
| **OpenCode Free** (keyless) | `mock` | No key — use `mock` or any free-tier `openai_compatible` endpoint. |
| **DeepSeek** | `openai_compatible` | `https://api.deepseek.com/v1` + `DEEPSEEK_API_KEY` |
| **Hugging Face** | `openai_compatible` | `https://router.huggingface.co/v1` (or `https://api-inference.huggingface.co/v1`) + `HF_TOKEN` |
| **Google / Gemini** (API key) | `openai_compatible` | `https://generativelanguage.googleapis.com/v1beta/openai/` + `GOOGLE_API_KEY`/`GEMINI_API_KEY` |
| **Google Vertex AI** (OAuth/service account) | `openai_compatible` | Vertex OpenAI-compatible endpoint or proxy via OpenRouter; OAuth not direct. |
| **OpenAI API (direct)** | `openai_compatible` | `https://api.openai.com/v1` + `OPENAI_API_KEY` |
| **Azure AI Foundry** (OAuth) | `openai_compatible` | Azure OpenAI endpoint (e.g. `https://{resource}.openai.azure.com/openai/v1`) + Azure key |
| **AWS Bedrock** (AWS creds) | `openai_compatible` | Bedrock OpenAI-compatible endpoint or proxy — not direct; consider OpenRouter. |
| **NVIDIA Build** | `openai_compatible` | `https://integrate.api.nvidia.com/v1` + `NVIDIA_API_KEY` |
| **Ollama Cloud** (OAuth) | `openai_compatible` | Cloud endpoint URL + key, or self-hosted `http://host.docker.internal:11434/v1` |
| **Qwen OAuth** | `openai_compatible` | OAuth has no key — use DashScope API-key endpoint above. |
| **MiniMax OAuth** | `openai_compatible` | OAuth has no key — use `https://api.minimax.chat/v1` + key above. |
| **StepFun** | `openai_compatible` | Vendor base URL + `STEPFUN_API_KEY` |
| **LM Studio** | `openai_compatible` | `http://host.docker.internal:1234/v1` (enable "Serve on Network") |
| **Custom Endpoint** | `openai_compatible` | Any `http(s)://host:port/v1` that speaks OpenAI Chat Completions (vLLM, SGLang, llama.cpp, etc.) |

> **Self-hosted:** Ollama (`http://host.docker.internal:11434/v1`), LM Studio (`:1234/v1`), vLLM/SGLang (`:8000/v1`), llama.cpp/llama-server — all use `LLM_PROVIDER=openai_compatible` with your local URL. On Linux replace `host.docker.internal` with the host gateway IP (e.g. `172.17.0.1`) if needed.

Verify the provider via **Settings -> LLM Test** (`POST /api/v1/settings/llm/test`) or `curl -s http://127.0.0.1:3525/health/ready | jq`.

### Examples

**1. Mock — no key required:**

```bash
LLM_PROVIDER=mock
# LLM_API_KEY can remain CHANGE_ME — mock ignores it
```

**2. OpenAI:**

```bash
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
LLM_API_KEY=sk-proj-...
```

**3. OpenRouter (Anthropic via OpenRouter):**

```bash
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_MODEL=anthropic/claude-3.5-sonnet
LLM_API_KEY=sk-or-v1-...
# OpenRouter may require HTTP-Referer / X-Title headers — set via provider config if needed.
```

**4. DeepSeek:**

```bash
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
LLM_API_KEY=sk-...
```

**5. xAI Grok:**

```bash
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://api.x.ai/v1
LLM_MODEL=grok-3-mini
LLM_API_KEY=xai-...
```

**6. Google Gemini (OpenAI-compatible):**

```bash
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
LLM_MODEL=gemini-2.0-flash
LLM_API_KEY=AIza...
```

**7. Alibaba Qwen (DashScope):**

```bash
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://dashscope-intl.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen-plus
LLM_API_KEY=sk-...
```

**8. Ollama (local):**

```bash
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://host.docker.internal:11434/v1
LLM_MODEL=llama3.1
LLM_API_KEY=ollama
# Linux: replace host.docker.internal with the host gateway IP (e.g. 172.17.0.1) if needed.
# Ensure Ollama is running: ollama serve && ollama pull llama3.1
```

**9. Self-hosted vLLM / LM Studio:**

```bash
# vLLM
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://host.docker.internal:8000/v1
LLM_MODEL=your-model
# LM Studio: http://host.docker.internal:1234/v1
```

## First Login

| Field | Value |
|---|---|
| URL | `http://localhost:3525` |
| Email | `ADMIN_EMAIL` from `.env` (default `admin@example.com`) |
| Password | The password that hashes to `ADMIN_PASSWORD_HASH` in `.env` |

`quickstart.sh` generates a random password if `ADMIN_PASSWORD_HASH` is still `CHANGE_ME` and prints it once to the log. Save it immediately — subsequent restarts validate against the stored hash.

## Telegram

| Variable | Example | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | `123456:AAF...` | From @BotFather; leave empty to disable Telegram |
| `TELEGRAM_ALLOWED_USER_IDS` | `12345678,987654321` | Comma-separated numeric IDs; `*` or empty denies all |
| `TELEGRAM_WEBHOOK_SECRET` | 64 hex chars | Verified via `X-Telegram-Bot-Api-Secret-Token` header |
| `TELEGRAM_GROUP_ENABLED` | `false` | Group mode is off by default |

- Webhook path is opaque: `/webhooks/telegram/<opaque>` — never log the token or path.
- Updates are deduplicated by `update_id` (idempotent inbox).
- To disable Telegram, leave `TELEGRAM_BOT_TOKEN` empty.

## Other Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `POSTGRES_PASSWORD` | yes | `CHANGE_ME` | Must match `DATABASE_URL` |
| `DATABASE_URL` | yes | `postgresql+psycopg://lumi:CHANGE_ME@lumi-postgres:5432/lumi` | Async variant uses `postgresql+asyncpg://` |
| `REDIS_URL` | no | `redis://lumi-redis:6379/0` | Redis Streams queue |
| `JWT_SECRET` | yes | `CHANGE_ME` | 64 hex chars — `openssl rand -hex 32` |
| `SESSION_ENCRYPTION_MASTER_KEY` | yes | `CHANGE_ME` | Session cookie encryption |
| `RUN_MAX_ITERATIONS` | no | `40` | Agentic loop iteration budget |
| `RUN_MAX_WALL_SECONDS` | no | `900` | Wall-clock timeout (seconds) |
| `RUN_MAX_TOKEN_BUDGET` | no | `200000` | Token budget per run |

See `.env.example` for the full list with comments. All `CHANGE_ME` values are placeholders that `quickstart.sh` replaces with generated secrets if left unchanged.

## Verification & Troubleshooting

```bash
docker --version && docker compose version
./scripts/secret-scan.sh .                          # must be clean
curl -s http://localhost:3525/health/live | jq      # liveness
curl -s http://localhost:3525/health/ready | jq     # readiness (DB + Redis)
docker compose logs -f
```

| Issue | Fix |
|---|---|
| `3525 in use` | `ss -tlnp | grep 3525` or `GATEWAY_PORT=3526 docker compose up -d` |
| `password authentication failed` | `POSTGRES_PASSWORD` and `DATABASE_URL` must use the same value — quickstart syncs them automatically |
| `secret-scan` fails | Remove committed secrets, ensure `.env` is in `.gitignore`; rotate exposed values |
| LLM 401/403 | Check `LLM_API_KEY` / `LLM_BASE_URL` — verify with `mock` first |
| Login failed | Verify `ADMIN_EMAIL` / `ADMIN_PASSWORD_HASH` in `.env` against the quickstart log |

## References

- Environment template: `.env.example`
- Quickstart script: `scripts/quickstart.sh` (idempotent) — `docker compose up -d --build` also works
- Health endpoints: `GET /health/live`, `GET /health/ready`
- Secret scan: `./scripts/secret-scan.sh .`
- Install guide: [INSTALL.md](INSTALL.md)
- UI guide: [UI_GUIDE.md](UI_GUIDE.md)
