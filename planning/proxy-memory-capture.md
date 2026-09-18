# Proxy-Based Implicit Memory Capture (WRIG-1482)

Status: PoC in progress
Date: 2026-09-16
Branch: `feat/wrig-1482-litellm-proxy-capture`
PoC: [`demos/litellm-proxy-capture/`](../demos/litellm-proxy-capture/)
Related: [turn-level-hooks.md](turn-level-hooks.md), [memory-extraction-pipeline.md](memory-extraction-pipeline.md),
[retrieval-unit-routing.md](retrieval-unit-routing.md)

## Problem

Every new harness needs its own MemoryHub integration (MCP config, rule files,
hooks), and even when configured, automatic reads/writes are inconsistent —
agents often have to be told explicitly to use MemoryHub. Turn-level hooks
reduce the dependence on agent initiative but are still harness-specific.

## Hypothesis

An LLM gateway sits at the one boundary every harness shares — agent ↔ model.
If the gateway turns LLM traffic into MemoryHub conversation threads, the
existing extraction pipeline can create memories with **no change to the
agent other than its base URL**.

Counter-hypothesis: raw LLM traffic lacks the semantics (identity, session,
turn boundaries, subagent structure) needed for trustworthy governed memory,
so hooks remain necessary where available. Framing:
*a proxy is framework-independent, but not context-independent.*

## Design

1. LiteLLM `CustomLogger.async_log_success_event` receives the
   `standard_logging_object` (messages, response, headers, key metadata)
   after each successful call, including assembled streaming responses and the
   Anthropic `/v1/messages` route.
2. Messages are normalized (OpenAI/Anthropic, tool blocks split out, thinking
   dropped, system prompt excluded from tracking).
3. Session key: `X-MemoryHub-Session` header → LiteLLM `litellm_session_id`
   → `actor:hash(first user message)` → `hash(first user message)`.
   The source is recorded with every observation.
4. Delta: chat APIs resend the full history, so the longest common prefix with
   the previously captured transcript is skipped; a shorter prefix is flagged
   as a history rewrite (compaction/edit).
5. Sink: `thread(create)` once per session (`a2a_context_id` = session key,
   `metadata.source = litellm-proxy`), `thread(append)` for new messages,
   `thread(extract)` every N user turns. Dedup, contradiction handling and
   provenance come from the existing `write_memory` path — the proxy does not
   reimplement extraction.
6. Every call yields an `Observation` (what the proxy could see, what it
   appended, errors). This log is the research output.

The capture adapter is intentionally thin so it can later emit a canonical
lifecycle event (`TURN_COMPLETED`, …) shared with hook adapters (hybrid
architecture: hooks where available, proxy as fallback, dreaming as backfill).

## Evaluation plan

| Phase | Goal | Exit criterion |
|---|---|---|
| 1 | Traffic → local log (mock model) | Correct deltas and session grouping for OpenAI, streaming, Anthropic |
| 2 | Traffic → MemoryHub threads | Threads visible via `thread list`, no duplicate messages |
| 3 | Threads → existing extraction | `preferences` scenario yields preference memories; agent never called MemoryHub |
| 4 | Compare explicit / proxy / dreaming / combined | Recall, false positives, duplicates, tokens, latency, attribution |
| 5 | Real harness (Claude Code) | Session grouping and side-call filtering hold on real traffic |

## Early findings (Phase 1, mock model, 2026-09-16)

* OpenAI sync + streaming and Anthropic `/v1/messages` all reach the callback
  with assembled responses; deltas were correct (no duplicate appends) and a
  changed system prompt between turns did not break session tracking.
* Without a session header, grouping relied entirely on fingerprinting.
* With the proxy master key, the only identity LiteLLM reports is
  `default_user_id`: **identity attribution requires per-user virtual keys, the
  OpenAI `user` field, or a client header** — i.e. some client-side config
  after all, although much less than MCP + rules + hooks.
* Tool traffic is visible only as serialized blocks; subagent relationships are
  not visible at all.

## Findings from the first cluster run (2026-09-16/17)

* **Extraction is a cluster-side dependency.** It runs synchronously inside the
  MCP pod with the pod's model, URL and key; the pod initially had none. The
  proxy removes client-side config for *capture*, but extraction quality and
  cost are governed by one central config.
* **There is no background dreaming on the cluster.** Threads are only processed
  when someone calls `thread(action="extract")`; the proxy does it via
  `EXTRACT_EVERY`. A production design needs a worker on
  `memoryhub_core.services.dreaming.extract_from_thread` (the unbuilt Dreamer).
* **Failed windows are silently consumed.** The cursor advances past them, so the
  demo thread returned `extracted 0, failures 0` on the second call although no
  memory was ever created. The proxy now warns on `failures > 0`; recovery is a
  `turn_range` re-run.
* **MCP pod restarts break the proxy's session** (`McpError: Session terminated`);
  three turns were lost before reconnect-and-retry was added.
* **Fingerprint sessions collide** across repeated runs of the same scenario.
* **Pipeline limits that shape proxy capture:** 4-message windows, messages over
  8 KB moved to S3 and hidden from extraction (the proxy now truncates to 8 KB),
  no update of existing memories, circuit breaker after 5 consecutive creates,
  and 90-day thread TTL with cascade delete of extracted memories.

## Personal-edition findings (2026-09-18)

The cluster is unavailable, so the PoC now runs end to end on the personal
edition: `demos/litellm-proxy-capture/run-poc-local.sh` (checklist),
`run-poc-compare.sh` (modes A-E), `demo.sh` + `DEMO.md` (the 10-minute demo).
Results in `demos/litellm-proxy-capture/RESULTS-local.md`.

* **Capture without harness integration works.** An agent with no MCP tool, no
  memory instruction and no hooks produces governed memories with thread-level
  provenance; the only client-side change is the model base URL.
* **The cost moves rather than disappearing.** The explicit path spends a second
  model call per turn (14 vs 7 calls for three turns) plus the instruction in
  every prompt; the proxy path spends a separate extraction call.
* **Config does not go to zero.** Session boundaries need `X-MemoryHub-Session`
  unless fingerprinting the first user message is acceptable, and identity needs
  a header, an end-user field or a per-user gateway key.
* **Identity remains the open architectural question.** Memory is owned by the
  gateway's identity; the observed speaker is metadata only.
* **Corrections are stored as new facts**, not as corrections, in both editions
  (for different reasons: no tiebreaker on the cluster, threshold on local).
* **Cross-source dedup is one-directional in the personal edition.** Extraction
  skips what the agent already wrote, but the local `write` path has no
  similarity gate, so an agent write after extraction duplicates it.
* **Re-extraction is safe**: cursor plus the >= 0.98 skip rule absorb a second
  pass over the same messages.

## Open questions

* Should the proxy write threads as itself (service identity) or impersonate the
  observed user? Needs an authz decision (driver/actor model).
* Where does the delta state live in a multi-replica gateway (Redis? the thread's
  own message digests?)
* Does per-turn extraction duplicate cost already paid by dreaming? (Phase 4)
* Do proxy-created memories actually improve retrieval, given facts currently
  rank below transcripts (see benchmarks/RESULTS.md, #447)?
* Pre-call injection (`async_pre_call_hook`) could also make *reads* implicit —
  out of scope for this PoC, but the natural next experiment.
