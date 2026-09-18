#!/usr/bin/env bash
# Phase 4: compare how memory gets written, on the personal edition.
#
#   ./run-poc-compare.sh            # offline stand-ins (CI / regression)
#   ./run-poc-compare.sh real       # real models from .env
#
# Modes, each on its own database under out/compare/<mode>:
#   A  explicit  -- the agent writes memories itself (today's harness integration)
#   B  proxy     -- proxy captures and extracts every 2 user turns
#   C  deferred  -- proxy captures only; extraction runs afterwards (dreaming)
#   D  B + re-extract over the whole thread (does the cursor/dedup hold?)
#   E  A + proxy on the same database (do two sources duplicate each other?)
#
# Results: out/compare.jsonl (raw) and out/compare.md (table).
set -uo pipefail
cd "$(dirname "$0")"

MODE="${1:-offline}"
PORT="${PORT:-4020}"
EXTRACTOR_PORT="${EXTRACTOR_PORT:-4120}"
export LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:-sk-poc-local}"
export MEMORYHUB_CAPTURE_SINK=local

if [ "$MODE" = "real" ]; then
  [ -f .env ] && set -a && . ./.env && set +a
  export MEMORYHUB_CAPTURE_SINK=local
  unset HF_HUB_OFFLINE
  CONFIG=config.yaml
  : "${MEMORYHUB_CAPTURE_EXTRACT_MODEL_URL:?set MEMORYHUB_CAPTURE_EXTRACT_MODEL_URL in .env}"
else
  CONFIG=config.mock.yaml
  export HF_HUB_OFFLINE=1
  export MEMORYHUB_CAPTURE_EXTRACT_MODEL=fake-extractor
  export MEMORYHUB_CAPTURE_EXTRACT_MODEL_URL="http://localhost:$EXTRACTOR_PORT/v1"
fi

rm -rf out/compare out/compare.jsonl out/compare.md
mkdir -p out/compare
exec > >(tee out/compare-run.txt) 2>&1

PIDS=()
cleanup() { for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null; done; }
trap cleanup EXIT

if [ "$MODE" != "real" ]; then
  python3 tools/fake_extractor.py --port "$EXTRACTOR_PORT" & PIDS+=("$!")
  sleep 1
fi

start_proxy() {
  litellm --config "$CONFIG" --port "$PORT" > "out/compare/proxy-$1.log" 2>&1 &
  PROXY_PID=$!
  for _ in $(seq 1 60); do curl -s "localhost:$PORT/health/liveliness" >/dev/null && return 0; sleep 1; done
  echo "proxy failed to start"; exit 1
}
stop_proxy() { kill "$PROXY_PID" 2>/dev/null; wait "$PROXY_PID" 2>/dev/null; sleep 1; }

scenarios() {  # $1 = session prefix
  python3 agent.py --base-url "http://localhost:$PORT" --scenario preferences --session "$1-prefs"
  python3 agent.py --base-url "http://localhost:$PORT" --scenario smalltalk --session "$1-small"
  python3 agent.py --base-url "http://localhost:$PORT" --scenario correction --session "$1-corr"
}

explicit_scenarios() {
  for s in preferences smalltalk correction; do
    python3 agent_explicit.py --base-url "http://localhost:$PORT" --scenario "$s" \
      --stats "out/compare/$1/explicit-$s.json"
  done
  python3 - "$1" <<'PY'
import json, sys, pathlib
d = pathlib.Path("out/compare") / sys.argv[1]
total = {"llm_calls": 0, "prompt_tokens": 0, "fallbacks": 0, "memories": []}
for f in d.glob("explicit-*.json"):
    s = json.loads(f.read_text())
    total["llm_calls"] += s["llm_calls"]; total["prompt_tokens"] += s["prompt_tokens"]
    total["fallbacks"] += s["fallbacks"]; total["memories"] += s["memories"]
(d / "explicit-total.json").write_text(json.dumps(total))
PY
}

record() {  # $1 = label, $2 = description
  EXPLICIT_STATS="out/compare/$1/explicit-total.json" \
    python3 tools/mode_stats.py "$1" "$2" >> out/compare.jsonl
}

prepare() {  # $1 = mode dir
  mkdir -p "out/compare/$1"
  export XDG_DATA_HOME="$PWD/out/compare/$1"
  export MEMORYHUB_CAPTURE_OBSERVATIONS="$PWD/out/compare/$1/observations.jsonl"
}

echo "=== A: explicit writes by the agent (today's harness integration) ==="
prepare A; export MEMORYHUB_CAPTURE_ENABLED=false MEMORYHUB_CAPTURE_EXTRACT_EVERY=0
start_proxy A; explicit_scenarios A; stop_proxy
record A "agent writes memories itself (MCP-style)"

echo "=== B: proxy capture, extraction every 2 user turns ==="
prepare B; export MEMORYHUB_CAPTURE_ENABLED=true MEMORYHUB_CAPTURE_EXTRACT_EVERY=2
start_proxy B; scenarios b; stop_proxy
record B "proxy captures, extracts during the session"

echo "=== C: proxy capture only, extraction afterwards (dreaming) ==="
prepare C; export MEMORYHUB_CAPTURE_EXTRACT_EVERY=0
start_proxy C; scenarios c; stop_proxy
python3 verify.py extract > "out/compare/C/extract.txt" 2>&1; tail -3 "out/compare/C/extract.txt"
record C "proxy captures, extraction runs later"

echo "=== D: like B, then a full re-extraction of every thread ==="
prepare D; export MEMORYHUB_CAPTURE_EXTRACT_EVERY=2
start_proxy D; scenarios d; stop_proxy
for t in $(python3 tools/thread_id.py --all); do
  python3 verify.py reextract "$t" >> "out/compare/D/reextract.txt" 2>&1
done
tail -3 "out/compare/D/reextract.txt" 2>/dev/null
record D "proxy + a second extraction pass over the same messages"

echo "=== E: explicit agent and proxy on the same database ==="
prepare E; export MEMORYHUB_CAPTURE_ENABLED=true MEMORYHUB_CAPTURE_EXTRACT_EVERY=2
start_proxy E; explicit_scenarios E; scenarios e; stop_proxy
record E "both sources writing into one database"

python3 tools/compare_report.py out/compare.jsonl | tee out/compare.md
echo
echo "table: out/compare.md   raw: out/compare.jsonl   transcript: out/compare-run.txt"
