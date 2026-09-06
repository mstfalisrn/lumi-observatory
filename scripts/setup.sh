#!/usr/bin/env bash
# LUMI Agentic Observatory — interactive setup wizard
# Usage:
#   ./scripts/setup.sh              # interactive, step-by-step
#   ./scripts/setup.sh --reconfigure # re-run to edit existing .env
#   ./scripts/setup.sh --help
#   ./scripts/setup.sh --yes        # non-interactive (CI) — keep CHANGE_ME auto-generation
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT}"

# ---- args ----
RECONFIGURE=false
NONINTERACTIVE=false
for arg in "$@"; do
  case "$arg" in
    --reconfigure) RECONFIGURE=true ;;
    --yes|--non-interactive) NONINTERACTIVE=true ;;
    --help|-h)
      echo "LUMI interactive setup wizard"
      echo ""
      echo "Usage:"
      echo "  ./scripts/setup.sh                # step-by-step wizard (recommended)"
      echo "  ./scripts/setup.sh --reconfigure  # edit existing .env again"
      echo "  ./scripts/setup.sh --yes          # non-interactive (CI) — auto-generate CHANGE_ME"
      echo "  ./scripts/setup.sh --help"
      echo ""
      echo "What it does:"
      echo "  1) Creates .env from .env.example if missing"
      echo "  2) Walks you through: Admin -> LLM Provider -> Telegram -> Security secrets"
      echo "  3) Writes .env, runs docker compose up -d --build, and verifies with secret-scan"
      echo ""
      echo "Re-run anytime to fix a value:"
      echo "  ./scripts/setup.sh --reconfigure"
      echo "  nano .env && docker compose up -d --build   # manual edit"
      exit 0
      ;;
  esac
done

# If not a TTY, force non-interactive
if [ ! -t 0 ] && [ "$NONINTERACTIVE" = false ]; then
  NONINTERACTIVE=true
fi

# ---- colors (disable if not tty) ----
if [ -t 1 ]; then
  BOLD="\033[1m"; DIM="\033[2m"; GREEN="\033[32m"; CYAN="\033[36m"; YELLOW="\033[33m"; RED="\033[31m"; RESET="\033[0m"
else
  BOLD=""; DIM=""; GREEN=""; CYAN=""; YELLOW=""; RED=""; RESET=""
fi

banner() {
  echo ""
  echo -e "${BOLD}════════════════════════════════════════════════════${RESET}"
  echo -e "${BOLD}  LUMI Agentic Observatory — Setup Wizard${RESET}"
  echo -e "${DIM}  Step-by-step: you choose every value. Re-run anytime with --reconfigure.${RESET}"
  echo -e "${BOLD}════════════════════════════════════════════════════${RESET}"
  echo ""
}

# ---- helpers ----
gen_hex() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  elif [ -r /dev/urandom ]; then
    head -c 32 /dev/urandom | od -A n -t x1 | tr -d ' \n'
  else
    echo "ERROR: no openssl or /dev/urandom" >&2; exit 1
  fi
}

hash_password() {
  local pw="$1"
  python3 -c "
import hashlib, os
pw = '''${pw}'''.replace(\"'\", \"''\")  # not perfect but pw from read is safe
import sys
pw = sys.argv[1]
salt = os.urandom(16)
dk = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt, 240000)
print(f'pbkdf2_sha256\$240000\${salt.hex()}\${dk.hex()}')
" "$pw"
}

get_env_val() {
  local key="$1"
  if [ -f .env ]; then
    grep -E "^${key}=" .env 2>/dev/null | tail -n1 | cut -d= -f2- | sed 's/ #.*//' | tr -d '\r' | xargs 2>/dev/null || true
  fi
}

set_env_val() {
  local key="$1" val="$2"
  # Use python for safe replacement (handles $ and special chars)
  python3 - "$key" "$val" <<'PY'
import pathlib, sys, re
k, v = sys.argv[1], sys.argv[2]
p = pathlib.Path(".env")
t = p.read_text() if p.exists() else ""
# escape $ for regex replacement? we build literal line
new_line = f"{k}={v}"
if re.search(rf'^{re.escape(k)}=.*$', t, flags=re.MULTILINE):
    t = re.sub(rf'^{re.escape(k)}=.*$', new_line, t, flags=re.MULTILINE)
else:
    t = t.rstrip() + f"\n{new_line}\n"
p.write_text(t)
PY
}

mask() {
  local v="$1"
  if [ -z "$v" ] || [ "$v" = "CHANGE_ME" ]; then echo "CHANGE_ME"; return; fi
  local len=${#v}
  if [ "$len" -le 8 ]; then echo "***"; else echo "${v:0:4}****${v: -4}"; fi
}

ask() {
  local prompt="$1" default="$2" varname="$3"
  local answer
  if [ "$NONINTERACTIVE" = true ]; then
    printf -v "$varname" "%s" "$default"
    return
  fi
  if [ -n "$default" ] && [ "$default" != "CHANGE_ME" ]; then
    echo -ne "${CYAN}${prompt} ${DIM}[${default}]${RESET}: "
  else
    echo -ne "${CYAN}${prompt}${RESET}: "
  fi
  read -r answer || true
  if [ -z "$answer" ]; then
    answer="$default"
  fi
  printf -v "$varname" "%s" "$answer"
}

ask_secret() {
  local prompt="$1" varname="$2"
  local answer
  if [ "$NONINTERACTIVE" = true ]; then
    printf -v "$varname" "%s" ""
    return
  fi
  echo -ne "${CYAN}${prompt} ${DIM}(hidden, leave empty to keep)${RESET}: "
  read -rs answer || true
  echo ""
  printf -v "$varname" "%s" "$answer"
}

# ---- ensure .env ----
if [ ! -f .env ]; then
  if [ ! -f .env.example ]; then
    echo -e "${RED}ERROR: .env.example not found${RESET}" >&2; exit 1
  fi
  cp .env.example .env
  echo -e "${GREEN}✓ Created .env from .env.example${RESET}"
else
  if [ "$RECONFIGURE" = true ]; then
    echo -e "${YELLOW}↻ Reconfiguring existing .env — current values shown as [defaults]. Press Enter to keep, or type a new value.${RESET}"
  else
    echo -e "${DIM}Found existing .env — will show current values as defaults. Use --reconfigure to edit explicitly.${RESET}"
  fi
fi

if [ "$NONINTERACTIVE" = true ]; then
  echo -e "${YELLOW}Non-interactive mode — will auto-generate any remaining CHANGE_ME and start the stack.${RESET}"
  exec ./scripts/quickstart.sh
fi

banner

# ---- Step 1: Admin ----
echo -e "${BOLD}Step 1/4 — Admin account${RESET}  ${DIM}(used for Web UI login)${RESET}"
CUR_EMAIL="$(get_env_val ADMIN_EMAIL)"
[ -z "$CUR_EMAIL" ] || [ "$CUR_EMAIL" = "CHANGE_ME" ] && CUR_EMAIL="admin@example.com"
ask "Admin email" "$CUR_EMAIL" IN_EMAIL
set_env_val "ADMIN_EMAIL" "$IN_EMAIL"

# Password: only if user provides one, otherwise keep existing hash
CUR_HASH="$(get_env_val ADMIN_PASSWORD_HASH)"
if [ -n "$CUR_HASH" ] && [ "$CUR_HASH" != "CHANGE_ME" ]; then
  echo -e "${DIM}  Current ADMIN_PASSWORD_HASH is set (${CUR_HASH:0:20}...). Leave empty to keep it.${RESET}"
fi
ask_secret "Admin password" IN_PW
if [ -n "$IN_PW" ]; then
  if command -v python3 >/dev/null 2>&1; then
    NEW_HASH="$(python3 -c "
import hashlib, os, sys
pw = sys.argv[1]
salt = os.urandom(16)
dk = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt, 240000)
print(f'pbkdf2_sha256\$240000\${salt.hex()}\${dk.hex()}')
" "$IN_PW")"
    set_env_val "ADMIN_PASSWORD_HASH" "$NEW_HASH"
    echo -e "${GREEN}✓ Admin password hash updated${RESET}  ${DIM}(password: you just typed)${RESET}"
  else
    echo -e "${RED}python3 not found — cannot hash password${RESET}" >&2; exit 1
  fi
else
  if [ -z "$CUR_HASH" ] || [ "$CUR_HASH" = "CHANGE_ME" ]; then
    # generate random password and hash it
    RAND_PW="$(openssl rand -hex 8 2>/dev/null || head -c 8 /dev/urandom | od -A n -t x1 | tr -d ' \n')"
    NEW_HASH="$(python3 -c "
import hashlib, os, sys
pw = sys.argv[1]
salt = os.urandom(16)
dk = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt, 240000)
print(f'pbkdf2_sha256\$240000\${salt.hex()}\${dk.hex()}')
" "$RAND_PW")"
    set_env_val "ADMIN_PASSWORD_HASH" "$NEW_HASH"
    echo -e "${GREEN}✓ Generated admin password:${RESET} ${BOLD}${RAND_PW}${RESET}  ${DIM}(email: ${IN_EMAIL} — save now, shown once)${RESET}"
  else
    echo -e "${DIM}  Keeping existing ADMIN_PASSWORD_HASH${RESET}"
  fi
fi
echo ""

# ---- Step 2: LLM Provider ----
# Covers the full provider ecosystem via OpenAI-compatible presets.
# See docs/CONFIGURATION.md for the complete 40+ provider mapping.
echo -e "${BOLD}Step 2/4 — LLM Provider${RESET}  ${DIM}(18 presets incl. OpenCode Free/Go/Zen + Custom; all OpenAI-compatible; mock = free)${RESET}"
CUR_PROVIDER="$(get_env_val LLM_PROVIDER)"; [ -z "$CUR_PROVIDER" ] && CUR_PROVIDER="mock"
CUR_BASE="$(get_env_val LLM_BASE_URL)"; CUR_MODEL_DISP="$(get_env_val LLM_MODEL)"
echo -e "  ${DIM}Current: provider=${CUR_PROVIDER}  base=${CUR_BASE:- -}  model=${CUR_MODEL_DISP:- -}${RESET}"

# Checkbox-style selector: whiptail radiolist (SPACE to select, ENTER to confirm)
# Falls back to numeric input if whiptail not available or not a TTY.
choose_llm_with_checkbox() {
  local _cur="$1" _out
  # map current provider+base to preset number for pre-selection
  local _def="1"
  case "${CUR_PROVIDER}:${CUR_BASE}" in
    mock:*) _def="1" ;;
    *:api.openai.com/v1) _def="2" ;;
    *:openrouter.ai*) _def="3" ;;
    *:api.deepseek.com*) _def="4" ;;
    *:api.x.ai*) _def="5" ;;
    *:generativelanguage.googleapis.com*) _def="6" ;;
    *:dashscope*.aliyuncs.com*) _def="7" ;;
    *:api.minimax.chat*) _def="8" ;;
    *:api.moonshot.cn*) _def="9" ;;
    *:api.fireworks.ai*) _def="10" ;;
    *:router.huggingface.co*|*:huggingface.co*) _def="11" ;;
    *:11434/v1) _def="12" ;;
    *:1234/v1) _def="13" ;;
    *:8000/v1) _def="14" ;;
    *:opencode.ai*free*|*:opencode-free*) _def="15" ;;
    *:opencode.ai*go*|*:opencode-go*) _def="16" ;;
    *:opencode.ai*zen*|*:opencode-zen*) _def="17" ;;
  esac
  _is_on() { [ "$_def" = "$1" ] && echo ON || echo OFF; }
  if command -v whiptail >/dev/null 2>&1 && [ -t 0 ] && [ -t 1 ]; then
    _out=$(whiptail --title "LUMI — LLM Provider" --radiolist "↑↓ to move  SPACE to select  ENTER to confirm" 22 78 11 \
      "1"  "Mock (free, no key) — fixtures" "$(_is_on 1)" \
      "2"  "OpenAI (api.openai.com)" "$(_is_on 2)" \
      "3"  "OpenRouter (300+ models)" "$(_is_on 3)" \
      "4"  "DeepSeek (api.deepseek.com)" "$(_is_on 4)" \
      "5"  "xAI Grok (api.x.ai)" "$(_is_on 5)" \
      "6"  "Google Gemini (generativelanguage)" "$(_is_on 6)" \
      "7"  "Alibaba Qwen / DashScope" "$(_is_on 7)" \
      "8"  "MiniMax (api.minimax.chat)" "$(_is_on 8)" \
      "9"  "Kimi / Moonshot (api.moonshot.cn)" "$(_is_on 9)" \
      "10"  "Fireworks AI (api.fireworks.ai)" "$(_is_on 10)" \
      "11"  "Hugging Face (router.huggingface.co)" "$(_is_on 11)" \
      "12"  "Ollama (local :11434/v1)" "$(_is_on 12)" \
      "13"  "LM Studio (local :1234/v1)" "$(_is_on 13)" \
      "14"  "vLLM / SGLang / llama.cpp (:8000/v1)" "$(_is_on 14)" \
      "15"  "OpenCode Free (keyless, opencode.ai)" "$(_is_on 15)" \
      "16"  "OpenCode Go (opencode.ai/zen/go)" "$(_is_on 16)" \
      "17"  "OpenCode Zen (opencode.ai/zen)" "$(_is_on 17)" \
      "18"  "Custom — any OpenAI-compatible URL" "$(_is_on 18)" \
      3>&1 1>&2 2>&3) || _out=""
    # whiptail returns with quotes, strip
    _out=$(echo "$_out" | tr -d '"')
    # pre-select default if user didn't change? whiptail needs default ON, we set via sed trick: if empty, use _def
    if [ -z "$_out" ]; then _out="$_def"; fi
    # map empty cancel to default
    echo "$_out"
  else
    echo "  1) Mock (free, no API key) — fixtures, no network"
    echo "  2) OpenAI (api.openai.com) — gpt-4o-mini, gpt-4o"
    echo "  3) OpenRouter (openrouter.ai) — 300+ models aggregator"
    echo "  4) DeepSeek (api.deepseek.com) — deepseek-chat / deepseek-reasoner"
    echo "  5) xAI Grok (api.x.ai) — grok-3-mini, grok-4"
    echo "  6) Google Gemini (generativelanguage.googleapis.com) — gemini-2.0-flash"
    echo "  7) Alibaba Qwen / DashScope — qwen-plus, qwen-max"
    echo "  8) MiniMax (api.minimax.chat) — MiniMax-M2, MiniMax-Text-01"
    echo "  9) Kimi / Moonshot (api.moonshot.cn) — moonshot-v1-8k/32k"
    echo " 10) Fireworks AI (api.fireworks.ai) — Llama, Mixtral, etc."
    echo " 11) Hugging Face Inference (router.huggingface.co)"
    echo " 12) Ollama (local) — http://host.docker.internal:11434/v1"
    echo " 13) LM Studio (local) — http://host.docker.internal:1234/v1"
    echo " 14) vLLM / SGLang / llama.cpp (self-hosted) — :8000/v1"
    echo " 15) OpenCode Free (keyless) — opencode.ai free tier"
    echo " 16) OpenCode Go (opencode.ai/zen/go) — go plan"
    echo " 17) OpenCode Zen (opencode.ai/zen) — zen plan"
    echo " 18) Custom — enter any OpenAI-compatible base URL"
    local _choice=""
    ask "Choose LLM [1-18]" "$_def" _choice
    echo "$_choice"
  fi
}
LLM_CHOICE="$(choose_llm_with_checkbox)" 
case "$LLM_CHOICE" in
  2)
    set_env_val "LLM_PROVIDER" "openai_compatible"
    set_env_val "LLM_BASE_URL" "https://api.openai.com/v1"
    CUR_MODEL="$(get_env_val LLM_MODEL)"; [ -z "$CUR_MODEL" ] && CUR_MODEL="gpt-4o-mini"
    ask "Model" "$CUR_MODEL" IN_MODEL
    set_env_val "LLM_MODEL" "$IN_MODEL"
    ask_secret "OpenAI API key (sk-...)" IN_KEY
    if [ -n "$IN_KEY" ]; then set_env_val "LLM_API_KEY" "$IN_KEY"; else echo -e "${YELLOW}  No key entered — you can set LLM_API_KEY in .env later and re-run with --reconfigure${RESET}"; fi
    ;;
  3)
    set_env_val "LLM_PROVIDER" "openai_compatible"
    set_env_val "LLM_BASE_URL" "https://openrouter.ai/api/v1"
    CUR_MODEL="$(get_env_val LLM_MODEL)"; [ -z "$CUR_MODEL" ] && CUR_MODEL="openai/gpt-4o-mini"
    ask "Model (e.g. openai/gpt-4o-mini, anthropic/claude-3.5-sonnet, google/gemini-2.0-flash)" "$CUR_MODEL" IN_MODEL
    set_env_val "LLM_MODEL" "$IN_MODEL"
    ask_secret "OpenRouter API key (sk-or-...)" IN_KEY
    if [ -n "$IN_KEY" ]; then set_env_val "LLM_API_KEY" "$IN_KEY"; else echo -e "${YELLOW}  No key entered — set LLM_API_KEY later${RESET}"; fi
    ;;
  4)
    set_env_val "LLM_PROVIDER" "openai_compatible"
    set_env_val "LLM_BASE_URL" "https://api.deepseek.com/v1"
    CUR_MODEL="$(get_env_val LLM_MODEL)"; [ -z "$CUR_MODEL" ] && CUR_MODEL="deepseek-chat"
    ask "Model (deepseek-chat, deepseek-reasoner)" "$CUR_MODEL" IN_MODEL
    set_env_val "LLM_MODEL" "$IN_MODEL"
    ask_secret "DeepSeek API key (sk-...)" IN_KEY
    if [ -n "$IN_KEY" ]; then set_env_val "LLM_API_KEY" "$IN_KEY"; else echo -e "${YELLOW}  No key entered — set LLM_API_KEY later${RESET}"; fi
    ;;
  5)
    set_env_val "LLM_PROVIDER" "openai_compatible"
    set_env_val "LLM_BASE_URL" "https://api.x.ai/v1"
    CUR_MODEL="$(get_env_val LLM_MODEL)"; [ -z "$CUR_MODEL" ] && CUR_MODEL="grok-3-mini"
    ask "Model (grok-3-mini, grok-4)" "$CUR_MODEL" IN_MODEL
    set_env_val "LLM_MODEL" "$IN_MODEL"
    ask_secret "xAI API key (xai-...)" IN_KEY
    if [ -n "$IN_KEY" ]; then set_env_val "LLM_API_KEY" "$IN_KEY"; else echo -e "${YELLOW}  No key entered — set LLM_API_KEY later${RESET}"; fi
    ;;
  6)
    set_env_val "LLM_PROVIDER" "openai_compatible"
    set_env_val "LLM_BASE_URL" "https://generativelanguage.googleapis.com/v1beta/openai/"
    CUR_MODEL="$(get_env_val LLM_MODEL)"; [ -z "$CUR_MODEL" ] && CUR_MODEL="gemini-2.0-flash"
    ask "Model (gemini-2.0-flash, gemini-1.5-pro)" "$CUR_MODEL" IN_MODEL
    set_env_val "LLM_MODEL" "$IN_MODEL"
    ask_secret "Google API key (AIza...)" IN_KEY
    if [ -n "$IN_KEY" ]; then set_env_val "LLM_API_KEY" "$IN_KEY"; else echo -e "${YELLOW}  No key entered — set LLM_API_KEY later${RESET}"; fi
    ;;
  7)
    set_env_val "LLM_PROVIDER" "openai_compatible"
    set_env_val "LLM_BASE_URL" "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    CUR_MODEL="$(get_env_val LLM_MODEL)"; [ -z "$CUR_MODEL" ] && CUR_MODEL="qwen-plus"
    ask "Model (qwen-plus, qwen-max, qwen-turbo)" "$CUR_MODEL" IN_MODEL
    set_env_val "LLM_MODEL" "$IN_MODEL"
    ask_secret "DashScope API key (sk-...)" IN_KEY
    if [ -n "$IN_KEY" ]; then set_env_val "LLM_API_KEY" "$IN_KEY"; else echo -e "${YELLOW}  No key entered — set LLM_API_KEY later${RESET}"; fi
    echo -e "${DIM}  CN endpoint alternative: https://dashscope.aliyuncs.com/compatible-mode/v1${RESET}"
    ;;
  8)
    set_env_val "LLM_PROVIDER" "openai_compatible"
    set_env_val "LLM_BASE_URL" "https://api.minimax.chat/v1"
    CUR_MODEL="$(get_env_val LLM_MODEL)"; [ -z "$CUR_MODEL" ] && CUR_MODEL="MiniMax-M2"
    ask "Model (MiniMax-M2, MiniMax-Text-01)" "$CUR_MODEL" IN_MODEL
    set_env_val "LLM_MODEL" "$IN_MODEL"
    ask_secret "MiniMax API key" IN_KEY
    if [ -n "$IN_KEY" ]; then set_env_val "LLM_API_KEY" "$IN_KEY"; else echo -e "${YELLOW}  No key entered — set LLM_API_KEY later${RESET}"; fi
    ;;
  9)
    set_env_val "LLM_PROVIDER" "openai_compatible"
    set_env_val "LLM_BASE_URL" "https://api.moonshot.cn/v1"
    CUR_MODEL="$(get_env_val LLM_MODEL)"; [ -z "$CUR_MODEL" ] && CUR_MODEL="moonshot-v1-8k"
    ask "Model (moonshot-v1-8k, moonshot-v1-32k)" "$CUR_MODEL" IN_MODEL
    set_env_val "LLM_MODEL" "$IN_MODEL"
    ask_secret "Moonshot/Kimi API key (sk-...)" IN_KEY
    if [ -n "$IN_KEY" ]; then set_env_val "LLM_API_KEY" "$IN_KEY"; else echo -e "${YELLOW}  No key entered — set LLM_API_KEY later${RESET}"; fi
    ;;
  10)
    set_env_val "LLM_PROVIDER" "openai_compatible"
    set_env_val "LLM_BASE_URL" "https://api.fireworks.ai/inference/v1"
    CUR_MODEL="$(get_env_val LLM_MODEL)"; [ -z "$CUR_MODEL" ] && CUR_MODEL="accounts/fireworks/models/llama-v3p1-8b-instruct"
    ask "Model" "$CUR_MODEL" IN_MODEL
    set_env_val "LLM_MODEL" "$IN_MODEL"
    ask_secret "Fireworks API key (fw_...)" IN_KEY
    if [ -n "$IN_KEY" ]; then set_env_val "LLM_API_KEY" "$IN_KEY"; else echo -e "${YELLOW}  No key entered — set LLM_API_KEY later${RESET}"; fi
    ;;
  11)
    set_env_val "LLM_PROVIDER" "openai_compatible"
    set_env_val "LLM_BASE_URL" "https://router.huggingface.co/v1"
    CUR_MODEL="$(get_env_val LLM_MODEL)"; [ -z "$CUR_MODEL" ] && CUR_MODEL="meta-llama/Llama-3.1-8B-Instruct"
    ask "Model (e.g. meta-llama/Llama-3.1-8B-Instruct)" "$CUR_MODEL" IN_MODEL
    set_env_val "LLM_MODEL" "$IN_MODEL"
    ask_secret "Hugging Face token (hf_...)" IN_KEY
    if [ -n "$IN_KEY" ]; then set_env_val "LLM_API_KEY" "$IN_KEY"; else echo -e "${YELLOW}  No key entered — set LLM_API_KEY later${RESET}"; fi
    ;;
  12)
    set_env_val "LLM_PROVIDER" "openai_compatible"
    CUR_URL="$(get_env_val LLM_BASE_URL)"; [ -z "$CUR_URL" ] && CUR_URL="http://host.docker.internal:11434/v1"
    ask "Ollama base URL" "$CUR_URL" IN_URL
    set_env_val "LLM_BASE_URL" "$IN_URL"
    CUR_MODEL="$(get_env_val LLM_MODEL)"; [ -z "$CUR_MODEL" ] && CUR_MODEL="llama3.1"
    ask "Model" "$CUR_MODEL" IN_MODEL
    set_env_val "LLM_MODEL" "$IN_MODEL"
    set_env_val "LLM_API_KEY" "ollama"
    echo -e "${DIM}  Note: run 'ollama serve && ollama pull ${IN_MODEL}' on host if needed.${RESET}"
    ;;
  13)
    set_env_val "LLM_PROVIDER" "openai_compatible"
    CUR_URL="$(get_env_val LLM_BASE_URL)"; [ -z "$CUR_URL" ] && CUR_URL="http://host.docker.internal:1234/v1"
    ask "LM Studio base URL" "$CUR_URL" IN_URL
    set_env_val "LLM_BASE_URL" "$IN_URL"
    CUR_MODEL="$(get_env_val LLM_MODEL)"; [ -z "$CUR_MODEL" ] && CUR_MODEL="local-model"
    ask "Model" "$CUR_MODEL" IN_MODEL
    set_env_val "LLM_MODEL" "$IN_MODEL"
    set_env_val "LLM_API_KEY" "lm-studio"
    echo -e "${DIM}  Note: enable 'Serve on Network' in LM Studio Developer tab.${RESET}"
    ;;
  14)
    set_env_val "LLM_PROVIDER" "openai_compatible"
    CUR_URL="$(get_env_val LLM_BASE_URL)"; [ -z "$CUR_URL" ] && CUR_URL="http://host.docker.internal:8000/v1"
    ask "Self-hosted base URL (vLLM/SGLang/llama.cpp)" "$CUR_URL" IN_URL
    set_env_val "LLM_BASE_URL" "$IN_URL"
    CUR_MODEL="$(get_env_val LLM_MODEL)"; [ -z "$CUR_MODEL" ] && CUR_MODEL="your-model"
    ask "Model" "$CUR_MODEL" IN_MODEL
    set_env_val "LLM_MODEL" "$IN_MODEL"
    CUR_KEY="$(get_env_val LLM_API_KEY)"; [ -z "$CUR_KEY" ] && CUR_KEY="CHANGE_ME"
    ask "API key (leave CHANGE_ME if none)" "$CUR_KEY" IN_KEY
    set_env_val "LLM_API_KEY" "$IN_KEY"
    echo -e "${DIM}  Start with: vLLM --enable-auto-tool-choice --tool-call-parser auto  or  llama-server --jinja${RESET}"
    ;;
  15)
    set_env_val "LLM_PROVIDER" "openai_compatible"
    set_env_val "LLM_BASE_URL" "https://opencode.ai/api/free/v1"
    CUR_MODEL="$(get_env_val LLM_MODEL)"; [ -z "$CUR_MODEL" ] && CUR_MODEL="muse-spark-1.2-contributor-free"
    ask "Model (muse-spark-1.2-contributor-free, deepseek-v4-flash-free, mimo-v2.5-free)" "$CUR_MODEL" IN_MODEL
    set_env_val "LLM_MODEL" "$IN_MODEL"
    set_env_val "LLM_API_KEY" "opencode-free"
    echo -e "${DIM}  Note: keyless — no API key needed. Live free catalog at opencode.ai (rotating promotions).${RESET}"
    ;;
  16)
    set_env_val "LLM_PROVIDER" "openai_compatible"
    set_env_val "LLM_BASE_URL" "https://opencode.ai/zen/go/v1"
    CUR_MODEL="$(get_env_val LLM_MODEL)"; [ -z "$CUR_MODEL" ] && CUR_MODEL="glm-5"
    ask "Model (glm-5, deepseek-v4-pro, qwen3.7-max, kimi-k2.5, etc.)" "$CUR_MODEL" IN_MODEL
    set_env_val "LLM_MODEL" "$IN_MODEL"
    ask_secret "OpenCode Go API key (OPENCODE_GO_API_KEY)" IN_KEY
    if [ -n "$IN_KEY" ]; then set_env_val "LLM_API_KEY" "$IN_KEY"; else echo -e "${YELLOW}  No key entered — set LLM_API_KEY later${RESET}"; fi
    echo -e "${DIM}  Get key at https://opencode.ai — Go plan. Also supports open models → opencode-go.${RESET}"
    ;;
  17)
    set_env_val "LLM_PROVIDER" "openai_compatible"
    set_env_val "LLM_BASE_URL" "https://opencode.ai/zen/v1"
    CUR_MODEL="$(get_env_val LLM_MODEL)"; [ -z "$CUR_MODEL" ] && CUR_MODEL="glm-5"
    ask "Model" "$CUR_MODEL" IN_MODEL
    set_env_val "LLM_MODEL" "$IN_MODEL"
    ask_secret "OpenCode Zen API key (OPENCODE_ZEN_API_KEY)" IN_KEY
    if [ -n "$IN_KEY" ]; then set_env_val "LLM_API_KEY" "$IN_KEY"; else echo -e "${YELLOW}  No key entered — set LLM_API_KEY later${RESET}"; fi
    echo -e "${DIM}  Get key at https://opencode.ai — Zen plan.${RESET}"
    ;;
  18)
    CUR_URL="$(get_env_val LLM_BASE_URL)"; [ -z "$CUR_URL" ] && CUR_URL="https://api.openai.com/v1"
    ask "Custom base URL (must speak OpenAI Chat Completions)" "$CUR_URL" IN_URL
    set_env_val "LLM_PROVIDER" "openai_compatible"
    set_env_val "LLM_BASE_URL" "$IN_URL"
    CUR_MODEL="$(get_env_val LLM_MODEL)"; [ -z "$CUR_MODEL" ] && CUR_MODEL="gpt-4o-mini"
    ask "Model" "$CUR_MODEL" IN_MODEL
    set_env_val "LLM_MODEL" "$IN_MODEL"
    CUR_KEY="$(get_env_val LLM_API_KEY)"; [ -z "$CUR_KEY" ] && CUR_KEY="CHANGE_ME"
    ask "API key" "$CUR_KEY" IN_KEY
    set_env_val "LLM_API_KEY" "$IN_KEY"
    echo -e "${DIM}  Covers: Novita, GMI, Nebius, Arcee, Tencent, StepFun, NVIDIA Build, Kilo, Xiaomi, Actual Computer, CommandCode, etc. — see docs/CONFIGURATION.md${RESET}"
    ;;
  *)
    set_env_val "LLM_PROVIDER" "mock"
    set_env_val "LLM_BASE_URL" "https://api.openai.com/v1"
    set_env_val "LLM_MODEL" "gpt-4o-mini"
    echo -e "${GREEN}✓ LLM set to mock (no key needed)${RESET}"
    ;;
esac
echo ""

# ---- Step 3: Telegram (optional) ----
echo -e "${BOLD}Step 3/4 — Telegram (optional)${RESET}  ${DIM}(leave empty to skip)${RESET}"
CUR_TOK="$(get_env_val TELEGRAM_BOT_TOKEN)"
if [ -n "$CUR_TOK" ] && [ "$CUR_TOK" != "CHANGE_ME" ]; then
  echo -e "${DIM}  Current bot token: $(mask "$CUR_TOK") — leave empty to keep, type 'clear' to disable${RESET}"
fi
ask "Telegram bot token (from @BotFather, e.g. 123456:ABC...)" "" IN_TOK
if [ "$IN_TOK" = "clear" ]; then
  set_env_val "TELEGRAM_BOT_TOKEN" "CHANGE_ME"
  set_env_val "TELEGRAM_ALLOWED_USER_IDS" ""
  echo -e "${YELLOW}  Telegram disabled${RESET}"
elif [ -n "$IN_TOK" ]; then
  set_env_val "TELEGRAM_BOT_TOKEN" "$IN_TOK"
  CUR_IDS="$(get_env_val TELEGRAM_ALLOWED_USER_IDS)"
  ask "Allowed user IDs (comma-separated, e.g. 12345678,87654321)" "$CUR_IDS" IN_IDS
  set_env_val "TELEGRAM_ALLOWED_USER_IDS" "$IN_IDS"
  echo -e "${GREEN}✓ Telegram configured${RESET}"
else
  if [ -z "$CUR_TOK" ] || [ "$CUR_TOK" = "CHANGE_ME" ]; then
    echo -e "${DIM}  Skipped — Telegram disabled (set later via --reconfigure)${RESET}"
  else
    echo -e "${DIM}  Keeping existing Telegram token${RESET}"
  fi
fi
echo ""

# ---- Step 4: Security secrets (auto-generated, not prompted) ----
echo -e "${BOLD}Step 4/4 — Security secrets${RESET}  ${DIM}(auto-generated if still CHANGE_ME)${RESET}"
for var in JWT_SECRET SESSION_ENCRYPTION_MASTER_KEY TELEGRAM_WEBHOOK_SECRET POSTGRES_PASSWORD; do
  cur="$(get_env_val "$var")"
  if [ -z "$cur" ] || [ "$cur" = "CHANGE_ME" ]; then
    val="$(gen_hex)"
    set_env_val "$var" "$val"
    echo -e "${GREEN}✓ Generated ${var}${RESET}"
  else
    echo -e "${DIM}  ${var} already set — kept${RESET}"
  fi
done
# Sync DATABASE_URL with POSTGRES_PASSWORD if it still has CHANGE_ME
if grep -q "DATABASE_URL=.*CHANGE_ME" .env 2>/dev/null; then
  _pw="$(get_env_val POSTGRES_PASSWORD)"
  if [ -n "$_pw" ] && [ "$_pw" != "CHANGE_ME" ]; then
    # replace CHANGE_ME in DATABASE_URL — use plain replace to avoid regex group reference issues when pw starts with digit
    python3 - "$_pw" <<'PY'
import pathlib, sys
pw=sys.argv[1]
p=pathlib.Path(".env")
t=p.read_text()
# Only touch DATABASE_URL line, preserve other CHANGE_ME (e.g. LLM_API_KEY)
lines=[]
for line in t.splitlines():
    if line.startswith("DATABASE_URL=") and "CHANGE_ME" in line:
        line=line.replace("CHANGE_ME", pw)
    lines.append(line)
p.write_text("\n".join(lines)+"\n")
PY
    echo -e "${GREEN}✓ Synced DATABASE_URL with POSTGRES_PASSWORD${RESET}"
  fi
fi
echo ""

# ---- Summary ----
echo -e "${BOLD}Summary — will be written to .env:${RESET}"
echo -e "  ADMIN_EMAIL=${GREEN}$(get_env_val ADMIN_EMAIL)${RESET}"
echo -e "  LLM_PROVIDER=$(get_env_val LLM_PROVIDER)  LLM_MODEL=$(get_env_val LLM_MODEL)  LLM_BASE_URL=$(get_env_val LLM_BASE_URL)"
echo -e "  LLM_API_KEY=$(mask "$(get_env_val LLM_API_KEY)")"
echo -e "  TELEGRAM_BOT_TOKEN=$(mask "$(get_env_val TELEGRAM_BOT_TOKEN)")  ALLOWED_IDS=$(get_env_val TELEGRAM_ALLOWED_USER_IDS)"
echo -e "  JWT_SECRET=$(mask "$(get_env_val JWT_SECRET)")  POSTGRES_PASSWORD=$(mask "$(get_env_val POSTGRES_PASSWORD)")"
echo ""
if [ "$RECONFIGURE" = false ]; then
  echo -e "${DIM}Tip: re-run to fix any value:  ./scripts/setup.sh --reconfigure${RESET}"
  echo -e "${DIM}     or edit manually:       nano .env && docker compose up -d --build${RESET}"
fi
echo ""
echo -ne "${BOLD}Apply and start the stack now? [Y/n]: ${RESET}"
read -r CONFIRM || true
CONFIRM="${CONFIRM:-Y}"
if [[ "$CONFIRM" =~ ^[nN] ]]; then
  echo -e "${YELLOW}Aborted — .env was updated but stack not started. Run: docker compose up -d --build${RESET}"
  exit 0
fi

echo ""
echo -e "${CYAN}→ docker compose up -d --build${RESET}"
docker compose up -d --build

if [ -x ./scripts/secret-scan.sh ]; then
  echo ""
  ./scripts/secret-scan.sh . || true
fi

FINAL_EMAIL="$(get_env_val ADMIN_EMAIL)"
echo ""
echo -e "${BOLD}════════════════════════════════════════════════════${RESET}"
echo -e "${GREEN}✅ LUMI ready!${RESET}"
echo -e "  ${BOLD}http://localhost:3525${RESET}  admin: ${GREEN}${FINAL_EMAIL}${RESET}  ${DIM}(password: you set in Step 1)${RESET}"
echo -e "  Logs:   ${CYAN}docker compose logs -f${RESET}"
echo -e "  Health: ${CYAN}curl -s http://localhost:3525/health/ready | jq${RESET}"
echo -e "  Fix:    ${CYAN}./scripts/setup.sh --reconfigure${RESET}  ${DIM}or  nano .env && docker compose up -d --build${RESET}"
echo -e "${BOLD}════════════════════════════════════════════════════${RESET}"
