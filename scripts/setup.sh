#!/usr/bin/env bash
# One-shot setup for the NineToVibes automation pipeline.
# Safe to re-run; each step is idempotent.

set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

say() { printf "\n\033[1;36m==>\033[0m %s\n" "$*"; }
ok()  { printf "   \033[1;32m✓\033[0m %s\n" "$*"; }
warn(){ printf "   \033[1;33m!\033[0m %s\n" "$*"; }

# --- 1. Homebrew deps ---
say "Checking Homebrew dependencies..."
if ! command -v brew >/dev/null; then
  echo "Homebrew is required. Install from https://brew.sh" >&2
  exit 1
fi

for pkg in ffmpeg ollama; do
  if brew list --formula | grep -q "^${pkg}\$"; then
    ok "$pkg already installed"
  else
    say "Installing $pkg (this may take a minute)..."
    brew install "$pkg"
  fi
done

# --- 2. Start Ollama as a background service ---
say "Ensuring Ollama service is running..."
if ! pgrep -qf "ollama serve"; then
  brew services start ollama >/dev/null 2>&1 || nohup ollama serve >/dev/null 2>&1 &
  sleep 2
fi
ok "Ollama reachable"

# --- 3. Pull the Llama 3.1 8B quantized model (~4.7 GB) ---
MODEL="llama3.1:8b-instruct-q4_K_M"
if ollama list 2>/dev/null | grep -q "$MODEL"; then
  ok "Model $MODEL already pulled"
else
  say "Pulling $MODEL (~4.7 GB, one time only)..."
  ollama pull "$MODEL"
fi

# --- 4. Python venv (pinned to 3.13 — 3.14 has broken ensurepip on some macOS builds) ---
say "Preparing Python 3.13..."
if ! command -v python3.13 >/dev/null; then
  say "Installing python@3.13 via Homebrew..."
  brew install python@3.13
fi
PYBIN="$(command -v python3.13)"
ok "Using $PYBIN ($("$PYBIN" --version))"

# Nuke any previously-broken venv so we get a clean 3.13 one
if [ -d ".venv" ]; then
  CURRENT_VENV_PY="$(readlink .venv/bin/python3 2>/dev/null || true)"
  if [[ "$CURRENT_VENV_PY" != *"3.13"* ]]; then
    warn "Removing stale .venv (not Python 3.13)"
    rm -rf .venv
  fi
fi

say "Creating Python venv..."
if [ ! -d ".venv" ]; then
  "$PYBIN" -m venv .venv
  ok "venv created at .venv/ with $("$PYBIN" --version)"
fi
# shellcheck source=/dev/null
source .venv/bin/activate
python -m pip install --quiet --upgrade pip wheel setuptools
python -m pip install --quiet -r requirements.txt
ok "Python deps installed"

# --- 5. Secrets scaffolding ---
mkdir -p secrets data/drafts data/published data/cache/sources data/cache/trends data/logs
if [ ! -f ".env" ]; then
  cp .env.example .env
  warn ".env created from .env.example — fill in API keys next."
fi

# --- 6. Summary ---
say "Setup complete."
cat <<EOF

Next steps (in order):
  1. Fill in API keys:      open .env  (see scripts/setup_keys.md for links)
  2. YouTube OAuth:         follow scripts/setup_youtube_oauth.md
  3. Telegram bot:          follow scripts/setup_telegram_bot.md
  4. Verify end-to-end:     .venv/bin/python -m src.orchestrator --dry-run

Project root: $ROOT
EOF
