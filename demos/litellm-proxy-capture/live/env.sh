#!/usr/bin/env bash
# Shared environment for the LIVE (manual) demo. Source it in EVERY terminal,
# from this directory (works in bash and zsh):
#
#     cd demos/litellm-proxy-capture && source live/env.sh
#
# It deliberately does NOT source .env: that file points the sink at the
# cluster (MEMORYHUB_CAPTURE_SINK=memoryhub) and holds keys. Model credentials
# for `real` mode are read by live/proxy.sh only, so .env never has to be
# opened on camera.

# --- locate the demo directory (zsh has no BASH_SOURCE) --------------------
if [ -f "$PWD/live/env.sh" ]; then
  _root="$PWD"
elif [ -n "${ZSH_VERSION:-}" ]; then
  _root="$(cd "$(dirname "$(eval 'printf %s "${(%):-%x}"')")/.." && pwd)"
elif [ -n "${BASH_SOURCE:-}" ]; then
  _root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi
if [ -z "${_root:-}" ] || [ ! -f "$_root/live/env.sh" ]; then
  echo "run this from demos/litellm-proxy-capture: cd ...; source live/env.sh" >&2
  return 1 2>/dev/null || exit 1
fi
cd "$_root" || return 1
export DEMO_ROOT="$_root"

# --- 1. isolated database: the demo starts from zero -----------------------
# Always derived from DEMO_ROOT: a stale POC_DATA_DIR left over from an
# earlier source in the same shell would otherwise point the database
# somewhere else. Override with MEMORYHUB_LIVE_DATA_DIR if you need to.
export POC_DATA_DIR="${MEMORYHUB_LIVE_DATA_DIR:-$DEMO_ROOT/out/live-data}"
export XDG_DATA_HOME="$POC_DATA_DIR"
mkdir -p "$POC_DATA_DIR/memoryhub" "$DEMO_ROOT/out"

# The ONNX embedding model is cached under $XDG_DATA_HOME/memoryhub/models,
# so an isolated data dir would hide an already downloaded model and silently
# fall back to mock embeddings. Link the real cache in instead of re-downloading.
_share="${MEMORYHUB_REAL_DATA_HOME:-$HOME/.local/share}"
if [ ! -e "$POC_DATA_DIR/memoryhub/models" ] && [ -d "$_share/memoryhub/models" ]; then
  ln -s "$_share/memoryhub/models" "$POC_DATA_DIR/memoryhub/models"
fi
_onnx="$POC_DATA_DIR/memoryhub/models/granite-embedding-small-english-r2-onnx/onnx/model_quantized.onnx"
if [ -f "$_onnx" ]; then _embed="onnx"; else _embed="MOCK -- model not found, search ranking and update/skip are meaningless"; fi

# --- 2. what the proxy callback does ---------------------------------------
export MEMORYHUB_CAPTURE_SINK=local          # personal edition, SQLite, in-process
export MEMORYHUB_CAPTURE_ENABLED=true
export MEMORYHUB_CAPTURE_TOOLS=false         # do not store tool_call/tool_result
export MEMORYHUB_CAPTURE_EXTRACT_EVERY=2     # extraction every 2 user turns
export MEMORYHUB_CAPTURE_WHEN_AGENT_WRITES=defer
export MEMORYHUB_CAPTURE_OBSERVATIONS="$DEMO_ROOT/out/live-observations.jsonl"

# Credentials are stripped before anything is stored OR hashed (redact.py).
# Set to false for one turn to show what the store looks like without it.
export MEMORYHUB_CAPTURE_REDACT="${MEMORYHUB_CAPTURE_REDACT:-true}"
# Site-specific patterns, `name=regex` separated by newlines or `;;`:
# export MEMORYHUB_CAPTURE_REDACT_EXTRA='rh_ticket=RH-[0-9]{6}'

# --- 2b. second memory system (shadow mode) --------------------------------
# Empty by default: one store, exactly as before. Set to `hindsight` and the
# same conversation goes into BOTH, with only MemoryHub tracked and answering.
export MEMORYHUB_CAPTURE_SHADOW_SINK="${MEMORYHUB_CAPTURE_SHADOW_SINK:-}"
export MEMORYHUB_CAPTURE_HINDSIGHT_URL="${MEMORYHUB_CAPTURE_HINDSIGHT_URL:-http://localhost:8888}"
export MEMORYHUB_CAPTURE_HINDSIGHT_BANK="${MEMORYHUB_CAPTURE_HINDSIGHT_BANK:-memoryhub-proxy-demo}"

# --- 3. the extraction ("dreaming") LLM ------------------------------------
#    Ollama by default. Offline fallback:
#      python tools/fake_extractor.py --port 4100
#      export MEMORYHUB_CAPTURE_EXTRACT_MODEL=fake-extractor
#      export MEMORYHUB_CAPTURE_EXTRACT_MODEL_URL=http://localhost:4100/v1
export MEMORYHUB_CAPTURE_EXTRACT_MODEL="${MEMORYHUB_CAPTURE_EXTRACT_MODEL:-qwen2.5:7b}"
export MEMORYHUB_CAPTURE_EXTRACT_MODEL_URL="${MEMORYHUB_CAPTURE_EXTRACT_MODEL_URL:-http://localhost:11434/v1}"

# The agent's own model for `./live/proxy.sh ollama` -- same weights as
# extraction, so Ollama keeps one model loaded instead of swapping.
export POC_AGENT_MODEL="${POC_AGENT_MODEL:-ollama_chat/${MEMORYHUB_CAPTURE_EXTRACT_MODEL}}"
export POC_AGENT_API_BASE="${POC_AGENT_API_BASE:-http://localhost:11434}"

# --- 4. the proxy itself ---------------------------------------------------
export PORT="${PORT:-4000}"
export LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:-sk-poc-local}"
export LITELLM_KEY="$LITELLM_MASTER_KEY"
export LLM_BASE_URL="http://localhost:${PORT}"

# Use the demo's own venv. Another project's venv may already be active (the
# repo root has one without `openai`), so switch rather than leave it alone.
if [ -d .venv ] && [ "${VIRTUAL_ENV:-}" != "$DEMO_ROOT/.venv" ]; then
  if [ -n "${VIRTUAL_ENV:-}" ] && command -v deactivate >/dev/null 2>&1; then
    deactivate
  fi
  # shellcheck disable=SC1091
  . .venv/bin/activate
fi

printf '\nlive demo environment\n'
printf '  dir           %s\n' "$DEMO_ROOT"
printf '  python        %s\n' "$(command -v python)"
printf '  database      %s\n' "$XDG_DATA_HOME/memoryhub/memoryhub.db"
printf '  embeddings    %s\n' "$_embed"
printf '  observations  %s\n' "$MEMORYHUB_CAPTURE_OBSERVATIONS"
printf '  extraction    every %s user turns -> %s @ %s\n' \
  "$MEMORYHUB_CAPTURE_EXTRACT_EVERY" "$MEMORYHUB_CAPTURE_EXTRACT_MODEL" "$MEMORYHUB_CAPTURE_EXTRACT_MODEL_URL"
printf '  agent model   %s (only for ./live/proxy.sh ollama)\n' "$POC_AGENT_MODEL"
printf '  redaction     %s\n' "$( [ "$MEMORYHUB_CAPTURE_REDACT" = "true" ] && echo on || echo 'OFF -- secrets will be stored' )"
printf '  shadow sink   %s\n' "${MEMORYHUB_CAPTURE_SHADOW_SINK:-none (single store)}"
printf '  proxy         %s\n\n' "$LLM_BASE_URL"
unset _root _share _onnx _embed
