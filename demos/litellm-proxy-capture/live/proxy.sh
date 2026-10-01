#!/usr/bin/env bash
# Start the LiteLLM proxy for the live demo.
#   source live/env.sh && ./live/proxy.sh          # canned answers, no keys needed
#   source live/env.sh && ./live/proxy.sh ollama   # real answers, fully local, no keys
#   source live/env.sh && ./live/proxy.sh real     # a hosted model, key read from .env
#
# Unlike run-proxy.sh this does NOT source the whole .env -- only the three
# model-credential names -- so the capture settings from live/env.sh survive.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ "${MEMORYHUB_CAPTURE_SINK:-}" != "local" ]; then
  echo "run 'source live/env.sh' in this terminal first" >&2
  exit 2
fi

cfg=config.mock.yaml
if [ "${1:-}" = "ollama" ]; then
  cfg=config.ollama.yaml
  echo "agent model: ${POC_AGENT_MODEL:?run 'source live/env.sh' first} via $POC_AGENT_API_BASE"
elif [ "${1:-}" = "real" ]; then
  cfg=config.yaml
  [ -f .env ] || { echo "real mode needs .env with POC_MODEL and POC_MODEL_API_KEY" >&2; exit 1; }
  set -a
  # shellcheck disable=SC1090
  . <(grep -E '^(POC_MODEL|POC_MODEL_API_KEY|ANTHROPIC_API_KEY)=' .env)
  set +a
  [ -n "${POC_MODEL:-}" ] || { echo "POC_MODEL is empty in .env" >&2; exit 1; }
  echo "model: $POC_MODEL"
fi

litellm=.venv/bin/litellm
[ -x "$litellm" ] || litellm=litellm
echo "config: $cfg   port: ${PORT:-4000}"
exec "$litellm" --config "$cfg" --port "${PORT:-4000}"
