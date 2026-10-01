# WRIG-1482 — live demo v2: step-by-step scenario

Updated 29 Sep 2026, after two meetings (25 Sep "Agentic memory PoV", 28 Sep
"OpenClaw memory platform") and research on the proxy as a memory layer.

This is not a repeat of the previous demo. The previous one answered **"does
capture through the gateway work"** — the answer was obtained and recorded on
video. This one answers the questions people asked out loud in the meetings,
and that nobody has an answer to yet.

---

## Five use cases and where they came from

| # | Use case | Whose request | What we prove |
|---|---|---|---|
| **UC1** | Provider interchangeability: one conversation → MemoryHub **and** Hindsight at the same time | Ray: "a shared architecture for the actual execution of the tests"; Ryan: "radio buttons" for the memory provider | The abstraction does not leak: a second provider plugs in as an adapter, the core is untouched |
| **UC2** | Split of extraction / retrieval + **fact preservation** | Ray: "it doesn't break down the extraction piece versus the retrieval piece" | End-to-end recall does not see how a chain of false merges erases a correct fact |
| **UC3** | Zero-touch tier: real Codex, change only `base_url` | Sanjeev: two integration tiers (plugin / MCP) — the third, cheapest one is missing from his model | The "change nothing in the agent" tier exists — **or** it runs into LiteLLM hooks, and we need to know that before October |
| **UC4** | Identity and the personal → team transition | Sanjeev: agent identity is "phase two, completely open"; Ryan: OpenClaw Enterprise targets teams | The gateway is the natural enforcement point for identity; and where isolation currently leaks |
| **UC5** | Secret redaction | Your observation: the proxy writes everything | Acceptable for "personal AI", a blocker for "an enterprise resource with audit". Now measured as a number |

Run order is not numbering order: shadow (UC1) is turned on before the proxy
starts, redaction (UC5) is visible from the first utterance. Below, everything
is in the order you need to press the keys.

---

## What appeared in the code since last time

| File | What it does |
|---|---|
| `redact.py` (new) | Strips credentials **before** anything is stored or hashed. 9 rules + operator patterns via env |
| `tests/test_redact.py` (new) | 23 tests: every rule fires, prose is left alone, redaction is idempotent (otherwise delta tracking drifts) |
| `sinks.py` → `HindsightSink` | Adapter for a second memory system under the same `Sink` contract |
| `sinks.py` → `ShadowSink` | Fan-out of one conversation into two stores; the shadow cannot take down the primary path |
| `live/facts.py` (new) | Metric: extraction / preservation / retrieval / precision / leakage — separately |
| `live/facts.json` (new) | Ground truth: exactly what should end up in memory after the conversation script |
| `capture_core.py`, `memoryhub_capture.py` | Redaction in the pipeline, a `redactions` counter in Observation and in the log |
| `live/env.sh`, `live/check.sh` | Redaction and shadow variables; preflight for Hindsight and codex |

All of this already sits on branch `feat/wrig-1482-litellm-proxy-capture`, uncommitted.
`python -m pytest tests/ -q` — 36 passed.

---

# Part 0. Preparation (before recording)

## Step 0.1 — bring up Hindsight

**Terminal 0** (you can close it later; it does not need to be in frame):

```bash
docker ps -a --filter name=hindsight
# if the container already exists:
docker start hindsight
# if not — quickstart (data lands in ~/.hindsight-docker, which you already have):
docker run -d --pull always --name hindsight --restart unless-stopped --shm-size=1g \
  -p 8888:8888 -p 9999:9999 \
  -e HINDSIGHT_API_LLM_PROVIDER=ollama \
  -e HINDSIGHT_API_LLM_BASE_URL=http://host.docker.internal:11434/v1 \
  -e HINDSIGHT_API_LLM_MODEL=qwen2.5:7b \
  -v $HOME/.hindsight-docker:/home/hindsight/.pg0 \
  ghcr.io/vectorize-io/hindsight:latest

curl -s localhost:8888/health/live; echo
curl -s localhost:8888/version; echo
```

**What we check:** Hindsight answers and can reach your local Ollama.

**Two caveats, stated honestly:**

* `host.docker.internal` for reaching Ollama from the host is my guess; it is
  not in the Hindsight docs. If LLM calls fail, try
  `http://172.17.0.1:11434/v1` or `--network host`.
* Hindsight requires a model with **structured output**. If `qwen2.5:7b` cannot
  handle it, `retain` will error. Fallback is `gemma3:12b`, listed in their
  `.env.example` as an example.
* Hindsight embeddings are **local by default** (`bge-small-en-v1.5`); nothing
  extra to configure.

If Hindsight does not come up in 10 minutes — **do not get stuck**. Send me the
output, and we run without shadow: `export MEMORYHUB_CAPTURE_SHADOW_SINK=` and
everything else works as before.

## Step 0.2 — clean start and preflight

**Terminal 1:**

```bash
cd ~/MemHub/memory-hub/demos/litellm-proxy-capture
rm -rf out/live-data out/live-observations.jsonl
export MEMORYHUB_CAPTURE_SHADOW_SINK=hindsight
source live/env.sh
./live/check.sh
```

**What to look for in the `env.sh` banner:**

```
  database      .../out/live-data/memoryhub/memoryhub.db
  embeddings    onnx                    <- NOT "MOCK", otherwise update/skip are meaningless
  redaction     on
  shadow sink   hindsight
```

**What to look for in `check.sh`:** it should be `0 to fix`, including the new
lines `hindsight answers at ...` and `codex present: ...`.

**The point:** zero assumptions. The last run was ruined by exactly two things
that should already be gone here — mock embeddings and a stale database.

---

# Part 1. Start and the base conversation

Five terminals, as before. Layout: T1 top left (proxy), T2 bottom left
(agent), T3 top right (observations), T4 bottom right (internals), T5 opens
later.

**In EVERY terminal, first thing:**

```bash
cd ~/MemHub/memory-hub/demos/litellm-proxy-capture
export MEMORYHUB_CAPTURE_SHADOW_SINK=hindsight
source live/env.sh
```

## Step 1.1 — start the gateway

**T1:**

```bash
./live/proxy.sh ollama
```

**What to look for in the output** (one line, the whole demo exists for it):

```
memoryhub_capture: ready (sink=local extract_every=2 tools=False
                          max_bytes=8000 when_agent_writes=defer redact=on)
memoryhub_capture: shadow mode -- primary=local shadow=hindsight
```

**The point:** this is the only place in the entire demo where memory is
mentioned at all. `redact=on` and `shadow mode` are the two new words compared
with the previous video.

## Step 1.2 — watchers

**T3:**

```bash
python live/watch.py
```

**T4:**

```bash
python live/peek.py state
```

Expected: `no database yet -- MemoryHub is empty.`

**The point:** both memory systems are empty. Everything that appears later
arrived from traffic, not from configuration.

## Step 1.3 — the agent

**T2:**

```bash
python agent.py --interactive --session demo-live
```

**The point:** no MCP tool, no memory instructions, no hooks. The only change
on the client side is `base_url`.

---

# Part 2. UC5 — secret redaction

This comes first, because the secret will end up in history anyway and will be
resent on every call.

## Step 2.1 — an utterance with a secret

**T2**, type:

```
The 'Nexus' upgrade moves our entire backend to Go, and the API documentation is due Friday.
```

then:

```
Here is the staging deploy line I keep forgetting: AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE and the registry token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345
```

> Both values are **fake** (`AKIAIOSFODNN7EXAMPLE` is the official example from
> the AWS docs). They are fine to show on camera. Do not open a real `.env`.

**What to watch in T1:**

```
memoryhub_capture: session=demo-live(header) new=2 appended=2 ...
   REDACTED=2['aws_access_key_id', 'github_token']
```

**What to watch in T4:**

```bash
python live/peek.py thread
```

The message text should contain `[redacted:aws_access_key_id]`, and the key
itself should be absent.

**The point / what is being checked:**

* The secret is stripped **before** storage and **before** hashing. This
  matters more than it looks: the session key is a hash of the first user
  utterance, and credentials must not live even in the hash function's input.
* The shape of the fact is preserved: "staging deploys with an AWS key and a
  registry token" — that is worth remembering. The value is not.
* Redaction is **idempotent**. The client resends the whole history on every
  call; if redaction produced a different result, the delta tracker would treat
  old messages as new. There is a dedicated test for this.

## Step 2.2 — show what would have happened without it

**T5** (new terminal):

```bash
cd ~/MemHub/memory-hub/demos/litellm-proxy-capture
source live/env.sh
grep -c "AKIAIOSFODNN7EXAMPLE" out/live-data/memoryhub/memoryhub.db || echo "not in the database"
python verify.py report | grep -i redact
```

**The point:** a number, not a promise. `credentials redacted: N {rule: count}`
is what you can bring to Ryan and Sally as the answer to "an enterprise
resource with audit".

**Honest framing, say it out loud:** this is a filter on the **shape** of
credentials, not a compliance control. It catches prefixes, lengths, charsets,
and assignments to secrets. It will not catch a secret that looks like an
English sentence. It reduces risk; it does not eliminate it — and that is how
it has to be presented.

---

# Part 3. UC2 — extraction, retrieval, and fact preservation

## Step 3.1 — a conversation that tests extraction

**T2**, one utterance at a time, waiting for the reply:

```
The API must stay backwards compatible with v2 clients until the end of the year.
```
```
We're moving CI from Jenkins to Tekton.
```
```
After a meeting we decided to migrate the backend to Rust, not Go.
```
```
Every new service must ship with OpenTelemetry tracing from day one.
```

**What to watch in T3 after every even utterance:**

```
extraction on <thread> -- N memories, 2 window(s), cursor K, ~8000 ms
  shadow hindsight -- M new, T total, ~12000 ms
```

**The point:** both systems think about **the same text at the same moment**.
That is the control of variables Sanjeev asked for: the difference cannot be
explained by the agent, the model, or the task.

## Step 3.2 — the extraction window and the decisions

**T4:**

```bash
python live/peek.py next        # exactly what will go to the extraction model
python live/peek.py decisions   # every candidate: action + similarity
python live/peek.py memories    # current memories with provenance
```

**What to look for in `decisions`:**

* `update` rows with a score between 0.85 and 0.98 — places where **two
  different facts** merged into one version chain;
* the Rust line: if it came out as `create` with "no similar memory found" —
  the correction target was already destroyed by an earlier false merge.
  That is exactly what happened in the recorded run (0.9171, then 0.8622).

**The point:** `update` is not an edit. It is a new version plus `is_current=0`
on the previous row. Three unrelated facts can collapse into one chain, and
**no extraction reports an error while that happens**.

## Step 3.3 — the metric this is all for

**T4:**

```bash
python live/facts.py check
```

**What to read in the output:**

```
extraction    5/6 expected facts reached the store
preservation  3/6 are still the current version   (2 extracted then retired by a merge)
leakage       none of the watched credentials reached the store
```

and, line by line, which fact was lost and **because of which merge**:

```
[ FAIL ] docs-friday   expected
          lost: replaced at similarity 0.9171 by API must stay backwards compatible...
```

**The point / what is proved:**

This is a direct answer to Ray's remark. End-to-end recall sees only the last
line — "the fact is gone". It does not distinguish **"the system decided not
to store it"** from **"the system stored it and then erased it itself"**. The
difference is fundamental: the first is a question of extraction-prompt
quality, the second is a reconciliation bug, which in an enterprise frame
means silent data loss.

`preservation` is a metric that exists in none of the current memory
benchmarks. It is worth bringing into the shared harness as our contribution.

## Step 3.4 — retrieval, separate from extraction

**T4:**

```bash
python live/facts.py check --search      # ~10 seconds, loads the embedding model
```

**What to watch:**

```
retrieval     4/5 come back in the top 5 for their own query
precision     2 ungrounded result(s) returned across those queries
```

**The point:** search has **no relevance cutoff** — it returns top-k by score,
and an egg-boiling recipe arrives on a query about CI. So the recall metric
flatters, and precision@k is required. Also input for harness design.

---

# Part 4. UC1 — provider interchangeability

## Step 4.1 — compare two stores on one input

**T4:**

```bash
python live/facts.py check --hindsight
```

It prints two blocks in a row: `memoryhub` and `hindsight`, against the same
fixture.

**What to watch:**

* which facts reached both systems, and which reached only one;
* ranks and scores on identical queries;
* whether a secret reached either of them (both `leakage` lines should be
  clean — redaction happens **before** the fork, so it protects both stores at
  once);
* how many memories each counted from the same conversation.

## Step 4.2 — look inside Hindsight directly

**T5:**

```bash
BANK=${MEMORYHUB_CAPTURE_HINDSIGHT_BANK:-memoryhub-proxy-demo}
curl -s "localhost:8888/v1/default/banks/$BANK/memories/list?limit=50" | python -m json.tool | head -60
curl -s -X POST "localhost:8888/v1/default/banks/$BANK/memories/recall" \
  -H 'content-type: application/json' \
  -d '{"query":"what language is the backend being migrated to?","budget":"mid"}' \
  | python -m json.tool | head -40
```

**The point / what is proved:**

1. **The abstraction does not leak.** Hindsight is built in a fundamentally
   different way: it has neither threads nor messages, the whole conversation
   is one document with a `document_id`, extraction happens inside `retain`,
   and isolation is by "banks". All of that fit under the same three-method
   `Sink` contract. The capture core did not change by a single line.
2. **This is the answer to Ryan about "radio buttons".** A product knob "pick
   a memory provider" only makes sense if a contract sits under it. Here it
   is, and here is a second provider plugged in through it.
3. **This is the answer to Ray about the missing standard.** A standard cannot
   be declared; it can only be checked — by a second adapter being written
   with no core changes. Checked.
4. **This is shadow mode for the shared harness.** Running one task twice
   against different memory systems is not a comparison: memory changes the
   agent's answers, and by the second turn they are already two different
   conversations. Fan-out of one live conversation removes that problem for
   the memory layer.

**What to say about the honest limits:** the `HindsightSink` buffer lives in
process memory. A proxy restart loses it, and the next `retain` sends only
what it saw after the restart — Hindsight will upsert and the document **will
shrink**. Fine for a demo, not for production; a real adapter would restore
the buffer from the primary store.

---

# Part 5. UC4 — identity and personal → team

## Step 5.1 — a second session for the same person

**T2** — exit (Ctrl-D) and start a second session:

```bash
python agent.py --interactive --session demo-airflow --actor kromashk
```

Utterances:

```
Our data pipeline runs on Airflow and loads into Snowflake.
```
```
The nightly job must finish before 6am UTC.
```

**T4:**

```bash
python live/peek.py state
python verify.py threads
```

**What to watch:** two threads, separate cursors, and in `verify.py threads` —
`observed_actor=kromashk (header)`.

## Step 5.2 — a second person on the same gateway

**T5:**

```bash
python agent.py --scenario preferences --session team-bob --actor bob
```

Then:

```bash
python verify.py threads
python verify.py search "deployment target"
```

**What is being checked, and what will most likely break:**

* The gateway **sees** identity and writes it: `observed_actor_id` +
  `observed_actor_source` (header / `user` field / virtual key). This is
  exactly the enforcement point Sanjeev called "phase two, completely open" —
  and the gateway is the only one that sees the whole transport.
* **Actor isolation does not exist in the personal edition**: everything sits
  in one tenant, and Bob's search will most likely return Katya's memories. If
  so, that is not a failed demo, it is **a finding to show**: the personal
  edition is built as personal, and OpenClaw Enterprise targets teams. Between
  those two facts sits work that nobody has named yet.

Send me the `verify.py search` output — we will draft the issue from it.

---

# Part 6. UC3 — the zero-touch tier on real Codex

The most important part, and the only one where the answer may turn out
negative.

## Step 6.1 — point Codex at the gateway

**T5:**

```bash
codex --version
cat ~/.codex/config.toml 2>/dev/null | head -30
```

Then the simplest path:

```bash
OPENAI_BASE_URL=http://localhost:4000/v1 \
OPENAI_API_KEY=$LITELLM_KEY \
codex exec "In one sentence: what language is this project's backend being migrated to?"
```

If Codex ignores the environment variables, write the provider into
`~/.codex/config.toml`:

```toml
model = "poc-model"
model_provider = "memoryhub-proxy"

[model_providers.memoryhub-proxy]
name = "MemoryHub proxy"
base_url = "http://localhost:4000/v1"
env_key = "LITELLM_KEY"
wire_api = "chat"
```

> I am giving the field names from memory and **have not checked** them on your
> version. If codex complains — send me its error and the output of
> `codex --help`, and I will fix it in a minute. The fallback that definitely
> works: `python tools/harness_probe.py --scenario harness`. But that is a
> simulator, and the point of the step is a real agent.

## Step 6.2 — what to watch

**T1 and T3:** whether a `memoryhub_capture: session=...` line appeared at all
on the request from Codex.

**T4:**

```bash
python verify.py report
python live/peek.py state
```

**The point, and three possible outcomes — all three are useful:**

1. **Capture happened.** The third integration tier exists: a real coding
   agent, zero changes besides `base_url`, memory fills up. This is what is
   missing from Sanjeev's two-tier model, and it has to be shown before the
   harness is locked in.
2. **Capture happened, but the session was identified as `fingerprint`.** That
   means Codex has no way to pass `X-MemoryHub-Session`, and conversations
   glue together or scatter by the fingerprint of the first utterance. That is
   already a concrete product requirement, not an abstraction.
3. **No capture.** Then look at which endpoint Codex used. Per the research,
   LiteLLM hooks **do not fire** on `/mcp/` (litellm#25011) or on
   `/v1/messages` (litellm#27518) — and those are exactly the endpoints modern
   coding agents use. **This is a direct threat to the claim "works with any
   agent with no changes"**, and learning it now is incomparably cheaper than
   in October at the harness discussion.

## Step 6.3 — check the Anthropic path separately

Regardless of the 6.2 result, check `/v1/messages` on its own:

```bash
curl -s -X POST http://localhost:4000/v1/messages \
  -H "x-api-key: $LITELLM_KEY" \
  -H 'anthropic-version: 2023-06-01' \
  -H 'content-type: application/json' \
  -d '{"model":"poc-model","max_tokens":64,"messages":[{"role":"user","content":"Remember: the release train is weekly, on Thursdays."}]}' \
  | head -20
```

Then **immediately** in T3 and T4 — whether a new observation line and a new
message appeared in the thread.

**The point:** this is a one-line test for a blocker found in the research.
The answer is "yes" or "no" — and in both cases we have something to say at
the next meeting.

---

# Part 7. Final summary

**T4:**

```bash
python verify.py report
python live/facts.py check --hindsight --search
python live/peek.py memories --all
python live/peek.py decisions
```

**T5:**

```bash
python -m pytest tests/ -q
```

The last one is not a formality: `test_extraction_does_not_block_the_callback`
is the regression for the bug that killed the first run (LiteLLM cancelled
extraction on a 20-second timeout), and `tests/test_redact.py` holds redaction
in place.

---

# What to send me

Copy the output **in full**, marking which terminal it came from (as last
time — that worked very well):

1. `env.sh` banner + `check.sh` — T1
2. The `ready (...)` line and `shadow mode` — T1
3. All `memoryhub_capture:` lines for the run, including `REDACTED=` — T1 or T3
4. `peek.py thread` after the utterance with the secret — T4
5. `peek.py decisions` and `peek.py memories --all` — T4
6. `facts.py check --hindsight --search` — T4
7. The Hindsight output from `curl` — T5
8. Everything Codex printed, including errors — T5
9. The response to `curl .../v1/messages` and what appeared (or did not) in T3 — T5
10. `verify.py threads` and `verify.py search "deployment target"` — T4
11. `pytest` — T5

If something fails — send the error **and leave it unfixed**: the cause of the
failure is often the finding. Last time, three of the five most valuable
conclusions came from steps that broke.

---

# Checklist before recording

- [ ] `.env` is not open in any terminal and will not be opened
- [ ] Secrets in the utterances are only the fake ones from this document
- [ ] `embeddings onnx`, not MOCK
- [ ] `out/live-data` is deleted, the database is created from scratch
- [ ] Hindsight is up **or** `MEMORYHUB_CAPTURE_SHADOW_SINK` is empty and that
      has been said out loud
- [ ] Ollama is already warmed up (`ollama run qwen2.5:7b` once before
      recording), otherwise the first reply waits for the weights to load
- [ ] Font is large, T3 and T4 are readable — on the previous video that was
      the main complaint about the frame
