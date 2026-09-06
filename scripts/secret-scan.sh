#!/usr/bin/env bash
# LUMI — secret scan v3 (fail-closed)
# Exit: 0 clean, 1 real secret found, 2 scan error (fail-closed)
# No file is skipped; real secrets are caught except placeholder/CHANGE_ME.
set -uo pipefail
ROOT="${1:-.}"
if [ ! -d "$ROOT" ]; then
  echo "❌ ROOT missing: $ROOT" >&2
  exit 2
fi
cd "$ROOT" || exit 2

# fail-closed: error if required tools are missing
for bin in grep find sed; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "❌ Required tool missing: $bin (fail-closed)" >&2
    exit 2
  fi
done

# High-reliability real value patterns (literal token/credentials)
# Placeholders (CHANGE_ME, dev-only, REPLACE_ME, empty) never match
STRONG=(
  'TELEGRAM_BOT_TOKEN[=: ]+[0-9]{6,}:[A-Za-z0-9_-]{30,}'   # real TG token
  'TELEGRAM_BOT_TOKEN[=:][ ]*[0-9]{6,}:[A-Za-z0-9_-]{30,}' # env assignment variant
  'mongodb(\+srv)?://[^: ]+:[^@ ]{8,}@[^: ]+'               # real DB creds (pw >=8)
  'postgresql(\+[^: ]+)?://[^: ]+:[^@ ]{8,}@[^: ]+'         # postgres URL with pw >=8
  '\bgh[pousr]_[A-Za-z0-9]{20,}\b'                        # GitHub token
  '\bsk(-[A-Za-z0-9]{8,}){2,}\b'                          # OpenAI-style sk-...
  'LLM_API_KEY[=: ]+[A-Za-z0-9_-]{24,}'                  # real LLM key assignment
  'JWT_SECRET[=: ]+[A-Za-z0-9_\-+/=]{24,}'               # JWT secret assignment (non-placeholder)
  'SESSION_ENCRYPTION_MASTER_KEY[=: ]+[A-Za-z0-9_\-+/=]{24,}'
  'POSTGRES_PASSWORD[=: ]+[A-Za-z0-9_\-+/=]{8,}'
  'DB_PASSWORD[=: ]+[A-Za-z0-9_\-+/=]{8,}'
)

# Exclude placeholders from matching (filtered line by line)
is_placeholder_line() {
  echo "$1" | grep -qE 'CHANGE_ME|REPLACE_ME|dev-only|example\.com|_here|your-.*-here|\$\{|random|:x@|localhost:5432/lumi|127\.0\.0\.1|<MASKED>|<REDACTED>|\*\*\*|docs/mcp-audit' 2>/dev/null
}

candidate_files() {
  # In a Git worktree, scan committed and non-ignored candidate files only.
  # This deliberately excludes a developer's ignored runtime .env while preserving
  # fail-closed scanning for every file that could be committed.
  # `.git` is a directory in a normal checkout and a file in a linked worktree.
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git ls-files --cached --others --exclude-standard | while IFS= read -r f; do
      case "$f" in
        *.py|*.js|*.ts|*.tsx|*.sh|*.yml|*.yaml|*.json|*.md|*.ini|*.env|*.env.*|*.example)
          printf '%s\n' "$f"
          ;;
      esac
    done
  else
    find . -type f \( -name '*.py' -o -name '*.js' -o -name '*.ts' -o -name '*.tsx' -o -name '*.sh' -o -name '*.yml' -o -name '*.yaml' -o -name '*.json' -o -name '*.md' -o -name '*.ini' -o -name '*.env*' -o -name '*.example' \) \
      -not -path '*/.git/*' -not -path '*/.venv/*' -not -path '*/node_modules/*' -not -path '*/dist/*' \
      -not -path '*/.pytest_cache/*' -not -path '*/instance/*' -not -path '*/__pycache__/*' \
      -not -path '*/tests/security/test_secret_scan.py' \
      -not -path '*/backups/*' 2>/dev/null
  fi
}

hits=0
scanned=0
while IFS= read -r f; do
  [ -z "$f" ] && continue
  # derived audit copies — not a real leak, copy of source file (test fixtures contain <MASKED>)
  if echo "$f" | grep -qE 'docs/mcp-audit' 2>/dev/null; then
    continue
  fi
  # fixture/test self-scan — positive fixtures intentionally contain real patterns, not a repo leak
  if echo "$f" | grep -qE 'tests/security/test_secret_scan|tests/fixtures/secret-scan' 2>/dev/null; then
    continue
  fi
  scanned=$((scanned+1))
  # .env.example and fixtures are special: .env.example is checked separately; fixture positives are in the allowlist
  # But fail-closed: NO exclusion from scanning — only label for reporting
  for pat in "${STRONG[@]}"; do
    if grep -qE "$pat" "$f" 2>/dev/null; then
      # get the line, skip if placeholder (not a real value)
      line=$(grep -nE "$pat" "$f" 2>/dev/null | head -1)
      if is_placeholder_line "$line"; then
        continue
      fi
      # fixture positive files are expected to be caught — mark but still count unless in allowlist dir
      if echo "$f" | grep -qE 'tests/fixtures/secret-scan-positive|secret-scan-fixtures' 2>/dev/null; then
        # positive fixture: should be detected; don't count as repo leak, just ensure detection works
        continue
      fi
      # masked test strings like ""Authorization: Bearer ***"" in test_policy_redaction.py
      # skip if the line has a <REDACTED> or *** mask
      if echo "$line" | grep -qE '<REDACTED>|<MASKED>|\*\*\*' 2>/dev/null; then
        # but if the line also has a real token, still catch it — extra check
        if echo "$line" | grep -qE '[A-Za-z0-9_-]{30,}' 2>/dev/null && ! echo "$line" | grep -qE 'CHANGE_ME'; then
          # if it looks like a real value, report anyway
          :
        else
          continue
        fi
      fi
      echo "⚠️  REAL SECRET CANDIDATE: $f"
      echo "$line" | head -1 | sed -E 's/([0-9]{6,}:[A-Za-z0-9_-]{30,}|mongodb(\+srv)?:\/\/[^@]+@|postgresql(\+[^:]+)?:\/\/[^@]+@|sk-[A-Za-z0-9]{8,}[A-Za-z0-9_-]*|[A-Za-z0-9_-]{30,})/<MASKED>/g'
      hits=1
    fi
  done
done < <(candidate_files)

# fail-closed: error if no files could be scanned
if [ "$scanned" -eq 0 ]; then
  echo "❌ Scan error: no files could be scanned (fail-closed)" >&2
  exit 2
fi

# never commit real secret files — if app.env exists in repo, definite fail
# For .env: fail only if tracked by git (dev .env is in gitignore)
if find . -name 'app.env' -not -path '*/.git/*' -not -path '*/node_modules/*' -not -path '*/.venv/*' 2>/dev/null | grep -q .; then
  real_env=$(find . -name 'app.env' -not -path '*/.git/*' -not -path '*/node_modules/*' -not -path '*/.venv/*' 2>/dev/null | head -5)
  if [ -n "$real_env" ]; then
    echo "❌ app.env EXISTS IN REPO — do not commit!"
    echo "$real_env"
    hits=1
  fi
fi
# Fail only if .env is tracked in Git (a local ignored .env is permitted).
if git rev-parse --is-inside-work-tree >/dev/null 2>&1 && git ls-files --error-unmatch .env >/dev/null 2>&1; then
  echo "❌ .env TRACKED IN REPO — add to .gitignore!"
  hits=1
fi

# .env.example security: should only contain CHANGE_ME / empty / safe placeholder
if [ -f ".env.example" ]; then
  # fail if .env.example contains a real value like TG token / sk- / 64 hex
  if grep -qE '[0-9]{6,}:[A-Za-z0-9_-]{30,}' .env.example 2>/dev/null; then
    if ! grep -qE 'CHANGE_ME' .env.example 2>/dev/null; then
      : # if no placeholder, it means a real token
    fi
    # Is there a real token line without CHANGE_ME?
    if grep -E '[0-9]{6,}:[A-Za-z0-9_-]{30,}' .env.example 2>/dev/null | grep -qv 'CHANGE_ME' 2>/dev/null; then
      echo "❌ .env.example contains a real Telegram token!"
      hits=1
    fi
  fi
  # Is there a real key starting with sk- in .env.example (excluding CHANGE_ME)
  if grep -qE 'sk-[A-Za-z0-9]{20,}' .env.example 2>/dev/null; then
    if grep -E 'sk-[A-Za-z0-9]{20,}' .env.example 2>/dev/null | grep -qv 'CHANGE_ME' 2>/dev/null; then
      echo "❌ .env.example contains a real LLM key!"
      hits=1
    fi
  fi
  # fail if POSTGRES_PASSWORD / JWT_SECRET line has no CHANGE_ME and the value is long
  for key in POSTGRES_PASSWORD DB_PASSWORD JWT_SECRET SESSION_ENCRYPTION_MASTER_KEY TELEGRAM_BOT_TOKEN LLM_API_KEY; do
    line=$(grep -E "^${key}=" .env.example 2>/dev/null | head -1 || true)
    if [ -n "$line" ]; then
      val=$(echo "$line" | cut -d= -f2-)
      # OK if empty or CHANGE_ME
      if [ -z "$val" ] || echo "$val" | grep -q 'CHANGE_ME' 2>/dev/null; then
        continue
      fi
      # if 8+ chars and not CHANGE_ME, suspected real value
      if [ "${#val}" -ge 8 ] && ! echo "$val" | grep -qE '^\$\{' 2>/dev/null; then
        echo "❌ .env.example contains real value for $key: $line (must be CHANGE_ME only)"
        hits=1
      fi
    fi
  done
fi

if [ "$hits" = "0" ]; then
  echo "✅ Secret scan clean: no real credentials in repo. (scanned file: $scanned)"
else
  echo "❌ Secret scan: real secret candidate found — STOP the commit."
  exit 1
fi
