# PoC run — personal edition — 2026-09-18

The cluster path is out of scope. Everything below is the MemoryHub personal
edition (SQLite under `out/`). Offline stand-ins (mock embeddings +
`tools/fake_extractor.py`) exercise the real capture, thread, windowing,
reconciliation, provenance and cursor path. A `real` run additionally needs
the ONNX embedding model (`memoryhub doctor`) and a live extraction LLM.

## Checklist (`./run-poc-local.sh`, offline)

| # | Criterion | Verdict | Evidence |
|---|---|---|---|
| 1 | Capture without the agent knowing about MemoryHub | PASS | `agent.py` has no MCP tool, no memory prompt, no hooks. Thread with 8 messages and 3 memories, `source=extraction`, thread + message provenance; "dark mode" memory present. |
| 2 | No duplicate messages, including across a proxy restart | PASS | Proxy restarted mid-session; sink re-found the thread by `a2a_context_id` and seeded the delta. No repeated message block in any of 4 threads. |
| 3 | Session identification | PASS | Header → `poc-prefs`. No header → fingerprint `default_user_id:…`. `--user alice@example.com` → `end_user` actor source. One thread per session key. |
| 4 | Ownership and attribution | PASS | Thread owner is the local OS identity the sink writes as; the observed speaker is metadata only. |
| 5 | Extraction quality per scenario | PARTIAL | `smalltalk` → 0 memories. `correction` → two facts (us-east-1 and eu-west-1), not an update. Mock embeddings make create/update/skip not semantically meaningful. |
| 6 | Cost and latency | PASS | Auto-extract from traffic works (`EXTRACT_EVERY=2`): 3 in-proxy extractions during capture, 4 total, avg 32 ms. 10 agent calls, 80 prompt tokens observed. |
| 7 | Harness-shaped traffic | PARTIAL | Anthropic `/v1/messages` with `tool_use` / `tool_result` and `<system-reminder>`: 5 messages stored, 0 reminder blocks, 0 tool messages (tools off). Synthetic probe, not a live Claude Code session. With `ANTHROPIC_API_KEY` in `.env`, point Claude Code at the proxy as in `DEMO.md`. |

`EXTRACT_EVERY` previously froze at import and was overwritten to `0` by a cluster `.env`. The callback now re-reads it per call; local scripts force `EXTRACT_EVERY=2` after sourcing `.env`.

## Mode comparison (`./run-poc-compare.sh`, offline)

Same three scenarios, five modes, isolated databases. Table from `out/compare.md`:

| Mode | What it is | Memories | Recall | False pos. | Exact dupes | Correction | Agent calls | Agent tokens | Extractions | Extract ms |
|---|---|---|---|---|---|---|---|---|---|---|
| A | agent writes memories itself (MCP-style) | 4 | 4/4 | 0 | 0 | two facts | 14 | 140 | 0 | - |
| B | proxy captures, extracts during the session | 4 | 4/4 | 0 | 0 | two facts | 7 | 70 | 3 | 29 |
| C | proxy captures, extraction runs later | 4 | 4/4 | 0 | 0 | two facts | 7 | 70 | 0 | - |
| D | proxy + a second extraction pass | 4 | 4/4 | 0 | 0 | two facts | 7 | 70 | 3 | 36 |
| E | both sources writing into one database | 4 | 4/4 | 0 | 0 | two facts | 21 | 210 | 6 | 22 |

What the numbers say:

- **The explicit path costs twice the agent calls** (14 vs 7 for the same three
  turns): every turn needs a second "should I remember this?" call, plus the
  memory instruction in every prompt. The proxy moves that cost to a separate
  extraction call the agent never waits for.
- **Recall is the same in this setup**, so the argument for the proxy is not
  "more memories" — it is "no harness integration". The offline extractor sees
  the same sentences on both paths; a real model may separate them.
- **Re-extraction is safe** (D = B): the cursor plus the ≥0.98 skip rule absorb a
  second pass over the same messages.
- **Mode A produces no threads and no provenance.** Memories written by the agent
  have nothing to audit against; every proxy-written memory links to a thread and
  message numbers.
- **Cross-source dedup is one-directional.** In E the agent wrote first and
  extraction skipped the duplicates (4 memories, not 8). Reversed, it breaks:
  the personal edition's `write` path has no similarity gate. The cluster edition
  does gate writes, which is one more place where local results do not transfer.

## Live demo (`PAUSE=0 ./demo.sh`, offline)

Act 1: `agent_explicit.py` — 3 turns, 6 LLM calls, 2 memories, instruction in
the prompt. Act 2: `agent.py` grep for MemoryHub/MCP/write_memory = 0; only
`--base-url` changes. Act 3: `verify.py search "dark mode"` finds the preference
with `source=extraction` and thread provenance **without a manual extract**
(proxy `EXTRACT_EVERY=2`). Act 4: session header, identity, correction-as-two-
facts, one-way dedup.

## What this run does not establish

- **Search and dedup quality.** Mock embeddings make ranking meaningless.
- **Extraction judgement.** The rule-based stand-in decides what is a fact.
- **Cluster behaviour.** Windows of 4, create-only reconciliation, circuit
  breaker, S3 offload of messages over 8 KB, tenants and RBAC only exist there.
- **A live Claude Code session** was not driven in this run; the capture path
  for Anthropic-shaped traffic is covered by `tools/harness_probe.py`. `DEMO.md`
  has the commands when `ANTHROPIC_API_KEY` is present.

## To repeat with real models

```bash
# .env: POC_MODEL / POC_MODEL_API_KEY for the agent,
#       MEMORYHUB_CAPTURE_EXTRACT_MODEL(_URL) for extraction (Ollama works).
# Local scripts force SINK=local and EXTRACT_EVERY=2 after sourcing .env.
# If EXTRACT_API_KEY is empty they reuse POC_MODEL_API_KEY.
memoryhub doctor          # the embedding model must be onnx, not mock
./demo.sh real            # the 10-minute demo (DEMO.md)
./run-poc-local.sh real   # the checklist
./run-poc-compare.sh real # modes A-E -> out/compare.md
```
