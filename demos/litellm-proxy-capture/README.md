# LiteLLM proxy capture — WRIG-1482 PoC

**Question:** can MemoryHub capture memories *implicitly*, outside the agent
harness, when the only client-side change is the LLM base URL?

```
agent (no MCP tool, no memory prompt, no hooks)
   │  OpenAI / Anthropic API
   ▼
LiteLLM proxy ─────────────────────────────► model provider
   │ async_log_success_event
   ▼
memoryhub_capture.py
   │ normalize → session key → delta → append
   ▼
MemoryHub thread (a2a_context_id = session key)
   │ thread(action="extract")   ← existing pipeline, dedup, provenance
   ▼
memories
```

The proxy does **not** implement extraction. It only turns LLM traffic into
MemoryHub conversation threads; everything after that is the existing
`write_memory` path (dedup, contradiction detection, provenance). Design notes
and the research framing live in
[`planning/proxy-memory-capture.md`](../../planning/proxy-memory-capture.md).

## Files

| File | Purpose |
|---|---|
| `memoryhub_capture.py` | LiteLLM `CustomLogger` callback (entry point) |
| `capture_core.py` | Pure logic: message normalization, session key, delta, observations |
| `sinks.py` | `JsonlSink` (offline) and `MemoryHubSink` (SDK `thread` ops) |
| `agent.py` | Memory-unaware agent with scripted scenarios |
| `verify.py` | `report` observations, list/extract proxy threads, search memories |
| `config.mock.yaml` | Offline proxy config (`mock_response`, no API keys) |
| `config.yaml` | Real models (OpenAI-compatible + Anthropic for Claude Code) |
| `demo.sh` + `DEMO.md` | The 10-minute demo: explicit harness integration vs. "only the base URL" |
| `agent_explicit.py` | Baseline agent that writes memories itself (today's harness integration) |
| `run-poc-local.sh` | Runs the whole PoC checklist against the personal edition and scores it |
| `run-poc-compare.sh` | Phase 4: modes A–E on isolated databases, writes `out/compare.md` |
| `tools/poc_summary.py` | Scores the checklist from the local database + observations |
| `tools/fake_extractor.py` | Rule-based stand-in for the extraction LLM (offline runs only) |
| `tools/harness_probe.py` | Anthropic-shaped traffic with tools and `<system-reminder>` |
| `tests/` | Unit tests (`pytest tests`) |

## Setup

```bash
cd demos/litellm-proxy-capture
uv venv && source .venv/bin/activate
uv pip install -r requirements.txt -e ../../sdk
cp .env.example .env    # fill in; never commit .env
```

## Phase 1 — offline, traffic → local log

```bash
./run-proxy.sh                                   # mock model, MEMORYHUB_CAPTURE_SINK=jsonl
python agent.py --scenario preferences           # no session header → fingerprinting
python agent.py --scenario correction --stream --session demo-1 --actor kate
python verify.py report                          # summary of out/observations.jsonl
less out/threads.jsonl                           # what would be written to MemoryHub
```

## Personal edition (SQLite, no cluster)

`MEMORYHUB_CAPTURE_SINK=local` writes into `memoryhub-local` — the same SQLite
database the local MCP server uses (`$XDG_DATA_HOME/memoryhub/memoryhub.db`).
The local edition has no HTTP endpoint, so the sink calls its services directly;
extraction runs in the proxy process through the same pipeline as
`memoryhub dream`, against any OpenAI-compatible endpoint.

```bash
uv pip install -e ../../memoryhub-local     # once, plus httpx for the dream path
./run-poc-local.sh                          # offline: mock agent model + rule-based extractor
./run-poc-local.sh real                     # real models from .env
```

The run creates its own database under `out/local-data/`, so it never touches a
working MemoryHub install. It walks the whole checklist — capture without the
agent, restart without duplicates, three ways of identifying a session,
ownership, per-scenario extraction, cost and latency, harness-shaped traffic —
prints `out/poc-report.txt` and ends with a PASS/PARTIAL table
(`tools/poc_summary.py`).

For a **real** local run set in `.env`:

```
MEMORYHUB_CAPTURE_SINK=local
MEMORYHUB_CAPTURE_EXTRACT_MODEL=llama3.2
MEMORYHUB_CAPTURE_EXTRACT_MODEL_URL=http://localhost:11434/v1   # Ollama
MEMORYHUB_CAPTURE_EXTRACT_API_KEY=                              # if the endpoint needs one
```

Two things must be real for the results to mean anything: the ONNX embedding
model (downloaded on first use; `memoryhub doctor` reports its status — without
it MemoryHub falls back to mock embeddings and search ranking is meaningless)
and a real extraction model instead of `tools/fake_extractor.py`.

Local and cluster editions differ in windowing, update-vs-create, circuit
breaker, S3 offload and identity, so extraction-quality results measured here
do not transfer to the cluster. See `planning/proxy-memory-capture.md`.

## Phase 2/3 — real model, MemoryHub threads, extraction

```bash
# .env: MEMORYHUB_CAPTURE_SINK=memoryhub, MEMORYHUB_URL / MEMORYHUB_API_KEY,
#       POC_MODEL / POC_MODEL_API_KEY, MEMORYHUB_CAPTURE_EXTRACT_EVERY=2
./run-proxy.sh real
python agent.py --scenario preferences --session demo-2
python verify.py threads            # session, extraction cursor, expiry date
python verify.py extract            # or rely on EXTRACT_EVERY
python verify.py search "dark mode" # expect: prefers dark mode, uses Python
python verify.py reextract <THREAD> # re-run over all messages after a failed extraction
```

### Cluster prerequisites and pitfalls

Extraction runs **inside the `memory-hub-mcp` pod**, not in the proxy. The pod
needs `MEMORYHUB_CONV_EXTRACTION_MODEL`, `..._MODEL_URL` and `..._API_KEY`
(the key cannot be passed per call; model and URL can, via
`MEMORYHUB_CAPTURE_EXTRACT_MODEL` / `_MODEL_URL`).

* **Read `failures` in every extraction result.** MemoryHub advances the
  thread's extraction cursor past windows that failed, so a misconfigured
  model silently "uses up" the messages. `extracted_count=0, failures=0` on a
  second call just means nothing new was left. Recover with `reextract`.
* The extraction request always sends `temperature: 0.0`, `max_tokens: 4000`
  and `response_format: json_object`. A model that rejects any of these returns
  400, which is not retried. Check the pod log line `Extraction failed for thread`.
* Restarting the pod (e.g. `oc set env`) kills the proxy's MCP session; the sink
  reconnects once and retries.
* Cluster dreaming never *updates* an existing memory (the LLM tiebreaker is not
  wired in), and the circuit breaker stops a run after 5 creates in a row for a
  user who already has memories. Call `extract` again for the rest.
* User-scope threads expire after 90 days and, by default policy, **delete the
  memories extracted from them** (`cascade_to_memories: delete`).

**Demo claim:** the agent never called MemoryHub, yet the preference is now a
governed memory with thread provenance.

## Phase 4 — compare capture modes

Run the same scenarios under each mode and compare with `verify.py report` plus
MemoryHub search results:

| Mode | How |
|---|---|
| A. explicit MCP only | agent with MemoryHub MCP, `MEMORYHUB_CAPTURE_ENABLED=false` |
| B. proxy only | this PoC, `EXTRACT_EVERY>0` |
| C. dreaming only | proxy appends only (`EXTRACT_EVERY=0`), then `verify.py extract` later (`memoryhub dream` works only on the local SQLite edition) |
| D. proxy + dreaming | B, then a later extraction pass over the same threads |
| E. explicit + proxy + dreaming | A + D — checks cross-source dedup |

Measure: memory recall / false positives (`smalltalk` scenario should create
nothing), duplicates and corrections (`correction` scenario: on the cluster
this currently yields two separate facts unless they are ≥0.98 similar, because
dreaming only creates or skips), extraction LLM tokens, latency to memory
availability, identity/session attribution correctness.

## Phase 5 — a real harness (Claude Code)

```bash
ANTHROPIC_BASE_URL=http://localhost:4000 \
ANTHROPIC_AUTH_TOKEN=$LITELLM_MASTER_KEY \
ANTHROPIC_CUSTOM_HEADERS="X-MemoryHub-Session: cc-demo" \
claude
```

Claude Code also makes small side calls (titles, summaries) on a fast model;
filter them with `MEMORYHUB_CAPTURE_IGNORE_MODELS=haiku`. Drop the custom
header to see how well fingerprinting alone groups a real session.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MEMORYHUB_CAPTURE_ENABLED` | `true` | Kill switch |
| `MEMORYHUB_CAPTURE_SINK` | `jsonl` | `jsonl`, `local` (personal edition) or `memoryhub` (cluster) |
| `MEMORYHUB_CAPTURE_EXTRACT_EVERY` | `0` | Trigger extraction every N user turns (0 = never) |
| `MEMORYHUB_CAPTURE_TOOLS` | `false` | Also append `tool_call` / `tool_result` messages |
| `MEMORYHUB_CAPTURE_MAX_MESSAGE_BYTES` | `8000` | Truncate longer messages (MemoryHub moves >8192 B to S3 and extraction then sees only a placeholder); `0` disables |
| `MEMORYHUB_CAPTURE_IGNORE_MODELS` | – | Regex on model / model group to skip |
| `MEMORYHUB_CAPTURE_SCOPE` / `_SCOPE_ID` | `user` / – | Thread scope (`project` uses `X-MemoryHub-Project` if sent) |
| `MEMORYHUB_CAPTURE_EXTRACT_MODEL` / `_MODEL_URL` / `_API_KEY` | server default | Extraction model override; local mode requires the URL (the key is used only locally) |
| `MEMORYHUB_CAPTURE_OBSERVATIONS` | `./out/observations.jsonl` | Research log |

Optional client headers: `X-MemoryHub-Session`, `X-MemoryHub-Actor`,
`X-MemoryHub-Project`. The OpenAI `user` field is used as the actor when no
header is sent. `Authorization` / `x-api-key` are never logged.

## Known limitations (by design — these are findings to measure)

* **Session boundaries are inferred** when no header is sent (hash of the first
  user message). Running the same scenario twice without `--session` lands in
  the same thread, and turns identical to earlier ones are treated as already
  captured. Use `--session` for demos.
* **Identity comes from the proxy key**, not the human. With the master key
  every call is `default_user_id`; per-user virtual keys or headers are needed.
* **Threads are written as the proxy's MemoryHub identity**, so the proxy's user
  owns the memories; the observed actor is kept in message metadata only.
* **System prompts are not captured** (volatile in real harnesses), and
  `<system-reminder>` blocks are stripped from user text (Claude Code injects them).
* **Model reasoning (`thinking`) is never persisted**; images are replaced by placeholders.
* **Delta state is in-process.** After a restart the sink re-finds the thread via
  `a2a_context_id` and seeds the delta from the stored messages (up to 5000).
  Messages are committed one by one, so a failure mid-turn does not duplicate
  what was already stored.
* No queue: if MemoryHub is down the turn is recorded as an error in the
  observations and appended on the next call of that session (the delta still
  contains it), but only if the session continues.
* The `out/observations.jsonl` log accumulates across runs and sinks; delete
  `out/` between experiments for clean reports.
