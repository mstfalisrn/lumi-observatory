#!/usr/bin/env bash
# LUMI — secret scan v4.1 (fail-closed, history-aware, single-pass)
#
# Usage:  ./scripts/secret-scan.sh [ROOT] [--history]
# Exit:   0 clean · 1 real secret candidate found · 2 scan error (fail-closed)
#
# v4 closes the false-negative gaps of v3:
#   * one combined pass per file — every matching line AND every matched token
#     is evaluated, so a placeholder on a line cannot mask a real value beside
#     it, and a placeholder line does not hide later real matches
#   * placeholder filtering is token-level, not line-level
#   * read errors abort the scan (fail-closed)
#   * archives (.docx/.xlsx/.zip) are extracted and their text scanned
#   * --history scans every blob in every ref through a single git grep pass
set -uo pipefail

ROOT="."
HISTORY=0
for arg in "$@"; do
  case "$arg" in
    --history) HISTORY=1 ;;
    *) ROOT="$arg" ;;
  esac
done

if [ ! -d "$ROOT" ]; then
  echo "❌ ROOT missing: $ROOT" >&2
  exit 2
fi
cd "$ROOT" || exit 2

REQUIRED=(grep find sed awk cut)
if [ "$HISTORY" = "1" ]; then
  REQUIRED+=(git)
fi
for bin in "${REQUIRED[@]}"; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "❌ Required tool missing: $bin (fail-closed)" >&2
    exit 2
  fi
done

# High-reliability real value patterns. Placeholders are filtered per TOKEN.
STRONG=(
  'TELEGRAM_BOT_TOKEN[=: ]+[0-9]{6,}:[A-Za-z0-9_-]{30,}'
  '[0-9]{8,10}:[A-Za-z0-9_-]{35}'                            # bare TG bot token shape
  'mongodb(\+srv)?://[^: ]+:[^@ ]{8,}@[^: ]+'                # DB URL with real-looking pw
  'postgresql(\+[^: ]+)?://[^: ]+:[^@ ]{8,}@[^: ]+'
  '\bgh[pousr]_[A-Za-z0-9]{20,}\b'                           # GitHub token
  '\bglpat-[A-Za-z0-9_-]{20,}\b'                             # GitLab token
  '\bsk(-[A-Za-z0-9]{8,}){2,}\b'                             # OpenAI-style
  '\bsk-ant-[A-Za-z0-9_-]{20,}\b'                            # Anthropic
  '\bxox[baprs]-[A-Za-z0-9-]{10,}\b'                         # Slack
  '\bAIza[0-9A-Za-z_-]{35}\b'                                # Google API key
  '\bhf_[A-Za-z0-9]{30,}\b'                                  # HuggingFace
  '\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b'  # JWT
  'LLM_API_KEY[=:][ ]*[A-Za-z0-9_-]{24,}'
  'JWT_SECRET[=:][ ]*[A-Za-z0-9_+/=-]{24,}'
  'SESSION_ENCRYPTION_MASTER_KEY[=:][ ]*[A-Za-z0-9_+/=-]{24,}'
  'POSTGRES_PASSWORD[=:][ ]*[A-Za-z0-9_+/=-]{8,}'
  'DB_PASSWORD[=:][ ]*[A-Za-z0-9_+/=-]{8,}'
  '[-]{5}BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY[-]{5}'
)

COMBINED=""
for pat in "${STRONG[@]}"; do
  COMBINED="${COMBINED:+$COMBINED|}(${pat})"
done

# Paths whose whole purpose is to contain synthetic positives.
EXCLUDE_RE='(tests/security/test_secret_scan|tests/security/test_policy_redaction|tests/unit/test_security_redact|tests/fixtures/secret-scan|docs/mcp-audit|\.git/)'

# A matched TOKEN is a placeholder when it is clearly not a live credential.
is_placeholder_token() {
  local t="$1"
  case "$t" in
    *CHANGE_ME*|*REPLACE_ME*|*change-me*|*dev-only*|*example*|*EXAMPLE*|*REDACTED*|*MASKED*|*'***'*) return 0 ;;
    *'${'*|*'{'*|*'<'*'>'*|*your-*|*_here*) return 0 ;;
    *random*|*localhost*|*127.0.0.1*|*lumi-postgres:5432*) return 0 ;;
  esac
  # heuristic: a value made almost entirely of one repeated character
  if printf '%s' "$t" | grep -qE '^(.)\1{15,}$'; then return 0; fi
  return 1
}

mask_line() {
  printf '%s\n' "$1" | cut -c1-220 | sed -E 's/([0-9]{6,}:[A-Za-z0-9_-]{30,}|mongodb(\+srv)?:\/\/[^@]+@|postgresql(\+[^:]+)?:\/\/[^@]+@|sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{10,}|glpat-[A-Za-z0-9_-]{10,}|xox[baprs]-[A-Za-z0-9-]{6,}|AIza[0-9A-Za-z_-]{10,}|hf_[A-Za-z0-9]{10,}|eyJ[A-Za-z0-9_.-]{20,}|[A-Za-z0-9_-]{30,})/<MASKED>/g'
}

# Evaluate one line: all matched tokens, token-level placeholder filter.
# Returns 0 when a real token was found (and reports it).
check_line() {
  local label="$1" line="$2"
  local tokens real tok
  tokens=$(printf '%s' "$line" | grep -oaE "$COMBINED" 2>/dev/null) || true
  [ -z "$tokens" ] && return 1
  real=""
  while IFS= read -r tok; do
    [ -z "$tok" ] && continue
    if ! is_placeholder_token "$tok"; then real="$tok"; break; fi
  done <<< "$tokens"
  if [ -n "$real" ]; then
    echo "⚠️  REAL SECRET CANDIDATE: $label"
    mask_line "$line"
    HITS=1
    return 0
  fi
  return 1
}

# Scan a text stream: ONE combined pass, every matching line evaluated.
scan_stream() {
  local label="$1"
  local lineno=0 line
  while IFS= read -r line || [ -n "$line" ]; do
    lineno=$((lineno+1))
    if printf '%s' "$line" | grep -qaE "$COMBINED" 2>/dev/null; then
      check_line "$label:$lineno" "$line" || true
    fi
  done
}

candidate_files() {
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git ls-files --cached --others --exclude-standard | while IFS= read -r f; do
      case "$f" in
        *.py|*.js|*.ts|*.tsx|*.sh|*.yml|*.yaml|*.json|*.md|*.ini|*.env|*.env.*|*.example|*.txt|*.toml)
          printf '%s\n' "$f" ;;
        *.docx|*.xlsx|*.zip)
          printf 'ARCHIVE:%s\n' "$f" ;;
      esac
    done
  else
    find . -type f \( -name '*.py' -o -name '*.js' -o -name '*.ts' -o -name '*.tsx' -o -name '*.sh' \
      -o -name '*.yml' -o -name '*.yaml' -o -name '*.json' -o -name '*.md' -o -name '*.ini' \
      -o -name '*.env*' -o -name '*.example' -o -name '*.txt' -o -name '*.toml' \) \
      -not -path '*/.git/*' -not -path '*/.venv/*' -not -path '*/node_modules/*' -not -path '*/dist/*' \
      -not -path '*/.pytest_cache/*' -not -path '*/instance/*' -not -path '*/__pycache__/*' 2>/dev/null | \
      while IFS= read -r f; do printf '%s\n' "$f"; done
    find . -type f \( -name '*.docx' -o -name '*.xlsx' -o -name '*.zip' \) \
      -not -path '*/.git/*' 2>/dev/null | while IFS= read -r f; do printf 'ARCHIVE:%s\n' "$f"; done
  fi
}

HITS=0
scanned=0
scan_error=0

while IFS= read -r f; do
  [ -z "$f" ] && continue
  is_archive=0
  case "$f" in ARCHIVE:*) is_archive=1; f="${f#ARCHIVE:}" ;; esac
  if printf '%s' "$f" | grep -qE "$EXCLUDE_RE"; then continue; fi

  if [ "$is_archive" = "1" ]; then
    if ! command -v unzip >/dev/null 2>&1; then
      echo "❌ archive present but unzip missing: $f (fail-closed)" >&2
      scan_error=1
      continue
    fi
    scanned=$((scanned+1))
    scan_stream "$f (archive)" < <(unzip -p "$f" 2>/dev/null) || true
    continue
  fi

  if [ ! -r "$f" ]; then
    echo "❌ unreadable file: $f (fail-closed)" >&2
    scan_error=1
    continue
  fi
  scanned=$((scanned+1))
  # fast path: skip files with no possible hit in one grep (fail-closed on read errors)
  grep -qaE "$COMBINED" "$f" 2>/dev/null
  rc=$?
  if [ "$rc" -ge 2 ]; then
    echo "❌ read error: $f (fail-closed)" >&2
    scan_error=1
    continue
  fi
  if [ "$rc" -eq 1 ]; then
    continue
  fi
  scan_stream "$f" < "$f"
done < <(candidate_files)

# ---------------------------------------------------------------- history mode
if [ "$HISTORY" = "1" ]; then
  if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "❌ --history requires a git worktree" >&2
    exit 2
  fi
  revs=$(git rev-list --all 2>/dev/null)
  if [ -z "$revs" ]; then
    echo "❌ history scan: no refs (fail-closed)" >&2
    exit 2
  fi
  echo "── history scan: $(printf '%s\n' "$revs" | wc -l) commits ──"
  while IFS= read -r hit; do
    [ -z "$hit" ] && continue
    rest="${hit#*:}"        # path:line:content
    path="${rest%%:*}"
    case "$path" in
      tests/security/test_secret_scan*|tests/security/test_policy_redaction*|tests/unit/test_security_redact*|tests/fixtures/secret-scan*|docs/mcp-audit*) continue ;;
    esac
    body="${rest#*:}"
    check_line "history: $path" "$body" || true
  done < <(git grep -nE -I "$COMBINED" $revs -- 2>/dev/null)
  # secret-bearing filenames must never have been added
  added=$(git log --all --diff-filter=A --name-only --pretty=format: 2>/dev/null | sort -u)
  if printf '%s\n' "$added" | grep -qE '(^|/)(\.env|\.env\.(bak|local|production|staging)[^/]*|app\.env)$'; then
    echo "❌ history: an env file was committed at some point:"
    printf '%s\n' "$added" | grep -E '(^|/)(\.env|\.env\.(bak|local|production|staging)[^/]*|app\.env)$' | head -5
    HITS=1
  fi
fi

# fail-closed: no files scanned is an error
if [ "$scanned" -eq 0 ]; then
  echo "❌ Scan error: no files could be scanned (fail-closed)" >&2
  exit 2
fi
if [ "$scan_error" -ne 0 ]; then
  echo "❌ Scan error: unreadable input (fail-closed)" >&2
  exit 2
fi

# never commit real secret files — if app.env exists in repo, definite fail
if find . -name 'app.env' -not -path '*/.git/*' -not -path '*/node_modules/*' -not -path '*/.venv/*' 2>/dev/null | grep -q .; then
  echo "❌ app.env EXISTS IN REPO — do not commit!"
  find . -name 'app.env' -not -path '*/.git/*' -not -path '*/node_modules/*' -not -path '*/.venv/*' 2>/dev/null | head -5
  HITS=1
fi
if git rev-parse --is-inside-work-tree >/dev/null 2>&1 && git ls-files --error-unmatch .env >/dev/null 2>&1; then
  echo "❌ .env TRACKED IN REPO — add to .gitignore!"
  HITS=1
fi

# .env.example: only CHANGE_ME / empty / safe placeholders
if [ -f ".env.example" ]; then
  for key in POSTGRES_PASSWORD DB_PASSWORD JWT_SECRET SESSION_ENCRYPTION_MASTER_KEY TELEGRAM_BOT_TOKEN LLM_API_KEY LOGS_AUTH_TOKEN; do
    line=$(grep -E "^${key}=" .env.example 2>/dev/null | head -1 || true)
    if [ -n "$line" ]; then
      val=$(echo "$line" | cut -d= -f2-)
      if [ -z "$val" ] || echo "$val" | grep -q 'CHANGE_ME' 2>/dev/null; then
        continue
      fi
      if [ "${#val}" -ge 8 ] && ! echo "$val" | grep -qE '^\$\{' 2>/dev/null; then
        echo "❌ .env.example contains real value for $key: $line (must be CHANGE_ME only)"
        HITS=1
      fi
    fi
  done
  if grep -E '[0-9]{8,10}:[A-Za-z0-9_-]{35}' .env.example 2>/dev/null | grep -qv 'CHANGE_ME' 2>/dev/null; then
    echo "❌ .env.example contains a real Telegram token!"
    HITS=1
  fi
  if grep -E 'sk-[A-Za-z0-9]{20,}' .env.example 2>/dev/null | grep -qv 'CHANGE_ME' 2>/dev/null; then
    echo "❌ .env.example contains a real LLM key!"
    HITS=1
  fi
fi

if [ "$HITS" = "0" ]; then
  echo "✅ Secret scan clean: no real credentials in repo. (scanned file: $scanned)"
  exit 0
else
  echo "❌ Secret scan: real secret candidate found — STOP the commit."
  exit 1
fi
