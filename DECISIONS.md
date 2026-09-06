# Decision Log — LUMI

This file records architecture and product decisions with rationale (append-only intent).

## D1 — Single-Origin Architecture (UI + API on the Same Origin)
- **Decision:** Embed the Web UI static build into the `lumi-api` image; remove the separate `lumi-web` service.
- **Rationale:** The specification requires serving the UI under the same origin as the API. A single origin reduces CORS/CSRF/cookie risks and allows SSO / Cloudflare Access to be verified at a single point.
- **Alternative:** separate static server. Rejected (dual origin).

## D2 — Redis List Instead of Quartz / Celery
- **Decision:** Job queue is a Redis list (`lumi:queue`); the worker performs a blocking poll.
- **Rationale:** Fewer dependencies, client-driven, sufficient for the current scale. Can be migrated to RQ/arq in production if needed.
- **Note:** Replaceable via an abstract queue interface.

## D3 — Breaking the FK Cycle on `runs.plan_id`
- **Decision:** `runs.plan_id` is a plain UUID column; the only foreign key is `plans.run_id -> runs` (one-directional).
- **Rationale:** A bidirectional FK between `plans` and `runs` broke Alembic autogenerate (table ordering).
- **Impact:** Referential integrity for `runs.plan_id` is enforced at the application layer.

## D4 — Alembic Async (asyncpg)
- **Decision:** Database uses `postgresql+asyncpg`; the Alembic environment runs in async mode.
- **Rationale:** The application is async; a synchronous psycopg setup would require two different database URLs.

## D5 — External Public Writes Disabled by Default
- **Decision:** `external_signed_write` is `REQUIRE_APPROVAL` in the policy engine, gated by `PUBLIC-POST-APPROVED`. The DID key is still generated and signatures are verified.
- **Rationale:** No writes without explicit user approval — mitigates airdrop / spam abuse.

## D6 — Cloudflare Access Not Enabled Yet
- **Decision:** The public hostname is not activated yet; only localhost and private-network access.
- **Rationale:** Follows the rule "do not activate the public hostname unless Access is ready." When Access is configured, add ingress, DNS, and origin JWT verification.

## D7 — Telegram / LLM Keys Are Provided Fresh (Not Copied)
- **Decision:** The Telegram bot and LLM provider credentials are provided separately (not copied from the operator). They are entered via the secrets setup script. A mock provider is the default for dev/test.
- **Rationale:** Isolation — operator secrets are not shared with the runtime.
