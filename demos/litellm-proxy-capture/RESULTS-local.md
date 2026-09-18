# PoC run — personal edition — 2026-09-18

Command: `./run-poc-local.sh` (offline mode), full transcript in `out/poc-report.txt`.
Environment: Linux sandbox, no outbound network to model hosts, so two stand-ins
were used: MemoryHub's own **mock embeddings** (the ONNX model could not be
downloaded) and `tools/fake_extractor.py` instead of an extraction LLM.
Everything else — proxy, capture, threads, windowing, reconciliation,
provenance, cursor — is the real code path.

| # | Criterion | Verdict | Evidence |
|---|---|---|---|
| 1 | Capture without the agent knowing about MemoryHub | PASS | `agent.py` has no MCP tool, no memory prompt, no hooks. Thread with 8 messages and 3 memories, all `source=extraction` with thread + message provenance; "dark mode" memory present. |
| 2 | No duplicate messages, including across a proxy restart | PASS | Proxy restarted mid-session; the sink re-found the thread by `a2a_context_id` and seeded the delta. No repeated message block in any of the 4 threads; 2 calls correctly flagged `history_rewritten`. |
| 3 | Session identification | PASS | Three paths exercised: `X-MemoryHub-Session` header, fingerprint of the first user message, and the OpenAI `user` field. One thread per session key. |
| 4 | Ownership and attribution | PASS | Thread owner is the local OS identity the sink writes as; the observed speaker (`alice@example.com`, source `end_user`) is metadata only. This is the open question for the proxy approach, now visible in data. |
| 5 | Extraction quality per scenario | PARTIAL | `smalltalk` → 0 memories (expected). `correction` → two separate facts (us-east-1 and eu-west-1) rather than an update. With mock embeddings the create/update/skip decision is not meaningful, so this must be re-measured with the real model. |
| 6 | Cost and latency | PASS | 10 agent calls, 4 extraction calls triggered by the proxy (`EXTRACT_EVERY=2`), average extraction call 114 ms, per-call latency and token counts recorded per observation. |
| 7 | Harness-shaped traffic | PARTIAL | Anthropic `/v1/messages` with `tool_use` / `tool_result` blocks and an injected `<system-reminder>`: 5 messages stored, 0 reminder blocks, 0 tool messages (tools off by default), model reasoning never stored. It is a synthetic probe, not a real Claude Code session. |

## Mode comparison (`./run-poc-compare.sh`, offline, 2026-09-18)

Same three scenarios, five modes, each on its own database.

| Mode | What it is | Memories | Recall | False pos. | Exact dupes | Correction | Agent calls | Agent tokens | Extractions |
|---|---|---|---|---|---|---|---|---|---|
| A | agent writes memories itself (MCP-style) | 4 | 4/4 | 0 | 0 | two facts | 14 | 140 | – |
| B | proxy captures, extracts during the session | 4 | 4/4 | 0 | 0 | two facts | 7 | 70 | 3 |
| C | proxy captures, extraction runs later | 4 | 4/4 | 0 | 0 | two facts | 7 | 70 | – |
| D | B + a second extraction pass | 4 | 4/4 | 0 | 0 | two facts | 7 | 70 | 3 |
| E | explicit agent and proxy on one database | 4 | 4/4 | 0 | 0 | two facts | 21 | 210 | 6 |

What the numbers say:

- **The explicit path costs twice the agent calls** (14 vs 7 for the same three
  turns): every turn needs a second "should I remember this?" call, plus the
  memory instruction in every prompt. The proxy moves that cost to a separate
  extraction call the agent never waits for.
- **Recall is the same in this setup**, so the argument for the proxy is not
  "more memories" — it is "no harness integration". With the offline stand-in
  extractor both paths see the same sentences; a real model may separate them.
- **Re-extraction is safe** (D = B): the cursor plus the ≥0.98 skip rule absorb a
  second pass over the same messages.
- **Mode A produces no threads and no provenance.** Memories written by the agent
  have nothing to audit against; every proxy-written memory links to a thread and
  message numbers.
- **Cross-source dedup is one-directional.** In E the agent wrote first and
  extraction skipped the duplicates (`skip, similarity=1.0`). Reversed, it breaks:
  the personal edition's `write` path has no similarity gate, so an agent write
  *after* extraction produces a second copy. Verified separately by writing the
  same sentence through both paths — two memories, no gate. The cluster edition
  does gate writes, which is one more place where local results do not transfer.

## What this run does not establish

- **Search and dedup quality.** Mock embeddings make ranking meaningless; the
  `correction` result in particular needs the real embedding model.
- **Extraction judgement.** The rule-based stand-in decides what is a fact.
- **Cluster behaviour.** Windows of 4, create-only reconciliation, circuit
  breaker, S3 offload of messages over 8 KB, tenants and RBAC only exist there.

## To repeat with real models

```bash
# .env: POC_MODEL / POC_MODEL_API_KEY for the agent,
#       MEMORYHUB_CAPTURE_EXTRACT_MODEL(_URL) for extraction (Ollama works)
memoryhub doctor          # the embedding model must be onnx, not mock
./run-poc-local.sh real   # the checklist
./run-poc-compare.sh real # modes A-E -> out/compare.md
./demo.sh real            # the 10-minute demo (DEMO.md)
```

`run-poc-local.sh real` and `demo.sh real` force `MEMORYHUB_CAPTURE_SINK=local`
and their own `XDG_DATA_HOME` after sourcing `.env`, so a `.env` left over from
the cluster experiments cannot redirect them.
