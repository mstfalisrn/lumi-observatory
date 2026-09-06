#!/usr/bin/env bash
# LUMI — database backup (PostgreSQL dump) and restore helper
# Backups are written under ${LUMI_BACKUP_DIR:-./backups} with timestamps.
set -euo pipefail

BACKUP_DIR="${LUMI_BACKUP_DIR:-./backups}"
CONTAINER="${LUMI_POSTGRES_CONTAINER:-lumi-postgres}"
DB_USER="${POSTGRES_USER:-lumi}"
DB_NAME="${POSTGRES_DB:-lumi}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"

mkdir -p "$BACKUP_DIR"
chmod 0700 "$BACKUP_DIR"

action="${1:-backup}"
TS=$(date +%Y%m%d-%H%M%S)

case "$action" in
  backup)
    OUT="$BACKUP_DIR/lumi-$TS.dump"
    docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" "$CONTAINER" \
      pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > "$OUT"
    chmod 0600 "$OUT"
    echo "✅ Yedek: $OUT ($(du -h "$OUT" | cut -f1))"
    ;;
  restore)
    SRC="${2:?source dump file required for restore}"
    # Restore to backup DB (without overwriting production DB)
    docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" "$CONTAINER" \
      createdb -U "$DB_USER" -O "$DB_USER" lumi_restore_test 2>/dev/null || true
    docker exec -i -e PGPASSWORD="$POSTGRES_PASSWORD" "$CONTAINER" \
      pg_restore -U "$DB_USER" -d lumi_restore_test --no-owner --no-privileges < "$SRC"
    echo "✅ Restore test complete: loaded into lumi_restore_test database"
    echo "   (production database untouched)"
    ;;
  *)
    echo "Usage: $0 backup|restore <file>"
    exit 1
    ;;
esac