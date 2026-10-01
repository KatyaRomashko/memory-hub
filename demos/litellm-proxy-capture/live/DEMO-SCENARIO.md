# Demo: capture at the gateway + read through MCP

**Run scenario · 7 acts · version of 30 Sep 2026**

---

## 0. The line everything rests on

> **Write where the traffic is. Read where the context is.**
> The gateway captures conversations unconditionally, for any agent; the only
> change is `base_url`. The model reads and edits memory through MCP, when it
> needs to.

Each act confirms one of the two halves. What does not confirm either half is
cut.

**Two versions of one run.** You record the long one (25–30 minutes) — it is
for review, for an offline conversation, and as an appendix to the document.
The **4-minute cut** for Sanjeev's format of "3–5 minutes per person" is
edited from the same material: acts 3, 4, and 5 with no pauses, one frame
each. Section 9 spells out what goes into it.

---

## 1. What to shoot before the recording, separately

Three things can fail live and eat half the time. Run them in advance; the
demo shows the result.

**Hooks on the Anthropic shape.** The "any agent" claim depends on this.

```bash
source live/env.sh && ./live/proxy.sh ollama        # in T1
curl -s -X POST http://localhost:4000/v1/messages \
  -H "x-api-key: $LITELLM_KEY" -H 'anthropic-version: 2023-06-01' \
  -H 'content-type: application/json' \
  -d '{"model":"poc-model","max_tokens":64,
       "messages":[{"role":"user","content":"Remember: releases ship Thursdays."}]}'
```

Watch whether an observation line appeared in T3 and a message in the thread.
Bring the answer into the demo either way — a negative one too.

**Which endpoint Codex uses.** Your remark is correct and more important than
it looks: recent versions probably talk to `/v1/responses`, and
`wire_api = "chat"` may be outdated. The normalizer in
`capture_core.normalize_messages` handles two shapes — OpenAI chat and
Anthropic; `response_messages` also knows the Responses API (`"output"` in the
response), but a **request** in that shape has never been parsed. So UC3 can
fail because of the request shape, not because of LiteLLM hooks, and that is a
different bug with a different fix.

```bash
codex --version
OPENAI_BASE_URL=http://localhost:4000/v1 OPENAI_API_KEY=$LITELLM_KEY \
  codex exec "In one sentence: what language is this project's backend moving to?"
# and immediately in T3/T4: did a line and a message appear
```

**Hindsight with a local model.** The document says "it works and is
implemented" — but the adapter has never had a live run, and Hindsight
requires a model with structured output. Check that `retain` does not fail:

```bash
docker start hindsight && curl -s localhost:8888/health/live
export MEMORYHUB_CAPTURE_SHADOW_SINK=hindsight && source live/env.sh && ./live/check.sh
```

If `qwen2.5:7b` cannot handle it — try `gemma3:12b`. If it does not come up in
twenty minutes, act 6 goes without Hindsight, and that has to be said out
loud, not papered over.

---

## 2. Preparation and layout

Five terminals. T1 top left, T2 bottom left, T3 top right, T4 bottom right;
T5 opens along the way.

**In every terminal, first thing:**

```bash
cd ~/MemHub/memory-hub/demos/litellm-proxy-capture
export MEMORYHUB_CAPTURE_SHADOW_SINK=hindsight     # drop this if act 6 is without Hindsight
source live/env.sh
```

**Once, before recording:**

```bash
rm -rf out/live-data out/live-observations.jsonl   # start from zero
./live/check.sh                                     # should be 0 to fix
ollama run qwen2.5:7b "warm up"                     # otherwise the first reply waits for the weights to load
python -m pytest tests/ -q                          # 36 passed
```

Frame checklist: `.env` is not open and will not be opened; secrets in the
utterances are only the fake ones from act 3; the banner says
`embeddings onnx`, not MOCK; font is large, T3 and T4 are readable — on the
previous video that was the main complaint.

---

## 3. Act 1 — framing the problem · slide · 2–3 min

On screen, two pictures from the document: the memory pipeline (section 2)
and the pair "without a gateway / with a gateway" (section 7).

**What to say.** The two paths are different in kind. Writing is
unconditional: an utterance is either captured or it is not, and the agent's
reasoning does not affect that — that is work for infrastructure. Reading is
conditional: it helps only when memory is appropriate, and an injection at the
wrong moment is worse than doing nothing — that is a decision, and the model
makes it. The number of integrations drops from `agents × systems` to
`1 + systems`.

**Do not say** "agents cannot be configured". Codex and Claude Code can be
configured. The right formulation: configuration is possible, but it is per
agent, per developer, and per machine — it cannot be turned on centrally, you
cannot see who has it, and you cannot notice when a reinstall wiped it. And
even where it exists, capture depends on whether the model calls the tool.

---

## 4. Act 2 — the code: how little there is · 3 min

Three fragments. Do not open anything else.

```bash
# 1. the only place memory is mentioned at all
grep -n -A2 "callbacks" config.ollama.yaml

# 2. the callback: runs after the response; normalization, redaction, session, delta
sed -n '/async def async_log_success_event/,/await self._capture/p' memoryhub_capture.py
sed -n '/full = transcript(/,/session = derive_session/p' memoryhub_capture.py

# 3. the whole storage contract — three verbs
sed -n '/^class Sink(Protocol)/,/def extract/p' sinks.py
```

**What to say.** Extraction is moved into a separate task because LiteLLM
cuts the callback off after 20 seconds
(`LOGGING_WORKER_MAX_TIME_PER_COROUTINE`). There is a regression test: the
callback returns in under 150 ms while the slow extraction is still running.
A gateway callback is not the place for slow work, and that is a general
rule, not a quirk of ours.

---

## 5. Act 3 — live capture and redaction · 5 min

**T1**

```bash
./live/proxy.sh ollama
```

Wait for two lines:

```
memoryhub_capture: ready (sink=local extract_every=2 tools=False
                          max_bytes=8000 when_agent_writes=defer redact=on)
memoryhub_capture: shadow mode -- primary=local shadow=hindsight
```

**T3 and T4**

```bash
python live/watch.py           # T3
python live/peek.py state      # T4  ->  no database yet
```

**T2**

```bash
python agent.py --interactive --session demo-live
```

Two utterances, one at a time, waiting for the reply:

```
For the upcoming 'Nexus' platform upgrade, we decided to migrate our entire backend to Go. The API documentation must be finalized by Friday
```

```
Here is the staging deploy line I keep forgetting: AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE and the registry token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345
```

> Both values are fake — `AKIAIOSFODNN7EXAMPLE` is the official example from
> the AWS docs. Fine to show on camera. Do not open a real `.env`.

**Four frames this act exists for**

| Where | What to show |
|---|---|
| T3 | `msgs 4  new 2` — the delta: the client resent the whole history, only the new messages were stored |
| T1 | `REDACTED=2['aws_access_key_id', 'github_token']` |
| T4 | `python live/peek.py thread` — the text has `[redacted:aws_access_key_id]`, the key is gone |
| T5 | `grep -c "AKIAIOSFODNN7EXAMPLE" out/live-data/memoryhub/memoryhub.db` → `0` |

**What to say.** Redaction fires before storage **and before hashing** — the
session key is a hash of the first utterance, and credentials must not survive
even as far as the hash function's input. It is idempotent, otherwise the
delta tracker would treat old messages as new on every resend; there is a
test for that.

**Say the caveat yourself, before anyone asks:** this is a filter on the
*shape* of credentials. A secret written as prose will get through. It
reduces risk, but it is not a compliance control, and it cannot be sold as
one. Add, honestly: this is the first live run with redaction — the 23
September rehearsal did not have it yet.

---

## 6. Act 4 — hybrid with a real MCP · 5 min · **the main point of the task**

This is what none of the previous versions had: **a live MemoryHub MCP
server, connected to an agent that talks through the gateway.** `live/mcp_agent.py`
was written for this. It starts `python -m memoryhub_local` over stdio against
the same database the gateway writes to, and gives the model exactly one
tool — `memory`. The `thread` tool is deliberately withheld: the gateway
writes the transcript, and a second writer into the same thread is not needed.

**T5**

```bash
python live/mcp_agent.py --interactive --session demo-mcp
```

On start it prints exactly what was given to the model:

```
MCP server ready — tools exposed to the model: ['memory']
gateway: http://localhost:4000   session: demo-mcp
```

### 6.1 The model reads memory itself

```
What did we decide about the backend language?
```

In T5 you see the tool call and the tool response:

```
   [mcp] memory({"action": "search", "query": "backend language"})
   [mcp] -> ... The entire backend for the 'Nexus' platform upgrade will be migrated to Go...
```

**T3**

```
demo-mcp  (header)  msgs N  new N [user,tool_call,tool_result,assistant]  appended 2
```

Note: `new` is larger than `appended`. The difference is the tool blocks the
gateway saw but did not write into the thread.

**T4**

```bash
python live/peek.py thread demo-mcp
```

The thread is a clean dialogue: the user's question and the assistant's
answer. No `tool_call`, no `tool_result`.

**What to say.** This is where the loop problem from section 11 of the
document closes. When the model reads memory and the result lands in its
context, the next call brings that result back in the history. If everything
were written into the thread, memory would be recorded again as "something
the user said", with provenance on a message nobody wrote. Tool blocks are
cut out of storage — and there is no loop. This is not a workaround; it is
the reason the read path was taken out of the gateway.

### 6.2 The model writes memory itself

```
New decision, please remember it: the on-call rotation starts every Monday at 09:00 UTC.
```

```
   [mcp] memory({"action": "write", "content": "On-call rotation starts every Monday at 09:00 UTC"})
```

**T3**

```
demo-mcp  (header)  msgs N  new 4 [tool_call,tool_result,user,assistant]  appended 2
          agent wrote 1 memory/memories itself; extraction deferred (agent_wrote:1)
```

**What to say.** MCP calls never reach the gateway — the agent talks to the
memory server directly. But the *decision* is visible in the traffic: the
model replied with a `tool_use` block, and the next request brought its
result. From that, the gateway understood that the agent closed this turn
itself, saved the transcript, and skipped its own extraction pass. The agent
is the fast path; "dreams" are the safety net.

And one more detail worth showing on purpose: **`search` does not count as a
write**. A read through MCP does not defer extraction; only `write`, `update`,
and `relate` do. Check it on the spot:

```bash
python live/peek.py state          # demo-mcp has cursor=0, extractions=0 after the write
```

**If the model does not call the tool.** `qwen2.5:7b` calls tools through
LiteLLM → Ollama, but not always on the first try. Fallback moves, in
decreasing order of honesty: rephrase the utterance more explicitly ("search
your memory and tell me…"); use `--scenario recall` and `--scenario write`,
where the utterances are chosen for this; as a last resort —
`harness_probe.py --scenario agent-writes`, **calling it a simulator out
loud**. The difference is fundamental: `mcp_agent.py` is real MCP,
`harness_probe.py` is traffic of the right shape with no server behind it.

---

## 7. Act 5 — extraction, decisions, fact preservation · 7 min · the most valuable

**T2**, continuing the `demo-live` session:

```
The API must stay backwards compatible with v2 clients until the end of the year.
```
```
We're moving CI from Jenkins to Tekton.
```

**T4** — the best frame of the previous video; show it before extraction fires:

```bash
python live/peek.py next
```

```
--- window 1  (seq 5..6) ---
[USER] (seq=5): Let's also move the CI pipeline to Tekton.
[ASSISTANT] (seq=6): Moving your CI pipeline to Tekton is a good idea...
```

**What to say.** "Dreams" receive a flat transcript with sequence numbers.
Nothing else. Everything that goes wrong after that is a property of what
landed in that text, and of where the window boundary fell.

Then the correction:

```
After a meeting we decided to migrate the backend to Rust, not Go.
```

**T4**

```bash
python live/peek.py decisions
python live/peek.py memories --all
python live/facts.py check
python live/facts.py check --search      # ~10 s, loads the embedding model
```

**What to look for and what to say.**

In `decisions` — `update` rows with a score between 0.85 and 0.98. Where two
different facts merged, that is an error, and it is visible in the "what it
replaced" column. If the Go → Rust correction came out as `create` with "no
similar memory found" — its target was already displaced by an earlier false
merge.

In `facts.py check` — three numbers, separately: how many expected facts
reached the store (extraction), how many of them are still the current
version (preservation), and whether any of the watched secrets reached the
database (leakage). Line by line you can see **which merge** lost the fact.

The main line of the act: end-to-end recall does not distinguish "the system
decided not to store it" from "the system stored it and then erased it
itself". The first is a question of prompt quality. The second is silent data
loss, and no published metric sees it. The gateway did not cause these bugs.
It made them visible — and that is an argument for a capture layer with its
own telemetry, on its own.

In `--search` — ranks and precision, and an egg-boiling recipe arriving on a
query about CI. Search has no relevance cutoff (F7), so the recall metric
flatters.

> **Quote numbers only from this run.** Previous materials carry two sets —
> 0.8855 / 0.8819 / 0.8668 from the 23 September rehearsal and 0.9171 / 0.8622
> from the recorded video. Do not mix them; say out loud whose run is on
> screen.

---

## 8. Act 6 — provider interchangeability · 3 min

**T4**

```bash
python live/facts.py check --hindsight
```

Two blocks in a row, against the same fixture: `memoryhub` and `hindsight`.

**T5**

```bash
BANK=${MEMORYHUB_CAPTURE_HINDSIGHT_BANK:-memoryhub-proxy-demo}
curl -s "localhost:8888/v1/default/banks/$BANK/memories/list?limit=50" | python -m json.tool | head -40
curl -s -X POST "localhost:8888/v1/default/banks/$BANK/memories/recall" \
  -H 'content-type: application/json' \
  -d '{"query":"what language is the backend being migrated to?","budget":"mid"}' \
  | python -m json.tool | head -30
```

**What to say.** The same text landed in both systems at the same moment —
same input, same sessions, same model. This is the only clean way to compare
memory systems: running one task twice against two systems is not a
comparison, because memory changes the agent's answers and by the second
utterance they are two different conversations.

And say why Hindsight: it has neither threads nor messages — a conversation is
one document, extraction happens inside the write, and the raw text is not
stored at all. If the three-verb contract survived both it and MemoryHub, the
abstraction is checked, not merely claimed.

**Honest limits, name them immediately.** The adapter buffer lives in the
gateway process: a restart loses it, and the next `retain` sends a shorter
document — Hindsight upserts, so the document shrinks rather than doubling.
Fine for a demo, not for production. And only two adapters are implemented,
MemoryHub and Hindsight; Mem0, Zep, and Cognee on the diagram are a plan, not
code.

---

## 9. Act 7 — limits and three requests · 3 min

Identity (a second actor, the expected leak between users) and Codex were
shot in advance — you show the result, not the run.

```bash
python verify.py threads                         # observed_actor and its source
python verify.py search "deployment target"      # does Bob see Katya's memory
python verify.py report                          # summary, including credentials redacted
```

**What to say about the limits.** Without the `X-MemoryHub-Session` header,
the session is identified by the fingerprint of the first utterance, and that
has two known failures: conversations with the same opening glue together,
and compaction splits one conversation into two. Both are visible in the
observation log. The honest formulation: zero changes in the agent, but with
a degraded session heuristic — and with the header it is still one small
change.

Scale is not shown: extraction is an LLM call every N utterances for every
session in the fleet, inside the gateway process. Production needs a queue
and a worker separate from the gateway, and an answer to who pays for the
extraction tokens.

**End with three requests**, not with conclusions:

1. **F5** — wire in the tiebreaker in the ambiguous band. A correct correction
   and a false merge are indistinguishable by score, and a false merge
   silently deletes a fact. Calibration data is already being collected in
   `reconciliation_decisions`.
2. **F4** — a grounding rule in the extraction prompt, in both copies. The
   question is not whose utterances to take, but whether a statement was
   asserted, decided, or confirmed by a participant.
3. **F3 and F6** — one transaction per window, covering memories, provenance,
   and the cursor advance; and distinguish "the model returned an empty list"
   from "the window was not processed".

---

## 10. The 4-minute version

The format Sanjeev asked for: 3–5 minutes per person. Edited from the same
material, with no new runs.

| Time | Frame | One line |
|---|---|---|
| 0:00–0:30 | the `ready (... redact=on)` line and starting `agent.py` | "The only change in the agent is base_url" |
| 0:30–1:15 | T3 `msgs 4 new 2`, T1 `REDACTED=2[...]` | "Delta and redaction before storage and before hashing" |
| 1:15–2:15 | T5 `[mcp] memory(search)` → T4 a clean thread → T3 `agent_wrote:1` | "The model reads and writes through MCP; the gateway sees it and yields the turn" |
| 2:15–3:15 | `peek.py decisions`, then `facts.py check` | "End-to-end recall does not distinguish 'did not store' from 'erased it itself'" |
| 3:15–4:00 | `facts.py check --hindsight` | "One conversation, two stores, the same input" |

Say the token number in the 3:15 frame — it is the only number Sanjeev asked
for and that nobody has.

---

## 11. Arguments and what backs each one

| Argument | What you show |
|---|---|
| Zero changes in the agent, besides `base_url` | the `ready` line; `agent.py` has no MCP, no prompt, no hooks |
| Capture is unconditional | in an MCP-only model, a conversation where the model did not call the tool is lost, and that is recorded nowhere. Act 4 shows the opposite: the thread is complete whether or not it called the tool |
| Reaches agents that cannot be changed | Codex, if the pre-check passed |
| Adds no latency | 7–9 s of extraction after the response is delivered; the callback returns in < 150 ms, there is a test |
| No duplicates on resends and restarts | `new 2` at `msgs 14`; `new 2` after a gateway restart |
| Independence from the memory system | three verbs covered MemoryHub (threads) and Hindsight (documents) with no core changes |
| One point for identity, redaction, tenancy, audit | `observed_actor_source`, the `redactions` counter, the observation log |
| The write stage is measurable | `facts.py`: preservation as its own metric |
| Comparing systems without distortion | shadow: one input, one moment |
| The read path does not break the cache and does not create a loop | act 4: tool blocks are cut out of storage, the request is untouched |

---

## 12. Questions and exact answers

Some answers changed after the sources were checked — marked where they did.

**"Injection at the proxy breaks the cache, hence a 10× bill?"**
Your correction is right, and the formulation has to be careful. At Anthropic,
a cache read costs 0.1× the input price, a cache write 1.25×. So a broken
cache raises the price of the affected prefix from 0.1× to 1.25× — that is
**12.5× relative to what caching would have cost**, and 1.25× relative to no
cache at all. The "about 10×" figures from field reports are that same ratio,
not a tenfold bill for the whole request. And it is fixable: dynamics after
the last breakpoint, the cache stays intact. Conclusion: **money is not the
main argument against injection.** The main three: the gateway does not know
when memory is needed; the loop (act 4 shows what treats it); the prompt is
not reproducible for the agent's author.

**"This already exists: supermemory, MemoryRouter…"**
It does, and I would now add a third example — **MemoryProxy, a component of
the open-source repository `TencentCloud/TencentDB-Agent-Memory`** (MIT, port
8096, branch `master`). Its pipeline: `auth → sessionInit → injection →
forward`, the raw L0 dialogue is written to sqlite, background workers drive
L1 → L2 → L3. Integration is a base_url change, and the documented
integrations include Claude Code, Codex, and OpenClaw.

This is a double-edged fact, and it is better to bring it yourself. It
**strengthens** you on one point: the basic scheme "change base_url and the
agent is captured" works with Claude Code and Codex, so the claim is not
fantasy. And it does **not** cancel your distinction: all three bind
injection in. You forward the request untouched, so an unrecognized API shape
costs a missed capture, not a failed call.

*Two caveats when citing:* `MemoryProxy/` exists on `master` and returns 404
on `main` — give the path explicitly. And the name "MemoryRouter" is worth
rechecking before you say it: the supermemory page at that path opens, but
the name itself is no longer on it; it looks like a rename.

**"Cloudflare, Langfuse, Helicone and so on already log everything at the gateway"**
Logs are telemetry, not memory. You have fact extraction, reconciliation,
versions, sessions via delta, and provenance down to the message number. Show
`peek.py memories` with `<- thread … messages [3, 4]`.

**"Without a read path, what is the value?"** — the main question from the
review. There is a consumer of the read path, and it is in act 4: MCP reads
of the same store. That is the hybrid. The second consumer is the comparative
harness: shadow plus `facts.py`.

**"You said 'only base_url', but `agent.py` sends a header"**
Yes. Without `X-MemoryHub-Session` the session is identified by the
fingerprint of the first utterance, with two known failures. The honest
formulation: zero changes, but with a degraded session heuristic.

**"Nobody measures the write stage" — true?** — *the answer changed after checking.*
Almost true, and in your favor. **Ground Truth First** (Quentin Spencer,
arXiv:2607.21962, 24 July 2026) does do a per-fact audit of the write: every
stored memory is scored against a checklist of atomic facts, with statuses
correct / distorted / absent. The numbers in the body of the paper are more
precise than the abstract: a question whose fact was stored badly misses
**24.2%** versus **1.6%** with a clean write. Those are **downstream misses
caused by write quality**, not the rate of bad writes.

But the key point: that work measures **only the moment of the write**. It
does not follow a stored fact further, through the system's own consolidation
and merges. Displacement appears there as a way of presenting the data, not
as a measurement. So **"fact preservation after reconciliation" remains
unmeasured**, and your `preservation` does not duplicate someone else's
metric.

Formulate it this way: "a per-fact audit of the write already exists — Ground
Truth First, July 2026; what does not exist is a metric of whether a fact
survives the system's own merge". And name the caveats yourself: it is a v1
preprint, one author, a synthetic corpus of about 380 questions, and the
authors explicitly write that causality is not isolated.

**Mem2ActBench** (ACL 2026, `2026.acl-long.370`) measures memory **in action**
— whether the agent picked the right tool and filled in the parameters, not
whether it answers a question about the past. Write and read are not split
there: the score is end-to-end on the final action. Useful neighboring work,
not a competitor to your metric.

**"The proxy writes everything. What about memory poisoning?"** — *the attack
channel was named incorrectly; correct it.*
A weak spot, and it is better to name it yourself. The paper exists: *When
Malicious Instructions Persist: Persistent Memory Poisoning Attack on
Harness-Based Agents* (arXiv:2609.13889, 12 September 2026), on OpenClaw and
Claude Code. **The channel is not a tool result and not the conversation; it
is content the agent ingests: text documents, PDFs, and images.** A malicious
instruction lands in persistent memory during an ordinary task and fires in
the next session. Success rates: OpenClaw 73.7% injection and 55.5%
cross-session attacks, Claude Code 66.9% and 81.7%; on text, up to 99.4%.
The authors note that prompt-level defense reduces the injection itself, but
barely helps once memory is already poisoned.

What to say next: we cut tool blocks out of storage, but **the assistant's
retelling is still stored**, so the channel is not closed. Grounding (F4)
plus provenance by source trust level is what treats it. Open work.

**"Retention, forget, consent?"**
Not done. Deletion has to propagate to derivatives: embeddings, versions, the
copy in Hindsight. Name it as open work; do not pretend it exists.

**"And scale?"**
Not shown; see act 7.

**"Who runs deferred dreaming after defer?"**
Have the answer ready in advance. If the agent wrote one fact and the window
had five more, they wait for "dreams". In the personal edition that is
`verify.py extract` or `reextract`, meaning by hand. There is no automatic
scheduler — and that is the next step for the hybrid policy, because without
it `defer` means "deferred forever".

**"Are Mem0, Zep, Cognee supported?"**
Two adapters are implemented: MemoryHub and Hindsight. The rest is section 13
of the document, and the diagram has to be labeled that way.

**"Does the cluster behave the same way?"**
No. Everything shown is on the personal edition; the cluster has windows of
4, a circuit breaker is wired in, and long messages spill to S3. The claim
"the cluster produces duplicates" is backed by nothing — there was no access
to the cluster in this run. Remove it or check it.

---

## 13. What to send me after the run

Mark the terminal, as last time — that worked very well.

1. The `env.sh` banner and `check.sh` — T1
2. `ready (...)` and `shadow mode` — T1
3. All `memoryhub_capture:` lines for the run, including `REDACTED=` — T1/T3
4. `peek.py thread` after the utterance with the secret — T4
5. The full `mcp_agent.py` output, including the `[mcp]` lines — T5
6. `peek.py thread demo-mcp` and `peek.py state` — T4
7. `peek.py decisions` and `peek.py memories --all` — T4
8. `facts.py check --hindsight --search` — T4
9. The Hindsight `curl` — T5
10. Pre-check results: `/v1/messages`, Codex, Hindsight health
11. `verify.py report` and `verify.py threads` — T4
12. `pytest` — T5

If something fails — send the error and **leave it unfixed**: last time, three
of the five most valuable conclusions came from steps that broke.

---

## 14. Document discrepancies to close before the showing

Your section 4 — I checked it against the original rehearsal report. Four
items confirmed as my splicing errors; the rest need wording fixes.

| Where | What is wrong | What is correct |
|---|---|---|
| Section 9, state summary | `messages 16` with `demo-live-2 messages=4` — two different moments in one block | at the moment of `messages 16`, `demo-live-2` had `messages=2, cursor=0` |
| F7 | "egg 0.7141 versus 0.8334" — a mix of two queries | both numbers are from the `backend language` query: egg **0.7287**, the relevant memory **0.8334** |
| F5 | the documents have 0.8855 / 0.8819 / 0.8668, walkthrough v2 has 0.9171 / 0.8622 | these are two different runs; name which one, and do not mix them |
| Section 0 | "all products integrate both jobs inside the agent" versus section 6 | "almost all; two exceptions do both on the proxy" |
| Section 11 | "roughly tenfold bills" | rephrase per the first question in section 12 |
| Section 15 | "nobody splits extraction and retrieval" | soften: a per-fact audit of the write exists (Ground Truth First); a preservation-after-reconciliation metric does not |
| Memora | "degrade 18.2% → 29.5%" reads as accuracy or error, and it is neither | it is the **size of the penalty** by which FAMA falls below MPA, and it grows with the horizon: 18.2 points at a week, 29.5 at a quarter. Ordinary LLMs go the other way, 32.6 → 17.8, and for an artifact reason: the long history does not fit in the window |
| Section 5 | "redaction is implemented" | true, but add: there has not been a live run with it yet |
| F5, about the cluster | "the cluster produces duplicates" | there was no access to the cluster; remove it or check it |

Say the word — I will fix all of this in `capture-at-the-gateway.md` and in
the Russian version in one pass.
