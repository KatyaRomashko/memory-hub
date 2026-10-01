# Prefer this demo's venv even if another project's venv is active on PATH.
# Sourced by demo.sh, run-poc-local.sh, run-poc-compare.sh (after cd to this dir).
if [ -d "$PWD/.venv/bin" ]; then
  PATH="$PWD/.venv/bin:$PATH"
  export PATH
fi
if ! command -v litellm >/dev/null 2>&1; then
  echo "litellm not found. From this directory run:" >&2
  echo "  uv venv && source .venv/bin/activate && uv pip install -r requirements.txt -e ../../sdk -e \"../../memoryhub-local[dream]\"" >&2
  exit 127
fi

wait_for_proxy() {  # pid port logfile
  local pid=$1 port=$2 log=$3 i
  for i in $(seq 1 60); do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "proxy failed to start; see $log" >&2
      [ -s "$log" ] && cat "$log" >&2
      return 1
    fi
    curl -s "localhost:$port/health/liveliness" >/dev/null && return 0
    sleep 1
  done
  echo "proxy failed to start (timeout); see $log" >&2
  [ -s "$log" ] && cat "$log" >&2
  return 1
}
