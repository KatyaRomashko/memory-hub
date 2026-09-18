#!/usr/bin/env bash
# Start the LiteLLM proxy with the MemoryHub capture callback.
#   ./run-proxy.sh            # mock model, jsonl sink (offline)
#   ./run-proxy.sh real       # config.yaml, real models
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
export LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:-sk-poc-local}"
cfg=config.mock.yaml
[ "${1:-}" = "real" ] && cfg=config.yaml
mkdir -p out

if [ "$cfg" = "config.yaml" ]; then
  if [ ! -f .env ]; then
    echo "No .env file. Real mode needs POC_MODEL and POC_MODEL_API_KEY:" >&2
    echo "  cp .env.example .env   # then fill in the values" >&2
    exit 1
  fi
  if [ -z "${POC_MODEL:-}" ]; then
    echo "POC_MODEL is empty. Set it in .env (e.g. gemini/gemini-2.5-flash or openai/gpt-4o-mini)." >&2
    exit 1
  fi
  if [ -z "${POC_MODEL_API_KEY:-}" ]; then
    echo "POC_MODEL_API_KEY is empty. Set it in .env to the provider key for \$POC_MODEL." >&2
    echo "LiteLLM interpolates os.environ/POC_MODEL from .env; a missing model name crashes proxy startup." >&2
    exit 1
  fi
fi

# Prefer the demo venv so this works even if another project's venv is active.
if [ -x .venv/bin/litellm ]; then
  litellm=.venv/bin/litellm
elif command -v litellm >/dev/null 2>&1; then
  litellm=litellm
else
  echo "litellm not found. From this directory run:" >&2
  echo "  uv venv && source .venv/bin/activate && uv pip install -r requirements.txt -e ../../sdk" >&2
  exit 127
fi
exec "$litellm" --config "$cfg" --port "${PORT:-4000}"
