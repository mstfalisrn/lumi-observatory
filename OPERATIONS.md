# Operations / Runbook — LUMI

## Services

| Service    | Status / Logs Command              | Port        |
|------------|------------------------------------|-------------|
| Stack (systemd) | `systemctl status lumi-observatory` / `journalctl -u lumi-observatory` | — |
| API        | `docker logs lumi-api`           | internal 8000 |
| Worker     | `docker logs lumi-worker`        | internal 8001 |
| Scheduler  | `docker logs lumi-scheduler`     | internal 8002 |
| Gateway    | `docker logs lumi-gateway`       | 127.0.0.1:3525 |
| PostgreSQL | `docker logs lumi-postgres`      | internal    |
| Redis      | `docker logs lumi-redis`         | internal    |

## Health
- `curl http://127.0.0.1:3525/health/live` — liveness
- `curl http://127.0.0.1:3525/health/ready` — readiness (DB connectivity)

## Backup / Restore
```bash
# Load DB_PASSWORD from your external secrets store (not from the repo)
export DB_PASSWORD="..."  # sourced externally
./scripts/backup-restore.sh backup
./scripts/backup-restore.sh restore ${LUMI_BACKUP_DIR:-./backups}/lumi-<timestamp>.dump
```
Restore writes to a separate `lumi_restore_test` database; it does not touch production data.

## Deployment / Update
```bash
# From the repository root
docker compose up -d --build
./scripts/secret-scan.sh .
```

## Incident

- **Worker stalled:** `docker restart lumi-worker` — resumes from the Redis queue.
- **API unhealthy:** `docker logs lumi-api` — check for migration or import errors.
- **Data loss:** restore from backup (`backup-restore.sh restore`).
- **Circuit breaker tripped:** resets automatically after a 30s cooldown; if persistent, review the failing tool.

## Secrets
- Rotate or generate: `./scripts/configure-secrets.sh --gen` or interactive mode, then `docker compose up -d`.
- Never print secrets to screen, logs, or commits.

## Access
- Local: `http://127.0.0.1:3525`
- Private network: via VPN / private overlay (e.g., Tailscale / WireGuard) if configured — no hardcoded IPs in the repo.
- Public hostname (optional): when Cloudflare Access is configured, expose via an example domain such as `lumi.example.com`. Public access is disabled by default until Access is active.
