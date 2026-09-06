## Summary

<!-- What does this PR change and why? Link issue if any: Closes #... -->

## Changes

- [ ] Feature / fix description
- [ ] Tests added / updated
- [ ] Docs updated (`README` / `docs/` / `CHANGELOG`)

## Checklist

- [ ] `ruff check packages apps migrations tests` passes
- [ ] `pytest` passes locally (coverage >= 70% if touching `packages/`)
- [ ] `bandit -r packages apps --severity-level high -q` — 0 High
- [ ] `./scripts/secret-scan.sh .` clean
- [ ] `docker compose config --quiet` valid (if compose changed)
- [ ] No secrets committed (`.env` ignored, only `.env.example` placeholders)
- [ ] Migration tested: `alembic upgrade head` + `downgrade -1` + `upgrade head` (if DB change)

## Screenshots / Evidence

<!-- Paste `curl /health/ready`, UI screenshot, or log snippet if relevant -->

## Risk & Rollback

<!-- Risk level: low/medium/high — rollback plan: revert + `docker compose up -d --build` -->
