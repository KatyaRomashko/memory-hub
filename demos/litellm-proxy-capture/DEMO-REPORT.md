# WRIG-1482 — Implicit memory capture via an LLM gateway: demo run report

**Date:** 23 September 2026
**Author:** Kateryna Romashko
**Branch:** `feat/wrig-1482-litellm-proxy-capture` (uncommitted at time of writing)
**Edition under test:** MemoryHub personal edition (SQLite, local)
**Status:** full walkthrough executed end to end; no video recorded yet

---

## 1. What this run was for

WRIG-1482 asks whether memory capture can move out of each agent harness (MCP
tool + prompt instructions + hooks) and into the **LLM gateway**, so that the
only client-side change is the base URL.

The PoC is a LiteLLM proxy with a `CustomLogger` callback. For every successful
LLM call it normalises the request and response, derives a session key, appends
only the *new* messages to a MemoryHub conversation thread, and every N user
turns triggers MemoryHub's existing extraction ("dreaming") pipeline.

This run had two goals:

1. **Execute the whole flow by hand**, in several terminals, so that every
   internal step is observable: what the proxy sees, what lands in the thread,
   where the extraction cursor is, what text the extraction model receives, how
   create/update/skip decisions are made, and how memories are retrieved.
2. **Record what breaks.** Everything that failed is included below with its
   cause, the fix, and the re-run that verified it.

Out of scope for this run: the cluster edition (no access), and memory
*injection* into prompts — this PoC covers capture only.

---

## 2. Environment

| | |
|---|---|
| Host | macOS, zsh |
| Repo | `~/MemHub/memory-hub` |
| Demo | `demos/litellm-proxy-capture` |
| Gateway | LiteLLM proxy, port 4000 |
| Agent model | `qwen2.5:7b` via Ollama (`ollama_chat/qwen2.5:7b`) |
| Extraction model | `qwen2.5:7b` via Ollama OpenAI-compatible endpoint |
| Embeddings | ONNX `granite-embedding-small-english-r2` (real, not mock) |
| Database | `demos/litellm-proxy-capture/out/live-data/memoryhub/memoryhub.db` |
| Capture settings | `SINK=local`, `EXTRACT_EVERY=2`, `TOOLS=false`, `WHEN_AGENT_WRITES=defer` |

The same model serves the agent and the extraction pass so Ollama does not swap
weights between answering and extracting.

Supporting tooling written for this run, in `demos/litellm-proxy-capture/live/`:

| file | purpose |
|---|---|
| `env.sh` | shared environment, sourced in every terminal; isolated database; never sources `.env` |
| `check.sh` | preflight: packages, ONNX model presence, extraction endpoint, free port |
| `proxy.sh` | starts LiteLLM (`ollama` / `real` / mock) |
| `watch.py` | follows the observation log, one readable line per intercepted call |
| `peek.py` | read-only view of the SQLite database: threads, cursor, windows, decisions, memories |

---

## 3. Terminal layout

| | role |
|---|---|
| **T1** | LiteLLM proxy |
| **T2** | live chat with the agent (session `demo-live`) |
| **T3** | `live/watch.py` — what the proxy sees |
| **T4** | `live/peek.py` / `verify.py` — what MemoryHub holds |
| **T5** | second live chat (session `demo-live-2`) |

---

## 4. Part A — Preparation

### A1, attempt 1 — failed

**T1**

```console
$ cd ~/MemHub/memory-hub/demos/litellm-proxy-capture
$ source live/env.sh
$ python -c "import litellm, memoryhub_local; print('ok')"
$ memoryhub doctor | head -20

live demo environment
  database      /Users/kromashk/MemHub/memory-hub/demos/out/live-data/memoryhub/memoryhub.db
  observations  /Users/kromashk/MemHub/memory-hub/demos/out/live-observations.jsonl
  extraction    every 2 user turns -> qwen2.5:7b @ http://localhost:11434/v1
  proxy         http://localhost:4000

ok
Usage: memoryhub [OPTIONS] COMMAND [ARGS]...
Try 'memoryhub --help' for help.
╭─ Error ──────────────────────────────────────────────────────────────╮
│ No such command 'doctor'.                                            │
╰──────────────────────────────────────────────────────────────────────╯
```

**Two problems.**

1. **The database path is one level too high** (`demos/out/...` instead of
   `demos/litellm-proxy-capture/out/...`). `env.sh` located its own directory via
   `${BASH_SOURCE[0]}`, which does not exist in zsh; `dirname ""` resolved to
   `.`, so `cd ..` went to `demos/`.
2. **`memoryhub doctor` does not exist** for the personal edition. `doctor` is
   defined in `memoryhub-cli/src/memoryhub_cli/main.py:2310`, the *cluster* CLI,
   and is about cluster connectivity — it would not have reported anything about
   local embeddings anyway.

**A third, more consequential problem surfaced while fixing these.** The ONNX
embedding model is cached at `$XDG_DATA_HOME/memoryhub/models/…`
(`embeddings/onnx.py:get_default_model_dir`). The demo overrides
`XDG_DATA_HOME` to isolate its database — which also hides an already-downloaded
model, so `startup.py` falls back to `MockEmbeddingService` **silently**. This is
the root cause of every earlier PoC run reporting `embeddings: mock`, and of
`update`/`skip` reconciliation never firing in those runs (similarity was always
empty).

**Fixes applied.**

- `env.sh` resolves its root from `$PWD` (with a zsh/bash fallback) and refuses
  to run from the wrong directory.
- `env.sh` symlinks the real model cache (`~/.local/share/memoryhub/models`) into
  the isolated data directory, and prints the resulting embedding mode.
- New `live/check.sh` replaces `memoryhub doctor`: it checks packages, calls
  `is_model_downloaded()` directly, probes the extraction endpoint, and checks
  the port.

### A1, attempt 2 — partially green

**T1**

```console
$ cd ~/MemHub/memory-hub/demos/litellm-proxy-capture
$ rm -rf ~/MemHub/memory-hub/demos/out
$ source live/env.sh
$ ./live/check.sh

live demo environment
  dir           /Users/kromashk/MemHub/memory-hub/demos/litellm-proxy-capture
  database      /Users/kromashk/MemHub/memory-hub/demos/out/live-data/memoryhub/memoryhub.db
  embeddings    onnx
  observations  /Users/kromashk/MemHub/memory-hub/demos/litellm-proxy-capture/out/live-observations.jsonl
  extraction    every 2 user turns -> qwen2.5:7b @ http://localhost:11434/v1
  proxy         http://localhost:4000

live demo preflight

  OK         environment sourced (sink=local, db=.../demos/out/live-data/memoryhub/memoryhub.db)
  OK         python packages: litellm, memoryhub_local, openai
  OK         embeddings: onnx model present
             .../demos/out/live-data/memoryhub/models/granite-embedding-small-english-r2-onnx
  OK         extraction endpoint answers: http://localhost:11434/v1
  FAIL       ollama has no qwen2.5:7b -- run: ollama pull qwen2.5:7b
  OK         port 4000 free for the proxy
  OK         database not created yet -- clean start

  6 ok, 1 to fix
```

`dir` is now correct and **embeddings are `onnx`** — the model-cache fix worked.
But `database` still points at `demos/out/...`: `POC_DATA_DIR` was already
exported in that shell by the *broken* first run, and `env.sh` honoured it via
`${POC_DATA_DIR:-…}`.

**Fix:** `POC_DATA_DIR` is now always derived from `DEMO_ROOT`; an explicit
override moved to a separate variable (`MEMORYHUB_LIVE_DATA_DIR`). `check.sh`
was also changed to list the models Ollama actually has when the configured one
is missing.

### A2 — extraction model

**T1**

```console
$ ollama list
NAME                    ID              SIZE      MODIFIED
qwen2.5:7b              845dbda0ea48    4.7 GB    About a minute ago
llama3.2:latest         a80c4f17acd5    2.0 GB    2 weeks ago
llama3.1:latest         46e0c10c039e    4.9 GB    2 months ago
qwen3-embedding:0.6b    ac6da0dfba84    639 MB    2 months ago

$ ollama run qwen2.5:7b "answer with one word: does it work?"
Yes.
```

### A1, attempt 3 — green

**T1**

```console
$ cd ~/MemHub/memory-hub/demos/litellm-proxy-capture
$ rm -rf ~/MemHub/memory-hub/demos/out out/live-data out/live-observations.jsonl
$ source live/env.sh
$ ./live/check.sh

live demo environment
  dir           /Users/kromashk/MemHub/memory-hub/demos/litellm-proxy-capture
  database      /Users/kromashk/MemHub/memory-hub/demos/litellm-proxy-capture/out/live-data/memoryhub/memoryhub.db
  embeddings    onnx
  observations  /Users/kromashk/MemHub/memory-hub/demos/litellm-proxy-capture/out/live-observations.jsonl
  extraction    every 2 user turns -> qwen2.5:7b @ http://localhost:11434/v1
  agent model   ollama_chat/qwen2.5:7b (only for ./live/proxy.sh ollama)
  proxy         http://localhost:4000

live demo preflight

  OK         environment sourced (sink=local, db=.../litellm-proxy-capture/out/live-data/memoryhub/memoryhub.db)
  OK         python packages: litellm, memoryhub_local, openai
  OK         embeddings: onnx model present
  OK         extraction endpoint answers: http://localhost:11434/v1
  OK         extraction model pulled: qwen2.5:7b
  OK         port 4000 free for the proxy
  OK         database not created yet -- clean start

  7 ok, 0 to fix
```

A third proxy mode (`config.ollama.yaml`, `./live/proxy.sh ollama`) was added at
this point so the agent's own model is also local: no API key is read, nothing
leaves the machine, and `.env` never has to be opened on screen.

---

## 5. Part B — The run

### Step 1 — start the gateway

**T1, attempt 1**

```console
$ ./live/proxy.sh ollama
agent model: ollama_chat/qwen2.5:7b via http://localhost:11434
config: config.ollama.yaml   port: 4000
INFO:     Started server process [89486]
INFO:     Waiting for application startup.
...
LiteLLM: Proxy initialized with Config, Set models:
    poc-model
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:4000 (Press CTRL+C to quit)
```

**Problem:** the callback's own `memoryhub_capture: ready …` line is missing.

**Cause:** the callback logs through `logging.getLogger("memoryhub_capture")`,
and LiteLLM leaves the root logger at `WARNING`, so `log.info` is dropped.
Raising LiteLLM's own log level would have worked but buries the capture lines
in LiteLLM's noise.

**Fix:** attach a dedicated `StreamHandler` to the `memoryhub_capture` logger at
import, with `propagate = False` and a level from
`MEMORYHUB_CAPTURE_LOG_LEVEL` (default `INFO`). The proxy terminal now shows
capture lines and nothing else from us.

**T1, attempt 2**

```console
$ ./live/proxy.sh ollama
...
13:25:40  memoryhub_capture: ready (sink=local extract_every=2 tools=False max_bytes=8000 when_agent_writes=defer)
LiteLLM: Proxy initialized with Config, Set models:
    poc-model
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:4000 (Press CTRL+C to quit)
```

### Step 2 — observers

**T3**

```console
$ source live/env.sh
$ python live/watch.py
watching /Users/kromashk/MemHub/.../out/live-observations.jsonl  (Ctrl-C to stop)
```

**T4**

```console
$ source live/env.sh
$ python live/peek.py state
------------------------------------------------------------------------------
/Users/kromashk/MemHub/.../out/live-data/memoryhub/memoryhub.db

no database yet -- MemoryHub is empty.
It is created the first time the proxy stores a message.
------------------------------------------------------------------------------
```

The database does not exist yet — a clean starting point.

### Step 3 — first live turn

**T2, attempt 1**

```console
$ source live/env.sh
$ python agent.py --interactive --session demo-live
...
Traceback (most recent call last):
  File ".../agent.py", line 17, in <module>
    from openai import OpenAI
ModuleNotFoundError: No module named 'openai'
```

**Cause:** the repository-root virtualenv was active; `openai` is installed in
the demo's own `.venv`. `env.sh` only activated `.venv` when no virtualenv was
active at all.

**Fix:** `env.sh` now switches to the demo venv even when another one is active,
and prints which `python` is in use.

**T2, attempt 2**

```console
$ source live/env.sh
$ python agent.py --interactive --session demo-live

live demo environment
  dir           /Users/kromashk/MemHub/memory-hub/demos/litellm-proxy-capture
  python        /Users/kromashk/MemHub/.../litellm-proxy-capture/.venv/bin/python
  database      .../out/live-data/memoryhub/memoryhub.db
  embeddings    onnx
  ...

user> Hi, I'm developing a memory system for agents. I always work in dark mode and write scripts in Python.
agent> Hello! It sounds like you're working on an interesting project. Developing a memory system for agents can be quite challenging but also rewarding. Since you mentioned that you always work in dark mode and use Python, here are some tips and suggestions to help you get started: ...
```

**T3**

```console
12:04:49  demo-live      (header)  msgs 2  new 2 [user,assistant]  appended 2  thread 0cd22719
```

**T1**

```console
12:04:52  memoryhub_capture: local MemoryHub backend ready
12:04:52  memoryhub_capture: session=demo-live(header) new=2 appended=2 skipped=None rewritten=False agent_writes=0 extract_skipped=None
```

**T4**

```console
$ python live/peek.py state
------------------------------------------------------------------------------
threads 1   messages 2   memories 0   decisions 0   failed windows 0
------------------------------------------------------------------------------

session  demo-live   thread 0cd22719   owner root
  source=litellm-proxy  session_source=header  observed_actor=default_user_id (virtual_key_user)
  messages=2  cursor=0  waiting for extraction=2  memories=0
```

Reading: the session key came from the `X-MemoryHub-Session` header
(`session_source=header`); without it the proxy falls back to the LiteLLM
session id, then to a fingerprint of the first user message. `new 2` is the
delta — chat APIs resend the whole history on every call, so the proxy computes
the longest common prefix and stores only what is new.

### Step 4 — extraction fires — **failed**

**T2**

```console
user> We decided today that the team's staging cluster moves to OpenShift 4.19 next Monday.
agent> That sounds like a significant move for your team! Transitioning to OpenShift 4.19 involves several preparatory steps ...
```

**T3** — no new line appeared at all.

**T1**

```console
12:14:37 - LiteLLM:ERROR: logging_worker.py:166 - LoggingWorker error:
Traceback (most recent call last):
  File ".../asyncio/tasks.py", line 520, in wait_for
    return await fut
  File ".../litellm/litellm_core_utils/litellm_logging.py", line 3146, in _async_success_handler_body
    await callback.async_log_success_event(
  File ".../memoryhub_capture.py", line 220, in _capture
    obs.extraction = await self.sink.extract(state.thread_id)
  File ".../sinks.py", line 304, in extract
    return await extract_from_thread(
  File ".../memoryhub_local/services/extraction.py", line 481, in extract_from_thread
    results = await extract_window(
  File ".../memoryhub_local/services/extraction.py", line 255, in extract_window
    raw_extractions = await llm_fn(formatted)
  ...
asyncio.exceptions.CancelledError

The above exception was the direct cause of the following exception:
...
  File ".../litellm/litellm_core_utils/logging_worker.py", line 161, in _process_log_task
    await asyncio.wait_for(
...
TimeoutError
```

**T4**

```console
$ python live/peek.py state
------------------------------------------------------------------------------
threads 1   messages 4   memories 5   decisions 5   failed windows 0
------------------------------------------------------------------------------

session  demo-live   thread 0cd22719   owner root
  messages=4  cursor=0  waiting for extraction=4  memories=4

$ python live/peek.py memories
------------------------------------------------------------------------------
memories: 5
------------------------------------------------------------------------------

[extraction] weight=0.8  2026-09-23 11:14:32
  The user always works in dark mode and writes scripts in Python.
  <- thread 0cd22719 (demo-live) messages [1, 2]

[extraction] weight=0.7  2026-09-23 11:14:32
  SQLite can be used for simple in-memory storage of memories.
  <- thread 0cd22719 (demo-live) messages [1, 2]

[extraction] weight=0.6  2026-09-23 11:14:32
  Natural Language Processing (NLP) libraries like spaCy or Hugging Face Transformers can be utilized ...
  <- thread 0cd22719 (demo-live) messages [1, 2]

[extraction] weight=0.7  2026-09-23 11:14:32
  A simple memory retrieval mechanism based on similarity using TF-IDF or embeddings from models like BERT ...
  <- thread 0cd22719 (demo-live) messages [1, 2]

[extraction] weight=0.8  2026-09-23 11:14:32
  The user needs to ensure their development environment supports dark mode for better readability and comfort.
  <- NO thread provenance (written directly, not by extraction)
```

**Cause 1 — ours.** LiteLLM wraps every logging callback in
`asyncio.wait_for(..., timeout=LOGGING_WORKER_MAX_TIME_PER_COROUTINE)`,
**20 seconds by default** (`litellm/constants.py:554`). Extraction is a second
LLM call over two windows; it exceeded that and the task was cancelled
mid-request.

**Cause 2 — MemoryHub.** The cancellation landed in a gap inside the pipeline:
`create_memory` commits each memory as it is written
(`services/memory.py:77`), while the extraction cursor is committed only at the
very end of `extract_from_thread` (`services/extraction.py:509`). The result is
a database where memories exist, the cursor has not moved (`cursor=0`,
`waiting for extraction=4`), and one memory has no `conversation_extractions`
row at all. A subsequent pass would re-process the same messages and duplicate
them.

**Cause 3 — extraction quality.** Four of the five "facts" are the *model's own
advice* (SQLite, spaCy, TF-IDF), not anything the user said or decided — stored
with thread provenance `messages [1, 2]`, so they read as if they had been
agreed in the conversation. See Finding F4.

**Fixes applied.**

- Extraction is now scheduled as a detached `asyncio.Task`, outside LiteLLM's
  callback timeout. The callback returns immediately; the result is written as a
  second observation line when the pass completes. Added `drain()` and a
  regression test asserting the callback does not wait for extraction.
- The demo agent's system prompt was shortened to at most three sentences, so a
  verbose reply does not dominate the extraction window, with a `--chatty` flag
  to demonstrate the problem deliberately.

**Verification.**

```console
$ rm -rf out/live-data out/live-observations.jsonl
$ source live/env.sh
$ python -m pytest tests -q
................F........                                             [100%]
FAILED tests/test_handler.py::test_flags_are_re_read_from_the_environment - assert 2 == 0
1 failed, 24 passed
```

That failure was also environmental: the test builds a logger "with
`EXTRACT_EVERY` unset", but the shell had `MEMORYHUB_CAPTURE_EXTRACT_EVERY=2`
exported by `live/env.sh`. **Fix:** the fixture now deletes every
`MEMORYHUB_CAPTURE_*` variable before constructing a logger.

### Steps 3–4, re-run on a clean database

**T2**

```console
user> For the upcoming 'Nexus' platform upgrade, we decided to migrate our entire backend to Go. The API documentation must be finalized by Friday
agent> Migrating your backend to Go for the 'Nexus' platform upgrade is a smart choice, given Go's efficiency and concurrency model. Ensure thorough testing as you finalize the API documentation by Friday to avoid last-minute issues. Consider involving team members who are familiar with Go to expedite the process.

user> The API must stay backwards compatible with v2 clients until the end of the year.
agent> To maintain backward compatibility with v2 clients until the end of the year, carefully document any changes and test extensively before deploying updates. Consider implementing a fallback mechanism in your new Go backend to handle v2 client requests as needed.
```

**T3**

```console
12:43:15  demo-live      (header)  msgs 2  new 2 [user,assistant]  appended 2  thread 02ece523
12:46:26  demo-live      (header)  msgs 4  new 2 [user,assistant]  appended 2  thread 02ece523
          extraction scheduled -- runs in the background, line follows
12:46:35                  extraction finished: 3 memories from 2 window(s), cursor 4, 8928 ms
```

**T1**

```console
12:46:26  memoryhub_capture: session=demo-live(header) new=2 appended=2 skipped=None rewritten=False agent_writes=0 extract_skipped=scheduled in background
12:46:35  memoryhub_capture: extraction on 02ece523 -- 3 memories, 2 window(s), cursor 4, 8928 ms
```

**T4**

```console
$ python live/peek.py memories
[extraction] weight=0.9  The entire backend for the 'Nexus' platform upgrade will be migrated to Go.
  <- thread 02ece523 (demo-live) messages [1, 2]
[extraction] weight=0.8  API documentation must be finalized by Friday.
  <- thread 02ece523 (demo-live) messages [1, 2]
[extraction] weight=0.9  The API must remain backward compatible with v2 clients until the end of the year.
  <- thread 02ece523 (demo-live) messages [3, 4]
```

The agent's reply arrived without waiting for extraction (8.9 s of extraction ran
after the response was already delivered), no traceback appeared, the cursor
advanced to 4, and all three memories are grounded facts.

### Step 5 — what the extraction model actually receives

A third user turn was added so an unprocessed window would exist (extraction
fires on every second user turn, so this one does not trigger it).

**T2**

```console
user> Let's also move the CI pipeline to Tekton.
agent> Moving your CI pipeline to Tekton is a good idea for better orchestration and scaling capabilities. Ensure you update your workflows and integrations accordingly, and test the new pipeline thoroughly before full deployment.
```

**T4**

```console
$ python live/peek.py thread
------------------------------------------------------------------------------
thread 02ece523-847b-423a-960b-c1e40bee9ea9   session demo-live   cursor 4
------------------------------------------------------------------------------
     1 user      [litellm-proxy] For the upcoming 'Nexus' platform upgrade, we decided to migrate ou…
     2 assistant [litellm-proxy] Migrating your backend to Go for the 'Nexus' platform upgrade is a …
     3 user      [litellm-proxy] The API must stay backwards compatible with v2 clients until the en…
     4 assistant [litellm-proxy] To maintain backward compatibility with v2 clients until the end of…
>>   5 user      [litellm-proxy] Let's also move the CI pipeline to Tekton.
>>   6 assistant [litellm-proxy] Moving your CI pipeline to Tekton is a good idea for better orchest…

   = already extracted (seq <= cursor 4)
  >> = still waiting

$ python live/peek.py next
------------------------------------------------------------------------------
thread 02ece523  cursor 4  unprocessed messages 2
------------------------------------------------------------------------------
MemoryHub would split them into 1 window(s) (max 10 messages, or a new user turn after an assistant reply).
Each window is sent to the extraction model exactly like this:

--- window 1  (seq 5..6) ---
[USER] (seq=5): Let's also move the CI pipeline to Tekton.
[ASSISTANT] (seq=6): Moving your CI pipeline to Tekton is a good idea for better orchestration and scaling capabilities. Ensure you update your workflows and integrations accordingly, and test the new pipeline thoroughly before full deployment.
```

This is the clearest view of the mechanism: "dreaming" receives a flat text
window with sequence numbers, nothing more.

### Steps 6 and 8 — reconciliation and the cost of `update`

**T2**

```console
user> after a meeting we decided to migrate the backend to Rust, not Go
agent> After the meeting, migrating the backend to Rust instead of Go is the new plan. Ensure you have the necessary expertise in Rust and plan a smooth transition by testing key functionalities along the way. Set clear milestones to monitor progress and address any challenges early.
```

**T3**

```console
13:08:04  demo-live      (header)  msgs 8  new 2 [user,assistant]  appended 2  thread 02ece523
          extraction scheduled -- runs in the background, line follows
13:08:11                  extraction finished: 2 memories from 2 window(s), cursor 8, 7169 ms
```

**T4**

```console
$ python live/peek.py decisions
------------------------------------------------------------------------------
reconciliation: every candidate the extractor produced
skip >= 0.98   update >= 0.85   below that -> create
------------------------------------------------------------------------------

run dream:qwen2.5:7b:1.0:2026-09-23T11:46:26.833803+00:00
  create  score=   -    The entire backend for the 'Nexus' platform upgrade…
          reason: no similar memory found
  create  score=   -    API documentation must be finalized by Friday.
          reason: no similar memory found
  update  score=0.882  The API must remain backward compatible with v2 cli…
          reason: similar memory found (similarity=0.8819)
          nearest: API documentation must be finalized by Friday.

run dream:qwen2.5:7b:1.0:2026-09-23T12:08:04.388444+00:00
  create  score=   -    Move the CI pipeline to Tekton for improved orchest…
          reason: no similar memory found
  update  score=0.867  The decision was made to migrate the backend from G…
          reason: similar memory found (similarity=0.8668)
          nearest: The entire backend for the 'Nexus' platform upgrade will be…
```

At this point `peek.py memories` still listed five memories and both API facts
looked alive. A direct query of the database showed otherwise:

```console
$ python3 - <<'PY'
... select id, version, is_current, previous_version_id, content from memory_nodes ...
PY
f6652e51 v1 cur=0 prev=None      The entire backend for the 'Nexus' platform upgrade will be migrated to Go.
33e35404 v1 cur=0 prev=None      API documentation must be finalized by Friday.
8b39161c v2 cur=1 prev=33e35404  The API must remain backward compatible with v2 clients until the end of the year.
78d2025c v1 cur=1 prev=None      Move the CI pipeline to Tekton for improved orchestration and scaling.
8a065046 v2 cur=1 prev=f6652e51  The decision was made to migrate the backend from Go to Rust.
```

**What `update` actually does:** it writes a *new version* and retires the
previous row (`is_current = 0`). The old fact disappears from current memory —
the row survives, but search will not return it.

- `Go → Rust` (0.8668): correct. The correction superseded the stale decision.
- `API docs by Friday → API backward compatible with v2` (0.8819): **wrong**.
  Two unrelated facts merged on topic similarity alone; the Friday deadline is
  gone from current memory with no warning.

**Tooling fix:** `peek.py memories` was showing retired versions as if live. It
now prints current memories, marks what each version replaced, and hides
superseded rows behind `--all`; `peek.py state` counts current memories only.

```console
$ python live/peek.py memories
------------------------------------------------------------------------------
current memories: 3   superseded: 2
------------------------------------------------------------------------------

[extraction] v2 weight=0.9  2026-09-23 11:46:35
  The API must remain backward compatible with v2 clients until the end of the year.
  <- thread 02ece523 (demo-live) messages [3, 4]
  ** this version REPLACED an earlier memory, which is no longer current:
     API documentation must be finalized by Friday.

[extraction] v1 weight=0.9  2026-09-23 12:08:08
  Move the CI pipeline to Tekton for improved orchestration and scaling.
  <- thread 02ece523 (demo-live) messages [5, 6]

[extraction] v2 weight=0.9  2026-09-23 12:08:11
  The decision was made to migrate the backend from Go to Rust.
  <- thread 02ece523 (demo-live) messages [7, 8]
  ** this version REPLACED an earlier memory, which is no longer current:
     The entire backend for the 'Nexus' platform upgrade will be migrated …

2 superseded row(s) hidden -- `peek.py memories --all` to see them
```

### Step 9 — where the filter actually sits

**T2**

```console
user> What's a good way to boil an egg?
agent> Boil an egg by bringing water to a rapid simmer in a pot, then carefully place the egg into the water and cook for 9 minutes for a soft-boiled egg or 12 minutes for a hard-boiled egg.

user> Thanks!
agent> You're welcome! Enjoy your perfectly boiled eggs.
```

**T3**

```console
13:22:23  demo-live      (header)  msgs 10  new 2 [user,assistant]  appended 2  thread 02ece523
13:22:26  demo-live      (header)  msgs 12  new 2 [user,assistant]  appended 2  thread 02ece523
          extraction scheduled -- runs in the background, line follows
13:22:26                  extraction finished: 1 memories from 2 window(s), cursor 12, 6482 ms
```

**T4**

```console
$ python live/peek.py decisions | tail -5
run dream:qwen2.5:7b:1.0:2026-09-23T12:22:26.375399+00:00
  create  score=   -    Boil an egg by bringing water to a rapid simmer in …
          reason: no similar memory found

$ python live/peek.py memories | tail -6
[extraction] v1 weight=0.7  2026-09-23 12:22:32
  Boil an egg by bringing water to a rapid simmer in a pot, then carefully place the egg into the water and cook for 9 minutes for a soft-boiled egg or 12 minutes for a hard-boiled egg.
  <- thread 02ece523 (demo-live) messages [9, 10]

$ python live/peek.py thread
...
     9 user      [litellm-proxy] What's a good way to boil an egg?
    10 assistant [litellm-proxy] Boil an egg by bringing water to a rapid simmer in a pot, then care…
    11 user      [litellm-proxy] Thanks!
    12 assistant [litellm-proxy] You're welcome! Enjoy your perfectly boiled eggs.
```

"Thanks!" was filtered; the recipe was stored. The prompt's filter is about the
*shape* of the utterance ("do not extract greetings or procedural chatter"), not
about whose knowledge it is — it explicitly asks for "facts, preferences,
decisions, **and knowledge** worth remembering". See Finding F4.

The thread holds all twelve messages: the proxy filters nothing. Selection is
MemoryHub's job, which is the point of removing thread-writing instructions from
each harness.

### Step 10 — proxy restart mid-conversation

**T1**

```console
^CINFO:     Shutting down
INFO:     Application shutdown complete.
$ ./live/proxy.sh ollama
...
13:25:40  memoryhub_capture: ready (sink=local extract_every=2 tools=False max_bytes=8000 when_agent_writes=defer)
...
13:26:02  memoryhub_capture: local MemoryHub backend ready
13:26:02  memoryhub_capture: session=demo-live(header) new=2 appended=2 skipped=None rewritten=False agent_writes=0 extract_skipped=None
```

**T2** (same chat session, not restarted)

```console
user> One more thing: all new services must ship with OpenTelemetry tracing from day one.
agent> To ensure all new services are shipped with OpenTelemetry tracing from day one, integrate the OpenTelemetry SDK into your service's codebase during development. ...
```

**T3**

```console
13:26:01  demo-live      (header)  msgs 14  new 2 [user,assistant]  appended 2  thread 02ece523
```

**T4**

```console
$ python live/peek.py thread
------------------------------------------------------------------------------
thread 02ece523-847b-423a-960b-c1e40bee9ea9   session demo-live   cursor 12
------------------------------------------------------------------------------
     1 user      ...
    ...
    12 assistant [litellm-proxy] You're welcome! Enjoy your perfectly boiled eggs.
>>  13 user      [litellm-proxy] One more thing: all new services must ship with OpenTelemetry traci…
>>  14 assistant [litellm-proxy] To ensure all new services are shipped with OpenTelemetry tracing f…
```

`new 2`, not `new 14`: on restart the proxy reused the thread by
`a2a_context_id` and seeded its delta state from the messages already stored
(`seed_state`). No duplicates.

### Step 11 — session isolation

**T5**

```console
$ source live/env.sh
$ python agent.py --interactive --session demo-live-2

user> I'm on the data platform team. We use Airflow for scheduling and our warehouse is Snowflake.
agent> That sounds like a common setup for managing ETL workflows; Airflow integrates well with Snowflake to schedule data processing tasks efficiently. ...
```

**T4**

```console
$ python live/peek.py state
threads 2   messages 16   current memories 4   decisions 6   failed windows 0

session  demo-live     thread 02ece523   messages=14  cursor=12  waiting for extraction=2
session  demo-live-2   thread 5eb10c3e   messages=2   cursor=0   waiting for extraction=2
```

Two threads, separate cursors, one proxy and one model.

### Step 12 — retrieval

**T4, attempt 1**

```console
$ python verify.py search "backend language"
query 'backend language': 4 result(s), owner=root
- [extraction] thread=02ece523… msgs=[7, 8]
  The decision was made to migrate the backend from Go to Rust.
- [extraction] thread=02ece523… msgs=[3, 4]
  The API must remain backward compatible with v2 clients until the end of the year.
- [extraction] thread=02ece523… msgs=[5, 6]
  Move the CI pipeline to Tekton for improved orchestration and scaling.
- [extraction] thread=02ece523… msgs=[9, 10]
  Boil an egg by bringing water to a rapid simmer in a pot, …

$ python verify.py search "scheduling"
query 'scheduling': 4 result(s), owner=root
- [extraction] … Move the CI pipeline to Tekton for improved orchestration and scaling.
- [extraction] … The decision was made to migrate the backend from Go to Rust.
- [extraction] … Boil an egg by bringing water to a rapid simmer in a pot, …
- [extraction] … The API must remain backward compatible with v2 clients until the end of the year.
```

Two observations. The Airflow fact is absent — correctly: the second session had
made only one user turn, so extraction had not run yet (`cursor=0`). And the
retired "Go" memory does not appear — `search_memories` filters on `is_current`.

But every stored memory is returned for both queries. `search_memories` takes
the top *N* (default 10) by score with **no relevance cut-off**.

**Tooling fix:** `verify.py search` now prints rank and `relevance_score`.

**T5**

```console
user> We also need to backfill the events table before the end of the quarter.
agent> To backfill the events table, you can create an Airflow DAG that runs tasks to process historical data up to the start of the current quarter. ...
```

**T3**

```console
13:41:43  demo-live-2    (header)  msgs 4  new 2 [user,assistant]  appended 2  thread 5eb10c3e
          extraction scheduled -- runs in the background, line follows
13:41:51                  extraction finished: 2 memories from 2 window(s), cursor 4, 8174 ms
```

**T4, attempt 2**

```console
$ python verify.py search "data warehouse"
query 'data warehouse': 5 result(s), owner=root
search has NO relevance cut-off: it returns the top matches by score,
filtered to the current version of each memory (superseded ones never appear).
1. score=0.8091  [extraction]  thread=5eb10c3e… msgs=[3, 4]
   Create an Airflow DAG with Snowflake operators to backfill the events table up to the start of the current quarter.
2. score=0.7724  [extraction]  thread=02ece523… msgs=[5, 6]
   Move the CI pipeline to Tekton for improved orchestration and scaling.
3. score=0.7604  [extraction]  thread=02ece523… msgs=[3, 4]
   The API must remain backward compatible with v2 clients until the end of the year.
4. score=0.7597  [extraction]  thread=02ece523… msgs=[7, 8]
   The decision was made to migrate the backend from Go to Rust.
5. score=0.7141  [extraction]  thread=02ece523… msgs=[9, 10]
   Boil an egg by bringing water to a rapid simmer in a pot, …

$ python verify.py search "backend language"
1. score=0.8334  The decision was made to migrate the backend from Go to Rust.
2. score=0.8106  The API must remain backward compatible with v2 clients until the end of the year.
3. score=0.78    Create an Airflow DAG with Snowflake operators to backfill the events table …
4. score=0.7756  Move the CI pipeline to Tekton for improved orchestration and scaling.
5. score=0.7287  Boil an egg by bringing water to a rapid simmer in a pot, …
```

Ranking is sensible in both cases, and memory written in one session is
retrievable from the other (threads are isolated, memory is per owner). But all
scores sit in a narrow 0.71–0.83 band, and everything is returned.

The second session also produced a second false merge, confirmed against the
database:

```console
$ python3 - <<'PY'
... select action, similarity_score, candidate_content from reconciliation_decisions ...
PY
create  score=None
   The data platform team uses Airflow for scheduling tasks and Snowflake as the data warehouse.
update  score=0.8855067456109604
   Create an Airflow DAG with Snowflake operators to backfill the events table up to the start of the current quarter.
```

A durable fact about the team's stack was replaced by a one-off task — and the
task wording ("create an Airflow DAG with Snowflake operators") is the
*assistant's* suggestion; the user only said the events table needed
backfilling.

### Step 13 — hybrid: an agent that writes memory itself

**T4, attempt 1**

```console
$ python tools/harness_probe.py --session demo-hybrid
Traceback (most recent call last):
  ...
urllib.error.URLError: <urlopen error [Errno 61] Connection refused>
```

**Causes.** `harness_probe.py` defaulted to `http://localhost:4010` (a port from
earlier experiments) and to model `claude-mock`, which exists only in
`config.mock.yaml`. Its `tool_use` block also called `Bash`, not a memory tool,
so it could not have demonstrated the hybrid path at all.

**Fix, round 1:** base URL and model taken from the environment; a new
`agent-writes` scenario with a `memoryhub` tool call and a `tool_result`
carrying a memory id.

**T4, attempt 2**

```console
$ python tools/harness_probe.py --scenario agent-writes --session demo-hybrid
assistant: I've saved the information about our on-call rotation starting every Monday at 09:00 UTC. ...
```

**T3**

```console
13:55:47  demo-hybrid    (header)  msgs 5  new 5 [user,assistant,tool_call,tool_result,assistant]  appended 3  thread 75f5ff80
          agent wrote 1 memory/memories itself
```

Detection worked, but the deferral is not visible: the scenario had a single
user turn, so extraction was not due anyway.

```console
$ python tools/harness_probe.py --scenario harness --session demo-harness
$ python live/peek.py thread demo-harness
------------------------------------------------------------------------------
thread c796bec2-6c15-41db-a763-bea672ad6bea   session demo-harness   cursor 0
------------------------------------------------------------------------------
>>   1 user      [litellm-proxy] I always run the test suite with pytest -q before pushing.
>>   2 assistant [litellm-proxy] It sounds like you're setting up a good practice for your developme…
>>   3 assistant [litellm-proxy] Noted.
>>   4 user      [litellm-proxy] We decided to keep the release branch frozen until Friday.
>>   5 assistant [litellm-proxy] Understood. Since the test suite (`pytest -q`) has completed succes…
```

**T3**

```console
13:57:00  demo-harness   (header)  msgs 6  new 5 [assistant,tool_call,tool_result,user,assistant]  appended 3  thread c796bec2
          history rewritten by the client; extraction scheduled -- runs in the background, line follows
13:57:00                  extraction finished: 2 memories from 2 window(s), cursor 5, 7729 ms
```

Two consecutive `assistant` messages and a `history_rewritten` flag. The proxy
was right: the probe replayed a hardcoded `"Noted."` as turn 1's assistant
message instead of what the model had actually replied, so the histories
diverged. That is a defect in the probe, not in capture — but it made the
harness scenario read like a bug.

**Fix, round 2:** the probe now builds its history from the model's real
replies, the way an actual harness does, and `agent-writes` makes two user turns
so extraction is genuinely due.

**T4, attempt 3**

```console
$ python tools/harness_probe.py --scenario agent-writes --session demo-hybrid-2
assistant: Indeed, the on-call rotation for our team begins every Monday at 09:00 UTC. ...
assistant: Got it! The handover notes for the on-call rotation will be documented in the team wiki. ...
assistant: No problem! If you need anything else ...
```

**T3**

```console
14:06:26  demo-hybrid-2  (header)  msgs 2   new 2 [user,assistant]  appended 2  thread 5e6fd6d9
14:06:33  demo-hybrid-2  (header)  msgs 6   new 4 [tool_call,tool_result,user,assistant]  appended 2  thread 5e6fd6d9
          agent wrote 1 memory/memories itself; extraction deferred (agent_wrote:1)
14:06:36  demo-hybrid-2  (header)  msgs 10  new 4 [tool_call,tool_result,user,assistant]  appended 2  thread 5e6fd6d9
          agent wrote 1 memory/memories itself; extraction deferred (agent_wrote:2)
```

**T4**

```console
$ python live/peek.py state
session  demo-hybrid-2   thread 5e6fd6d9   messages=6  cursor=0  waiting for extraction=6  extractions=0
```

This is the hybrid policy working: the proxy saw the agent's own memory writes
in the traffic, kept the full transcript, and skipped its own extraction pass
(`MEMORYHUB_CAPTURE_WHEN_AGENT_WRITES=defer`).

```console
$ python tools/harness_probe.py --scenario harness --session demo-harness-2
$ python live/peek.py thread demo-harness-2
------------------------------------------------------------------------------
thread 671e6c01-62a8-4888-bbf1-822a1ff1e493   session demo-harness-2   cursor 0
------------------------------------------------------------------------------
>>   1 user      [litellm-proxy] I always run the test suite with pytest -q before pushing.
>>   2 assistant [litellm-proxy] Got it! Before we proceed with running any tests, could you remind …
>>   3 user      [litellm-proxy] We decided to keep the release branch frozen until Friday.
>>   4 assistant [litellm-proxy] Understood. Given that the release branch is currently frozen until…
```

Clean thread, no `history_rewritten`. The `<system-reminder>` block, the
`thinking` block and the `tool_use`/`tool_result` pair were all stripped from
storage — while still being visible to the proxy, which is what made the hybrid
decision possible.

**T3**

```console
14:08:06  demo-harness-2 (header)  msgs 6  new 4 [tool_call,tool_result,user,assistant]  appended 2  thread 671e6c01
          extraction scheduled -- runs in the background, line follows
14:08:06                  extraction finished: 0 memories from 2 window(s), cursor 4, 7436 ms
```

**Zero memories** — from windows containing two perfectly extractable statements
("I always run `pytest -q` before pushing", "the release branch is frozen until
Friday"). The first harness run had extracted both:

```console
[extraction] v1 weight=0.8  The user always runs the test suite with `pytest -q` before pushing code.
  <- thread c796bec2 (demo-harness) messages [1, 2, 3]
[extraction] v1 weight=0.9  The release branch will be frozen until Friday.
  <- thread c796bec2 (demo-harness) messages [4, 5]
```

### Recall investigation

**T4, attempt 1**

```console
$ python verify.py reextract 671e6c01
Traceback (most recent call last):
  File ".../verify.py", line 153, in run_extract
    thread = await db.get(ConversationThread, uuid.UUID(thread_id))
ValueError: badly formed hexadecimal UUID string
```

`reextract` required a full UUID while `peek.py` prints an 8-character prefix.
**Fix:** `verify.py` now resolves a full id, an id prefix, or a session key.

**T4, attempt 2**

```console
$ python verify.py reextract demo-harness-2
embeddings: onnx
rewinding cursor 4 -> 0
   extraction (10.3s): {'extracted_count': 0, 'cursor': 4, 'failures': 0, 'windows_processed': 2,
                        'extraction_run_id': 'dream:qwen2.5:7b:1.0:2026-09-23T13:38:43.879604+00:00'}
```

Zero again. `make_http_llm_fn` pins `temperature: 0.0`, so this is not sampling
noise: on these exact windows the model returns nothing, deterministically. The
difference from the first harness run is the window boundaries (5 messages vs 4)
and what the assistant said — `"Noted."` in one case, a clarifying question in
the other.

### Final state of the database

```console
$ python live/peek.py state
------------------------------------------------------------------------------
threads 6   messages 36   current memories 7   decisions 12   failed windows 0
------------------------------------------------------------------------------

session  demo-live       thread 02ece523   messages=14  cursor=12  waiting=2  extractions=6
session  demo-live-2     thread 5eb10c3e   messages=4   cursor=4   waiting=0  extractions=2
session  demo-hybrid     thread 75f5ff80   messages=3   cursor=0   waiting=3  extractions=0
session  demo-harness    thread c796bec2   messages=5   cursor=5   waiting=0  extractions=2
session  demo-hybrid-2   thread 5e6fd6d9   messages=6   cursor=0   waiting=6  extractions=0
session  demo-harness-2  thread 671e6c01   messages=4   cursor=4   waiting=0  extractions=0
```

---

## 6. What the PoC demonstrated

| Claim | Evidence in this run |
|---|---|
| Capture needs no agent-side change | The agent has no MCP tool, no memory instructions, no hooks; only `--base-url` points at the gateway. Memories appeared in all live sessions. |
| No duplication on resend | Chat APIs resend full history; `new 2` per turn throughout. |
| No duplication across a proxy restart | Step 10: `new 2` after restart, thread reused by `a2a_context_id`. |
| Sessions are identified and isolated | `session_source=header`; two concurrent sessions, separate threads and cursors. |
| No added response latency | Extraction runs detached; 7–9 s of extraction happened after the reply was delivered. |
| Full provenance | Every memory carries `source=extraction`, thread id and source message numbers. |
| Hybrid with agent-side writes works | `agent_wrote:1`, `agent_wrote:2`; transcript kept, second LLM pass skipped. |
| Harness-shaped traffic is handled | `<system-reminder>`, `thinking` and tool blocks stripped from storage while still visible to the policy. |

Not demonstrated: cluster behaviour, memory injection into prompts, secret
redaction, and anything about scale.

---

## 7. Findings

### Fixed during this run (our code)

**F1 — `mock` embeddings in every previous PoC run were an artefact of test isolation.**
*Cause:* the ONNX model is cached under `$XDG_DATA_HOME/memoryhub/models`, and
the demo overrides `XDG_DATA_HOME` to isolate its database, so the model was
hidden and `startup.py` fell back to `MockEmbeddingService` without an error.
*Impact:* all earlier reconciliation results were meaningless — similarity was
never computed, so `update`/`skip` could never fire.
*Fix:* `env.sh` symlinks the real model cache into the isolated data directory
and reports the embedding mode; `check.sh` verifies it with
`is_model_downloaded()`.
*Also fixed:* `env.sh` used `BASH_SOURCE`, absent in zsh, and honoured a stale
`POC_DATA_DIR` from a previous shell.

**F2 — Extraction inside the proxy callback was cancelled after 20 s.**
*Cause:* LiteLLM wraps every logging callback in
`asyncio.wait_for(timeout=LOGGING_WORKER_MAX_TIME_PER_COROUTINE)`, 20 s by
default. Extraction is a second LLM call and routinely exceeds that.
*Fix:* extraction is scheduled as a detached task outside the callback; the
result is logged as a separate observation when it finishes. Added `drain()`
and a regression test that asserts the callback returns in under 150 ms while a
slow extraction is pending.

### To file as MemoryHub issues

**F3 — Memory writes and the extraction cursor are not committed atomically.**
*Evidence:* after the cancellation in F2 — `memories 5`, `decisions 5`,
`cursor=0`, and one memory with no `conversation_extractions` row.
*Cause:* `create_memory` commits each memory immediately
(`services/memory.py:77`); the cursor is committed only at the end of
`extract_from_thread` (`services/extraction.py:509`). Any cancellation, crash or
restart between those points leaves memories stored with the cursor unmoved, so
the next pass re-extracts the same messages as duplicates.
*Suggested fix:* one transaction per window covering the memories, the
provenance rows and the cursor advance.
*Status:* not fixed. Issue to file.

**F4 — The extraction prompt does not require groundedness.**
*Evidence:* a question about boiling an egg produced the memory
`"Boil an egg by bringing water to a rapid simmer…"` (weight 0.7) with
provenance `messages [9, 10]`. In the pre-fix run, four of five memories were
the model's own suggestions (SQLite, spaCy, TF-IDF). In the second session the
stored fact was the assistant's proposed DAG, not what the user asked for.
*Cause:* the prompt asks for "facts, preferences, decisions, **and knowledge**
worth remembering" and filters only by the *shape* of an utterance (no
greetings, no ephemera). Nothing requires a fact to have been stated, decided,
chosen or confirmed by a participant.
*Why it matters:* such entries are not evidence; a later session reads the
model's own invention as established context; generic statements crowd the
embedding space next to real facts and distort reconciliation; search returns
advice instead of decisions.
*Note:* this is **not** an argument for excluding assistant turns — they carry
confirmations, decisions and tool results. The distinction is groundedness, not
speaker.
*Suggested fix:* a groundedness rule in the prompt, plus a minimum-weight floor
(the model itself scored decisions 0.8–0.9 and the recipe 0.7).
*Also:* the personal edition keeps its **own copy** of the prompt in code
(`EXTRACTION_SYSTEM_PROMPT` in `services/extraction.py`), separate from
`prompts/conversation_extraction.yaml`. Editing one does not change the other.
*Status:* worked around for the demo by shortening the agent's system prompt
(with `--chatty` to reproduce). Issue to file.

**F5 — The `update` threshold merges unrelated facts, and no threshold value fixes it.**
*Evidence:* three `update` decisions in this run.

| score | candidate → what it replaced | verdict |
|---|---|---|
| 0.8855 | "Create an Airflow DAG … backfill the events table" → "The data platform team uses Airflow for scheduling and Snowflake as the warehouse" | wrong |
| 0.8819 | "The API must remain backward compatible with v2 clients" → "API documentation must be finalized by Friday" | wrong |
| 0.8668 | "Migrate the backend from Go to Rust" → "The backend will be migrated to Go" | correct |

*The only correct merge has the lowest score.* Raising the threshold to 0.88
would break the Go→Rust correction and still allow the Airflow merge. No
threshold separates these three cases.
*Consequence:* `update` writes a new version and sets `is_current = 0` on the
previous row, so a wrongly merged fact silently leaves current memory (the
Friday deadline, the team's stack).
*Cause:* the decision "refinement of the same fact vs a different fact" is not
derivable from cosine similarity. The cluster edition has a tiebreaker band
(0.80–0.98) designed for exactly this, but it is **not wired**, so the cluster
fails in the opposite direction (always create → duplicates).
*Supporting observation:* all search scores sit in a 0.71–0.83 band with this
embedding model — everything is "somewhat similar", which makes fixed thresholds
of 0.85/0.98 fragile in principle.
*Good news:* `reconciliation_decisions` already records `action`,
`similarity_score`, `nearest_match_id` and `reason` for every candidate, so
thresholds and a tiebreaker can be calibrated from data.
*Suggested fix:* wire the tiebreaker (an LLM judge or a contradiction check) in
the ambiguous band, in both editions.
*Status:* not fixed. Issue to file — highest priority of the four.

**F6 — Extraction recall depends on window boundaries and on how verbose the assistant was; a miss is indistinguishable from "nothing to extract".**
*Evidence:* the same two user facts produced 2 memories in one harness run and 0
in another. The only differences were the window split (5 messages vs 4) and the
assistant's reply. `temperature` is pinned to `0.0` in `make_http_llm_fn`, and
`reextract` over the same windows returned 0 again — so this is input
sensitivity, not sampling noise.
*Why it matters:* `extracted_count=0, failures=0` means either "nothing worth
keeping" or "the model missed it", and the cursor advances either way. Combined
with the existing behaviour of advancing past *failed* windows, this is silent
data loss with no signal.
*Suggested fix:* distinguish "model returned an empty list" from "window not
processed"; consider a cheap second pass or a recall check on windows that
contain user statements but yield nothing.
*Status:* not fixed. Issue to file.

**F7 — Search has no relevance cut-off.**
*Evidence:* with 5 memories stored, both `"backend language"` and
`"data warehouse"` returned all 5, including the egg recipe (0.7287 against
0.8334 for the relevant one).
*Cause:* `search_memories` returns the top `max_results` (default 10) by score
with no minimum. `is_current` filtering works correctly — retired versions never
appear.
*Why it matters:* at scale, a caller that injects search results into a prompt
will inject a dozen weakly related facts unless it filters by score itself.
*Suggested fix:* a configurable score floor, and/or return the score in a way
callers are expected to use.
*Status:* not fixed. Issue to file. `verify.py search` now prints rank and score
so the effect is visible.

### Demo tooling defects fixed along the way

| Defect | Cause | Fix |
|---|---|---|
| Capture log lines invisible in the proxy terminal | LiteLLM leaves the root logger at `WARNING` | dedicated handler on the `memoryhub_capture` logger, level via `MEMORYHUB_CAPTURE_LOG_LEVEL` |
| `agent.py` failed with `ModuleNotFoundError: openai` | repo-root venv was active; `env.sh` only activated its own when none was | `env.sh` switches to the demo venv and prints which `python` is used |
| `pytest` failure `assert 2 == 0` | the test built a logger "with `EXTRACT_EVERY` unset" but inherited it from the sourced demo environment | fixture deletes all `MEMORYHUB_CAPTURE_*` variables first |
| `peek.py memories` showed retired versions as live | query did not filter `is_current` | prints current memories, marks what each replaced, `--all` for the rest |
| `harness_probe.py` — connection refused | default port 4010, model `claude-mock` from a different config | base URL and model from the environment |
| `harness_probe.py` did not exercise the hybrid path | its `tool_use` called `Bash`, not a memory tool | new `agent-writes` scenario with two user turns |
| `harness_probe.py` produced a false `history_rewritten` | replayed a hardcoded assistant reply instead of the real one | history is now built from the model's actual replies |
| `verify.py reextract` rejected an id prefix | required a full UUID while `peek.py` prints 8 chars | resolves full id, prefix, or session key |
| `verify.py search` gave no way to judge relevance | score not printed | prints rank and `relevance_score` with a note about the missing cut-off |

---

## 8. Recommended next steps

1. **File F5** (reconciliation tiebreaker) first. Memory versioning currently
   works against the user: a correct correction and a wrong merge are
   indistinguishable by score, and the wrong merge silently removes a fact.
2. **File F3** (atomic memory + cursor commit) — any interruption corrupts the
   thread's extraction state.
3. **File F4** (groundedness rule + weight floor, in both copies of the prompt)
   and **F6** (distinguish an empty extraction from an unprocessed window).
4. **File F7** (search relevance floor).
5. Re-run this walkthrough against the **cluster edition** when access returns.
   Its extraction differs (window size 4, unwired tiebreaker, circuit breaker,
   S3 offload for long messages), so none of these numbers transfer.
6. Add a **secret-redaction filter** before thread writes — the gateway stores
   everything that passes through it.
7. Decide on **gateway mode** for MemoryHub: remove the thread-writing
   instruction from the harness prompt and tool profile, keeping read/fork/A2A,
   so threads arrive only through the gateway and are not written twice.

---

## Appendix — command reference

```bash
source live/env.sh              # in every terminal, from the demo directory
./live/check.sh                 # preflight
./live/proxy.sh ollama          # T1 (also: real, or no argument for mock)
python agent.py --interactive --session demo-live   # T2 (+ --chatty)
python live/watch.py            # T3
python live/peek.py state       # T4
python live/peek.py thread [SESSION]
python live/peek.py next [SESSION]
python live/peek.py decisions
python live/peek.py memories [--all]
python verify.py search "..."
python verify.py reextract SESSION|PREFIX
python tools/harness_probe.py --scenario agent-writes|harness --session NAME
```
