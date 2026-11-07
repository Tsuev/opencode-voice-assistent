#!/usr/bin/env bash
# Jarvis one-command installer for macOS (Apple Silicon) and Windows (Git Bash).
#
#   ./install.sh
#   ./install.sh --model q4 --offline --models-dir ./models
#   ./install.sh --build-from-source --cuda
#
# The shell part only bootstraps uv and the Python environment; all
# platform-specific work lives in scripts/install.py.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

say() { printf '%s\n' "$*"; }

if ! command -v uv >/dev/null 2>&1; then
  say "uv не найден — устанавливаю."
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://astral.sh/uv/install.sh | sh
  else
    say "Нужен curl или wget, чтобы установить uv." >&2
    exit 1
  fi
fi

# uv's installer usually drops the binary here; make the current shell see it.
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$HOME/.local/share/uv:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  say "uv установлен, но не найден в PATH. Откройте новый терминал и повторите ./install.sh." >&2
  exit 1
fi

say "Python 3.12 и зависимости Jarvis..."
uv python install 3.12
uv sync

say "Установка моделей и голоса..."
exec uv run python scripts/install.py "$@"
