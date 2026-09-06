# UI Guide — LUMI Observatory

The Web UI is a single-origin React SPA served from `lumi-api` (same origin as `/api`). This guide covers navigation, design system, onboarding, and live updates.

## Quick Start

```bash
git clone https://github.com/your-owner/lumi-observatory.git && cd lumi-observatory
cp .env.example .env
./scripts/quickstart.sh
# -> http://localhost:3525
```

- **Prerequisites:** Docker 24+, 4 GB RAM, port 3525 available — see [INSTALL.md](INSTALL.md#prerequisites).
- **LLM:** `mock` (no key) or `openai_compatible` for OpenAI / OpenRouter / Ollama — see [CONFIGURATION.md](CONFIGURATION.md).

## First Login

| Field | Value |
|---|---|
| URL | `http://localhost:3525` |
| Email | `ADMIN_EMAIL` from `.env` (default `admin@example.com`) |
| Password | The password that hashes to `ADMIN_PASSWORD_HASH` in `.env` |

On first run, `quickstart.sh` generates a random password if `ADMIN_PASSWORD_HASH` is still `CHANGE_ME` and prints it once:

```
-> Save: admin email=admin@example.com password=<generated>
```

Save it immediately. Verify the stack: `curl -s http://localhost:3525/health/ready | jq` or open **Settings -> LLM Test** after login.

> Secrets are placeholders — never commit real values. Verify with `./scripts/secret-scan.sh`.

## Navigation Map — 11 Tabs

| # | Tab | Key | Route / Purpose |
|---|---|---|---|
| 1 | **Dashboard** | `dashboard` | KPI cards, timeline of last 5 runs, approvals queue, SSE health |
| 2 | **Runs** | `runs` | Paginated table with search and status filter, stepper, status badges |
| 3 | **Approvals** | `approvals` | `PENDING` queue — approve or reject with expiry and HMAC token |
| 4 | **Context** | `context` | Context Inspector — 7 layers, token budget, segment breakdown |
| 5 | **Memory** | `memory` | Candidate -> approved/active -> superseded/expired lifecycle, auto-promotion |
| 6 | **Sources** | `sources` | Source registry — enable/disable, last access and error history |
| 7 | **Technocore** | `technocore` | Room `<your-room>` (configurable via TECHNOCORE_MONITORED_ROOMS), read cursors, signed write gating |
| 8 | **Telegram** | `telegram` | Bot status, webhook health, allowed user IDs |
| 9 | **Settings** | `settings` | Non-secret settings, LLM connectivity test (`POST /v1/settings/llm/test`) |
| 10 | **Reports** | `reports` | Evidence bundle and auditor packet generation |
| 11 | **Audit** | `audit` | Append-only run events with `global_seq`, verifier log |

Detail view: **Run Detail** (`run-detail`) — reachable from Dashboard or Runs — shows the execution stepper, event stream, and controls (pause / resume / stop / retry).

## Design System

| Token | Value | Notes |
|---|---|---|
| Framework | **Tailwind CSS 4** + `@tailwindcss/vite` | Vite plugin, no PostCSS config |
| Components | **shadcn/ui** — `button`, `card`, `badge`, `input`, `sheet` | `cn()` via `clsx` + `tailwind-merge` |
| Colors | **oklch** tokens in `styles.css` | `--background`, `--primary`, `--sidebar-*`; light/dark via `.dark` |
| Spacing | **8pt grid** | `p-3` / `gap-3` / `h-14` top bar, `rounded-xl` cards |
| Icons | **lucide-react** | One-to-one per tab (e.g. `Bird`, `LayoutDashboard`, `Satellite`) |
| Motion | **framer-motion** | `motion.div` for KPI pulse, timeline, toast, and page transitions |
| Fonts | System sans, `tracking-tight`, `text-sm` body | `antialiased`, `backdrop-blur` top bar |
| Layout | Sidebar (collapsible) + Top bar (`sticky h-14`) + Drawer (`Sheet`) on mobile | `md:hidden` breakpoint |

Additional details:

- Sidebar collapses to icon-only mode via the chevron (`PanelLeft`) — tooltips remain via `title`.
- Mobile navigation uses a hamburger (`Menu`) that opens a `Sheet` drawer — same `NAV` array, no duplication.
- Search and filter in Runs are client-side (`statusFilter` + `q`) over a `limit`/`offset` paginated page.

## Screenshots (Placeholders)

Replace `docs/screenshots/*.png` with real captures before release.

```
docs/screenshots/
  01-dashboard.png      — KPI grid + timeline + SSE badge
  02-runs.png           — table + pagination + filter
  03-run-detail.png     — stepper + event stream
  04-approvals.png      — pending card + approve action
  05-context.png        — 7-layer inspector
  06-memory.png         — status chips + promotion
  07-settings.png       — LLM Test button + result
```

Embed once images exist:

```md
![Dashboard](screenshots/01-dashboard.png)
```

## Onboarding Flow

1. **First visit** — `GET /v1/auth/me` returns 401 -> `LoginPage` (email + password).
2. **Onboarding wizard** (`pages/Onboarding.tsx`) — 3 steps:
   - (a) Environment check (health endpoints)
   - (b) LLM provider selection (mock vs OpenAI-compatible)
   - (c) First task prompt
3. **Command Center** — `POST /v1/tasks` returns a `run_id` -> SSE `run:queued` event appears on Dashboard.
4. **Live follow** — the SSE stream updates Dashboard KPIs, the timeline, and the Run Detail stepper in real time.
5. **Approval gate** (if policy is `REQUIRE_APPROVAL`) — open the **Approvals** tab, review, then approve -> `run:resumed`.

Session state is stored as an API token in `localStorage`; logout clears the token.

## SSE Pulse & Live Updates

- **Endpoint:** `GET /api/v1/events/stream` — `text/event-stream` with `Last-Event-ID` and `global_seq` cursor for resumption.
- **Client:** `openSSE()` in `api.ts` — auto-reconnects with backoff. The badge in the top bar and on the Dashboard shows `open | connecting | error | closed` with an animated dot (`animate-ping`, `shadow-[0_0_8px_...]`).
- **Health:** `GET /health/live` (liveness) and `GET /health/ready` (readiness including DB) are polled on the Dashboard.
- **Event store:** Run events are append-only. `global_seq` is an `IDENTITY` column with a partial index for ordered fan-out.

## Troubleshooting

| Issue | Fix |
|---|---|
| `3525 in use` | `ss -tlnp | grep 3525` then stop the holder, or `GATEWAY_PORT=3526 docker compose up -d` |
| Login failed | `grep ADMIN .env` — verify email and hash; use the password from the quickstart log or generate a new hash |
| UI empty / SSE stuck on `connecting` | `curl -s http://localhost:3525/health/ready | jq` and `docker compose logs -f lumi-api` |
| LLM test returns 401 | Check `LLM_API_KEY` / `LLM_BASE_URL` — try `mock` first to isolate the issue |

```bash
docker compose logs -f                # all services
curl -s http://localhost:3525/health/ready | jq
./scripts/secret-scan.sh .            # must be clean
```

## Tips

- Collapse the sidebar with the chevron (`PanelLeft`) — icon-only mode preserves `title` tooltips.
- On mobile, the hamburger (`Menu`) opens the `Sheet` drawer with the same navigation items.
- In **Runs**, combine the text search (`q`) and status filter for quick triage over paginated results.
