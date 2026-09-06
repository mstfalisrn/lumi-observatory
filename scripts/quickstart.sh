#!/usr/bin/env bash
# LUMI Agentic Observatory — one-command quickstart (legacy, non-interactive)
# Usage: ./scripts/quickstart.sh  (run from repo root or via path)
# Idempotent: existing .env is preserved; only CHANGE_ME placeholders are replaced.
# Note: for step-by-step choice, use ./scripts/setup.sh (interactive wizard)
#       Re-run wizard anytime: ./scripts/setup.sh --reconfigure
set -euo pipefail

# Resolve repo root (script is at <root>/scripts/quickstart.sh)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT}"

# 1) Ensure .env exists (do not overwrite existing)
# Spec behavior: [ -f .env ] || cp .env.example .env
[ -f .env ] || cp .env.example .env
if [ ! -f .env ]; then
  echo "❌ .env.example not found at ${ROOT}/.env.example" >&2
  exit 1
fi
echo "✓ .env ready at ${ROOT}/.env"

# Helper: generate 32 bytes hex (64 chars) — openssl preferred, fallback to /dev/urandom
gen_hex() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  elif [ -r /dev/urandom ]; then
    head -c 32 /dev/urandom | od -A n -t x1 | tr -d ' \n'
  else
    echo "❌ No openssl or /dev/urandom available to generate secrets" >&2
    exit 1
  fi
}

replace_if_placeholder() {
  local var="$1"
  if grep -q "${var}=CHANGE_ME" .env; then
    local val
    val="$(gen_hex)"
    # Replace only the token CHANGE_ME, preserving trailing comments/whitespace would be lost — keep simple
    sed -i "s/${var}=CHANGE_ME/${var}=${val}/" .env
    echo "✓ Generated ${var}"
  fi
}

replace_if_placeholder "JWT_SECRET"
replace_if_placeholder "SESSION_ENCRYPTION_MASTER_KEY"
replace_if_placeholder "TELEGRAM_WEBHOOK_SECRET"
replace_if_placeholder "POSTGRES_PASSWORD"

# ADMIN_PASSWORD_HASH — PBKDF2 hash (contains $), handle separately via python to avoid sed escaping
if grep -qE "^ADMIN_PASSWORD_HASH=(CHANGE_ME)?$" .env || grep -q "^ADMIN_PASSWORD_HASH=CHANGE_ME" .env; then
  # Check if already a real hash (pbkdf2_sha256$...) then skip
  if grep -qE "^ADMIN_PASSWORD_HASH=pbkdf2_sha256\\$" .env; then
    echo "✓ ADMIN_PASSWORD_HASH already set"
  else
    # Generate random admin password and hash it
    ADMIN_PLAIN="$(openssl rand -hex 8 2>/dev/null || head -c 8 /dev/urandom | od -A n -t x1 | tr -d ' \n')"
    if command -v python3 >/dev/null 2>&1; then
      HASHED="$(python3 -c "
import hashlib, os
pw = '${ADMIN_PLAIN}'
salt = os.urandom(16)
dk = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt, 240000)
print(f'pbkdf2_sha256\$240000\${salt.hex()}\${dk.hex()}')
" 2>/dev/null || true)"
      if [ -n "${HASHED:-}" ]; then
        # Use python to replace line safely (handles $ in hash)
        python3 - "$HASHED" <<'PY'
import pathlib, sys
h = sys.argv[1]
p = pathlib.Path(".env")
t = p.read_text()
# replace CHANGE_ME or empty value
if "ADMIN_PASSWORD_HASH=CHANGE_ME" in t:
    t = t.replace("ADMIN_PASSWORD_HASH=CHANGE_ME", f"ADMIN_PASSWORD_HASH={h}")
elif "ADMIN_PASSWORD_HASH=" in t:
    # handle empty ADMIN_PASSWORD_HASH= line
    import re
    t = re.sub(r'^ADMIN_PASSWORD_HASH=.*$', f'ADMIN_PASSWORD_HASH={h}', t, flags=re.MULTILINE)
else:
    t += f"\nADMIN_PASSWORD_HASH={h}\n"
p.write_text(t)
PY
        echo "✓ Generated ADMIN_PASSWORD_HASH (admin password: ${ADMIN_PLAIN})"
        _admin_email_tmp="$(grep -E '^ADMIN_EMAIL=' .env | cut -d= -f2- | awk '{print $1}' | tail -n1 || echo 'admin@example.com')"
        echo "  → Save securely: admin email=${_admin_email_tmp}  password=${ADMIN_PLAIN}"
        unset HASHED _admin_email_tmp
      else
        echo "⚠️  Python password-hash generation failed — set ADMIN_PASSWORD_HASH manually" >&2
      fi
    else
      echo "⚠️  python3 missing — ADMIN_PASSWORD_HASH could not be generated, add manually" >&2
    fi
    unset ADMIN_PLAIN
  fi
fi

# Also handle case where ADMIN_PASSWORD_HASH key is missing entirely (old .env)
if ! grep -q "^ADMIN_PASSWORD_HASH=" .env; then
  ADMIN_PLAIN="$(openssl rand -hex 8 2>/dev/null || head -c 8 /dev/urandom | od -A n -t x1 | tr -d ' \n')"
  if command -v python3 >/dev/null 2>&1; then
    HASHED="$(python3 -c "
import hashlib, os
pw = '${ADMIN_PLAIN}'
salt = os.urandom(16)
dk = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt, 240000)
print(f'pbkdf2_sha256\$240000\${salt.hex()}\${dk.hex()}')
" 2>/dev/null || true)"
    python3 - "$HASHED" <<'PY'
import pathlib, sys
h = sys.argv[1]
p = pathlib.Path(".env")
t = p.read_text()
t = t.rstrip() + f"\nADMIN_PASSWORD_HASH={h}\n"
p.write_text(t)
PY
    echo "✓ Generated ADMIN_PASSWORD_HASH (admin password: ${ADMIN_PLAIN})"
    unset HASHED
  fi
  unset ADMIN_PLAIN
fi

# Sync DATABASE_URL password if it still contains CHANGE_ME (keep DB URL consistent with POSTGRES_PASSWORD)
if grep -q "DATABASE_URL=.*CHANGE_ME" .env; then
  _pw="$(grep -E '^POSTGRES_PASSWORD=' .env | cut -d= -f2- | awk '{print $1}' | tail -n1 || true)"
  if [ -n "${_pw:-}" ] && [ "${_pw}" != "CHANGE_ME" ]; then
    sed -i "s|postgresql+psycopg://lumi:CHANGE_ME@|postgresql+psycopg://lumi:${_pw}@|" .env || true
    if grep -q "DATABASE_URL=.*CHANGE_ME" .env; then
      sed -i "/^DATABASE_URL=/ s/CHANGE_ME/${_pw}/" .env || true
    fi
    echo "✓ Synced DATABASE_URL with POSTGRES_PASSWORD"
  fi
  unset _pw
fi

# Ensure DATABASE_URL also handled for asyncpg variant if present
if grep -q "DATABASE_URL=.*CHANGE_ME" .env; then
  _pw="$(grep -E '^POSTGRES_PASSWORD=' .env | cut -d= -f2- | awk '{print $1}' | tail -n1 || true)"
  if [ -n "${_pw:-}" ] && [ "${_pw}" != "CHANGE_ME" ]; then
    sed -i "/^DATABASE_URL=/ s/CHANGE_ME/${_pw}/g" .env || true
    echo "✓ Synced DATABASE_URL (fallback)"
  fi
  unset _pw
fi

echo "→ docker compose up -d --build"
docker compose up -d --build

if [ -x ./scripts/secret-scan.sh ]; then
  ./scripts/secret-scan.sh . || true
else
  echo "⚠️  scripts/secret-scan.sh not found or not executable — skipping scan" >&2
fi

# Clear next steps
ADMIN_EMAIL_VAL="$(grep -E '^ADMIN_EMAIL=' .env | cut -d= -f2- | awk '{print $1}' | tail -n1 || echo 'admin@example.com')"
if [ -z "$ADMIN_EMAIL_VAL" ] || [ "$ADMIN_EMAIL_VAL" = "CHANGE_ME" ]; then
  ADMIN_EMAIL_VAL="admin@example.com"
fi
echo ""
echo "════════════════════════════════════════════════════"
echo "✅ LUMI ready!"
echo "→ http://localhost:3525  admin: ${ADMIN_EMAIL_VAL}  logs: docker compose logs -f"
echo "→ http://localhost:3525"
echo "  admin: ${ADMIN_EMAIL_VAL}  (password: .env → ADMIN_PASSWORD_HASH — shown above during initial setup)"
echo "  no key needed with mock LLM; for OpenAI/OpenRouter/Ollama fill .env → LLM_API_KEY"
echo "  logs: docker compose logs -f"
echo "  health: curl -s http://localhost:3525/health/ready | jq"
echo "  secret scan: ./scripts/secret-scan.sh .  (no secret info in repo — verify with ./scripts/secret-scan.sh)"
echo "════════════════════════════════════════════════════"
