#!/usr/bin/env bash
# Create/refresh the read-only Postgres role the logs service should use, and
# wire it into .env (LOGS_DATABASE_URL). The dashboard then cannot write even
# if it wanted to; least privilege for a service that only ever runs SELECT.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

DB="${POSTGRES_DB:-lumi}"
OWNER="${POSTGRES_USER:-lumi}"
PW="${LOGS_DB_PASSWORD:-$(openssl rand -hex 16)}"

docker compose exec -T lumi-postgres psql -U "$OWNER" -d "$DB" -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'lumi_reader') THEN
    CREATE ROLE lumi_reader LOGIN PASSWORD '$PW';
  ELSE
    ALTER ROLE lumi_reader PASSWORD '$PW';
  END IF;
END
\$\$;
GRANT CONNECT ON DATABASE "$DB" TO lumi_reader;
GRANT USAGE ON SCHEMA public TO lumi_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO lumi_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO lumi_reader;
SQL

touch .env
grep -q '^LOGS_DB_PASSWORD=' .env || printf '\nLOGS_DB_PASSWORD=%s\n' "$PW" >> .env
grep -q '^LOGS_DATABASE_URL=' .env || printf 'LOGS_DATABASE_URL=postgresql://lumi_reader:%s@lumi-postgres:5432/%s\n' "$PW" "$DB" >> .env
echo "reader role ready — LOGS_DATABASE_URL written to .env (restart lumi-logs to use it)"
