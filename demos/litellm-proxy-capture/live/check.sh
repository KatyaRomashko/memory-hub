#!/usr/bin/env bash
# Preflight for the live demo. Run it once per machine before recording:
#
#     source live/env.sh && ./live/check.sh
#
# There is no `memoryhub doctor` for the personal edition (that command belongs
# to the cluster CLI), so this checks the things the demo actually depends on.
cd "$(dirname "$0")/.."
ok=0; bad=0
say()  { printf '  %-10s %s\n' "$1" "$2"; [ "$1" = "FAIL" ] && bad=$((bad+1)) || ok=$((ok+1)); }

echo
echo "live demo preflight"
echo

[ "${MEMORYHUB_CAPTURE_SINK:-}" = "local" ] \
  && say OK "environment sourced (sink=local, db=$XDG_DATA_HOME/memoryhub/memoryhub.db)" \
  || say FAIL "run 'source live/env.sh' in this terminal first"

python - <<'PY' && say OK "python packages: litellm, memoryhub_local, openai" || say FAIL "missing python packages -- uv pip install -r requirements.txt"
import litellm, memoryhub_local, openai  # noqa: F401
PY

python - <<'PY'
import sys
from memoryhub_local.embeddings.onnx import get_default_model_dir, is_model_downloaded
d = get_default_model_dir()
print(f"  {'OK' if is_model_downloaded(d) else 'FAIL':<10} embeddings: "
      f"{'onnx model present' if is_model_downloaded(d) else 'model NOT downloaded -> mock embeddings'}")
print(f"             {d}")
sys.exit(0 if is_model_downloaded(d) else 1)
PY
if [ $? -ne 0 ]; then
  bad=$((bad+1))
  echo "             fix: python -c 'from memoryhub_local.embeddings.onnx import download_model; download_model()'"
  echo "             (needs network to huggingface.co; without it the demo runs on mock embeddings"
  echo "              and the update-vs-create step cannot be shown honestly)"
else
  ok=$((ok+1))
fi

url="${MEMORYHUB_CAPTURE_EXTRACT_MODEL_URL:-}"
if curl -s -m 3 -o /dev/null -w '' "${url%/v1}/v1/models" 2>/dev/null; then
  say OK "extraction endpoint answers: $url"
  if [ -n "${MEMORYHUB_CAPTURE_EXTRACT_MODEL:-}" ] && command -v ollama >/dev/null 2>&1; then
    ollama list 2>/dev/null | grep -q "${MEMORYHUB_CAPTURE_EXTRACT_MODEL%%:*}" \
      && say OK "extraction model pulled: $MEMORYHUB_CAPTURE_EXTRACT_MODEL" \
      || { say FAIL "ollama has no $MEMORYHUB_CAPTURE_EXTRACT_MODEL"
           echo "             either: ollama pull $MEMORYHUB_CAPTURE_EXTRACT_MODEL"
           echo "             or use one you already have:"
           ollama list 2>/dev/null | sed -n '2,8p' | sed 's/^/               /'
           echo "               export MEMORYHUB_CAPTURE_EXTRACT_MODEL=<name>"; }
  fi
else
  say FAIL "extraction endpoint unreachable: $url"
  echo "             fix: 'ollama serve' + 'ollama pull $MEMORYHUB_CAPTURE_EXTRACT_MODEL',"
  echo "             or the stand-in: python tools/fake_extractor.py --port 4100"
  echo "               export MEMORYHUB_CAPTURE_EXTRACT_MODEL=fake-extractor"
  echo "               export MEMORYHUB_CAPTURE_EXTRACT_MODEL_URL=http://localhost:4100/v1"
fi

if lsof -nP -iTCP:"${PORT:-4000}" -sTCP:LISTEN >/dev/null 2>&1; then
  say FAIL "port ${PORT:-4000} is already in use -- stop that process or export PORT=4010"
else
  say OK "port ${PORT:-4000} free for the proxy"
fi

# --- optional: the second memory system, only checked when it is configured ---
if [ -n "${MEMORYHUB_CAPTURE_SHADOW_SINK:-}" ]; then
  hs="${MEMORYHUB_CAPTURE_HINDSIGHT_URL:-http://localhost:8888}"
  if curl -s -m 3 -o /dev/null "$hs/health/live" 2>/dev/null; then
    ver=$(curl -s -m 3 "$hs/version" 2>/dev/null | head -c 120)
    say OK "hindsight answers at $hs  ${ver}"
    say OK "shadow mode: $MEMORYHUB_CAPTURE_SHADOW_SINK, bank=${MEMORYHUB_CAPTURE_HINDSIGHT_BANK}"
  else
    say FAIL "shadow sink is set but hindsight is unreachable at $hs"
    echo "             start it (data lives in ~/.hindsight-docker):"
    echo "               docker start hindsight   # if the container already exists"
    echo "             or unset it for a single-store run:"
    echo "               export MEMORYHUB_CAPTURE_SHADOW_SINK="
  fi
fi

# --- optional: a real agent for the zero-touch tier --------------------------
if command -v codex >/dev/null 2>&1; then
  say OK "codex present: $(codex --version 2>/dev/null | head -1)"
else
  printf '  %-10s %s\n' "note" "codex not on PATH -- UC3 falls back to tools/harness_probe.py"
fi

[ -f "$XDG_DATA_HOME/memoryhub/memoryhub.db" ] \
  && say OK "database exists (rm -rf out/live-data to start from zero)" \
  || say OK "database not created yet -- clean start"

echo
echo "  $ok ok, $bad to fix"
echo
exit $((bad > 0))
