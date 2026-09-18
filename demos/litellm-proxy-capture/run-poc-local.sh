#!/usr/bin/env bash
# Run the whole WRIG-1482 PoC checklist against the MemoryHub personal edition.
#
#   ./run-poc-local.sh              # offline: mock agent model + rule-based extractor
#   ./run-poc-local.sh real         # real models: see .env (POC_MODEL, EXTRACT_MODEL_URL)
#
# Every step prints what it checks and what it found; the whole transcript is
# saved to out/poc-report.txt. The local database lives in a run-specific
# directory (POC_DATA_DIR) so a run never touches a working MemoryHub install.
set -uo pipefail
cd "$(dirname "$0")"

MODE="${1:-offline}"
PORT="${PORT:-4010}"
EXTRACTOR_PORT="${EXTRACTOR_PORT:-4100}"
export POC_DATA_DIR="${POC_DATA_DIR:-$PWD/out/local-data}"
export XDG_DATA_HOME="$POC_DATA_DIR"
export MEMORYHUB_CAPTURE_SINK=local
export MEMORYHUB_CAPTURE_OBSERVATIONS="$PWD/out/observations.jsonl"
export MEMORYHUB_CAPTURE_EXTRACT_EVERY="${MEMORYHUB_CAPTURE_EXTRACT_EVERY:-2}"
export LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:-sk-poc-local}"

if [ "$MODE" = "real" ]; then
  [ -f .env ] && set -a && . ./.env && set +a
  CONFIG=config.yaml
  # .env is shared with the cluster demo; this script is the local one, so the
  # sink and the database location are forced back after sourcing it.
  export MEMORYHUB_CAPTURE_SINK=local
  export XDG_DATA_HOME="$POC_DATA_DIR"
  export MEMORYHUB_CAPTURE_OBSERVATIONS="$PWD/out/observations.jsonl"
  unset HF_HUB_OFFLINE
  : "${MEMORYHUB_CAPTURE_EXTRACT_MODEL_URL:?set MEMORYHUB_CAPTURE_EXTRACT_MODEL_URL in .env (e.g. http://localhost:11434/v1)}"
  echo "extraction model: ${MEMORYHUB_CAPTURE_EXTRACT_MODEL:-unset} at $MEMORYHUB_CAPTURE_EXTRACT_MODEL_URL"
else
  CONFIG=config.mock.yaml
  export HF_HUB_OFFLINE=1          # the ONNX model cannot be fetched offline; fail fast
  export MEMORYHUB_CAPTURE_EXTRACT_MODEL=fake-extractor
  export MEMORYHUB_CAPTURE_EXTRACT_MODEL_URL="http://localhost:$EXTRACTOR_PORT/v1"
fi

rm -rf out/local-data out/observations.jsonl out/poc-report.txt
mkdir -p out
exec > >(tee out/poc-report.txt) 2>&1

PIDS=()
cleanup() { for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null; done; }
trap cleanup EXIT

step() { printf '\n\n========== %s ==========\n' "$*"; }
run()  { printf '\n$ %s\n' "$*"; "$@"; }

start_proxy() {
  litellm --config "$CONFIG" --port "$PORT" > "out/proxy-$1.log" 2>&1 &
  PROXY_PID=$!; PIDS+=("$PROXY_PID")
  for _ in $(seq 1 60); do
    curl -s "localhost:$PORT/health/liveliness" >/dev/null && return 0
    sleep 1
  done
  echo "proxy failed to start; see out/proxy-$1.log"; exit 1
}
stop_proxy() { kill "$PROXY_PID" 2>/dev/null; wait "$PROXY_PID" 2>/dev/null; sleep 1; }

AGENT=(python3 agent.py --base-url "http://localhost:$PORT")

echo "MemoryHub proxy-capture PoC -- personal edition -- mode: $MODE"
echo "database: $POC_DATA_DIR/memoryhub/memoryhub.db"
date

if [ "$MODE" != "real" ]; then
  python3 tools/fake_extractor.py --port "$EXTRACTOR_PORT" &
  PIDS+=("$!")
  sleep 1
fi
start_proxy 1

step "1. Capture without the agent knowing about MemoryHub"
echo "agent.py has no MCP tool, no memory prompt, no hooks; only --base-url points at the proxy"
run "${AGENT[@]}" --scenario preferences --session poc-prefs
sleep 2
run python3 verify.py threads

step "2. No duplicates, including across a proxy restart"
THREAD=$(python3 tools/thread_id.py poc-prefs)
echo "thread for session poc-prefs: $THREAD"
stop_proxy
echo "(proxy restarted -- in-process delta state is gone)"
start_proxy 2
run "${AGENT[@]}" --scenario preferences-followup --session poc-prefs
sleep 2
run python3 verify.py messages "$THREAD"

step "3. Session identification"
echo "3a. explicit header (above): session=poc-prefs"
echo "3b. no header -- the proxy has to fingerprint the first user message"
run "${AGENT[@]}" --scenario smalltalk
echo "3c. OpenAI 'user' field as the actor"
run "${AGENT[@]}" --scenario correction --user alice@example.com
sleep 2
run python3 verify.py threads

step "4. Ownership and attribution"
echo "owner = the identity the sink writes as; observed_actor = what the proxy saw"
run python3 verify.py threads

step "5. Extraction quality per scenario"
echo "preferences -> facts, smalltalk -> nothing, correction -> update or second fact"
run python3 verify.py extract
run python3 verify.py search "dark mode"
run python3 verify.py search "deployment target"
run python3 verify.py search "boil an egg"

step "6. Cost and latency"
run python3 verify.py report

step "7. Harness-shaped traffic (Anthropic /v1/messages, tools, system-reminder)"
run python3 tools/harness_probe.py --base-url "http://localhost:$PORT" --key "$LITELLM_MASTER_KEY"
sleep 2
HARNESS=$(python3 tools/thread_id.py poc-harness)
run python3 verify.py messages "$HARNESS"
run python3 verify.py threads

step "Checklist summary"
run python3 tools/poc_summary.py

step "Done"
echo "full transcript: out/poc-report.txt"
echo "database:        $POC_DATA_DIR/memoryhub/memoryhub.db"
