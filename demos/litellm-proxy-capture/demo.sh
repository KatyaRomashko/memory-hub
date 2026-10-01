#!/usr/bin/env bash
# The 10-minute demo: the same agent, with and without harness integration.
#
#   ./demo.sh              # offline stand-ins (no models needed)
#   ./demo.sh real         # real models from .env -- this is the one to show
#
# Follow DEMO.md while this runs; it pauses between acts (PAUSE=0 to disable).
set -uo pipefail
cd "$(dirname "$0")"
# shellcheck source=tools/bins.sh
. ./tools/bins.sh

MODE="${1:-offline}"
PORT="${PORT:-4030}"
EXTRACTOR_PORT="${EXTRACTOR_PORT:-4130}"
PAUSE="${PAUSE:-1}"
export POC_DATA_DIR="$PWD/out/demo-data"
export XDG_DATA_HOME="$POC_DATA_DIR"
export MEMORYHUB_CAPTURE_SINK=local
export MEMORYHUB_CAPTURE_OBSERVATIONS="$PWD/out/demo-observations.jsonl"
export LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:-sk-poc-local}"

if [ "$MODE" = "real" ]; then
  [ -f .env ] && set -a && . ./.env && set +a
  export MEMORYHUB_CAPTURE_SINK=local XDG_DATA_HOME="$POC_DATA_DIR"
  export MEMORYHUB_CAPTURE_OBSERVATIONS="$PWD/out/demo-observations.jsonl"
  unset HF_HUB_OFFLINE
  CONFIG=config.yaml
  : "${MEMORYHUB_CAPTURE_EXTRACT_MODEL_URL:?set MEMORYHUB_CAPTURE_EXTRACT_MODEL_URL in .env}"
  if [ -z "${MEMORYHUB_CAPTURE_EXTRACT_API_KEY:-}" ] && [ -n "${POC_MODEL_API_KEY:-}" ]; then
    export MEMORYHUB_CAPTURE_EXTRACT_API_KEY="$POC_MODEL_API_KEY"
  fi
else
  CONFIG=config.mock.yaml
  export HF_HUB_OFFLINE=1
  export MEMORYHUB_CAPTURE_EXTRACT_MODEL=fake-extractor
  export MEMORYHUB_CAPTURE_EXTRACT_MODEL_URL="http://localhost:$EXTRACTOR_PORT/v1"
  echo "NOTE: offline mode -- mock embeddings and a rule-based extractor. Use 'real' to demo."
fi
# Do not inherit EXTRACT_EVERY=0 from a cluster .env.
export MEMORYHUB_CAPTURE_EXTRACT_EVERY="${POC_EXTRACT_EVERY:-2}"

rm -rf out/demo-data out/demo-observations.jsonl
mkdir -p out
PIDS=()
trap 'for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null; done' EXIT
pause() { [ "$PAUSE" = "1" ] && { printf '\n-- press enter --'; read -r _; } || true; }
act()   { printf '\n\n=== %s ===\n\n' "$*"; }

[ "$MODE" != "real" ] && { python3 tools/fake_extractor.py --port "$EXTRACTOR_PORT" & PIDS+=("$!"); sleep 1; }

# Each act gets its own database so the two approaches are measured separately,
# and the proxy is started with capture off for act 1 (the agent does the work).
start_proxy() {  # $1 = capture on|off
  MEMORYHUB_CAPTURE_ENABLED="$1" litellm --config "$CONFIG" --port "$PORT" > "out/demo-proxy-$1.log" 2>&1 &
  PROXY_PID=$!; PIDS+=("$PROXY_PID")
  wait_for_proxy "$PROXY_PID" "$PORT" "out/demo-proxy-$1.log" || exit 1
}
stop_proxy() { kill "$PROXY_PID" 2>/dev/null; wait "$PROXY_PID" 2>/dev/null; sleep 1; }

export XDG_DATA_HOME="$POC_DATA_DIR/act1-explicit"
start_proxy off

act "Act 1 -- how memory works today: the agent has to do it"
echo "agent_explicit.py carries the memory instruction and writes memories itself."
echo "That instruction, an MCP tool and a rules file are what every harness has to be given."
grep -n "MEMORY_INSTRUCTION = " -A 4 agent_explicit.py | head -8
pause
python3 agent_explicit.py --base-url "http://localhost:$PORT" --scenario preferences
echo
echo "cost of that: one extra model call per turn, plus the instruction in every prompt"
pause

stop_proxy
export XDG_DATA_HOME="$POC_DATA_DIR/act2-proxy"
start_proxy on

act "Act 2 -- the proxy agent: nothing but a base URL"
echo "agent.py (fresh database, capture on). Memory machinery in its code:"
echo -n "  calls to write_memory / MemoryHub SDK / MCP in its code: "
grep -Ev "^\s*#|^\s*\"" agent.py | grep -Ec "write_memory|MemoryHubClient|memoryhub_local" || true
echo "  the only change is where the model lives:"
grep -n "add_argument(\"--base-url\"" agent.py
pause
python3 agent.py --base-url "http://localhost:$PORT" --scenario preferences --session demo
pause

act "Act 3 -- the memory is already there"
echo "No manual extraction was run: the proxy triggered it from the traffic."
sleep 2
python3 verify.py search "dark mode"
python3 verify.py threads
pause

act "Act 4 -- what this does not solve"
cat <<'TXT'
- Session boundaries: without X-MemoryHub-Session the proxy has to fingerprint the
  first user message. One header, versus MCP + rules + hooks per harness.
- Identity: the memory is owned by whoever the proxy authenticates as, not by the
  person speaking. The observed speaker is metadata only.
- A correction ("eu-west-1, not us-east-1") is stored as a second fact, not as a
  correction of the first.
- The personal edition's own write path has no duplicate gate: extraction skips
  what the agent already wrote, but an agent write after extraction duplicates it.
TXT
echo
echo "act 1 db: $POC_DATA_DIR/act1-explicit/memoryhub/memoryhub.db"
echo "act 2 db: $POC_DATA_DIR/act2-proxy/memoryhub/memoryhub.db"
echo "observations: out/demo-observations.jsonl"
