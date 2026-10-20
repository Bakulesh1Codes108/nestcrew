#!/usr/bin/env bash
# One-time setup for macOS / Linux. Needs internet ONCE to download; offline after.
set -e
cd "$(dirname "$0")/.."

if ! command -v ollama >/dev/null 2>&1; then
  echo ">> Installing Ollama (local model runtime)..."
  if [[ "$OSTYPE" == darwin* ]]; then
    echo "   Download the Mac app from https://ollama.com/download, open it, then re-run this script."; exit 1
  fi
  curl -fsSL https://ollama.com/install.sh | sh
fi

# Pick a model size from RAM
RAM_GB=$( (sysctl -n hw.memsize 2>/dev/null || awk '/MemTotal/{print $2*1024}' /proc/meminfo) | awk '{printf "%d", $1/1073741824}')
if   [ "$RAM_GB" -ge 16 ]; then MODEL="gemma4:e4b"
elif [ "$RAM_GB" -ge 8 ];  then MODEL="qwen3-vl:2b"
else MODEL="gemma3:1b"; echo "!! Under 8 GB RAM: using a tiny text-only model (no image support)."; fi
echo ">> ${RAM_GB} GB RAM detected -> pulling $MODEL"
ollama pull "$MODEL"

python3 -m venv .venv
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -e ".[dev,media]"
[ -f .env ] || cp .env.example .env

echo
echo "Done. Set 'local.model: $MODEL' in config.yaml if it differs, then:"
echo "  source .venv/bin/activate && nestcrew doctor && nestcrew serve"
