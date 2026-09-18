# Demo: implicit memory capture through an LLM gateway

**Length:** 10–15 minutes, one terminal.
**Claim being demonstrated:** getting memory into an agent today means integrating
the harness — an MCP tool, a rules file, hooks, an instruction in every prompt.
A gateway gets the same memory out of the traffic, and the agent's only change is
where the model lives.

```bash
cd demos/litellm-proxy-capture
./demo.sh real            # the version to show; ./demo.sh alone runs offline stand-ins
```

Everything lands in `out/demo-data/` (SQLite, personal edition). Act 1 and act 2
use separate databases, so the two approaches are never mixed.

## Before you start

- `uv pip install -r requirements.txt -e ../../memoryhub-local`
- `.env` with a model for the agent (`POC_MODEL`, `POC_MODEL_API_KEY`) and an
  extraction endpoint (`MEMORYHUB_CAPTURE_EXTRACT_MODEL`,
  `MEMORYHUB_CAPTURE_EXTRACT_MODEL_URL` — Ollama at `http://localhost:11434/v1`
  works). Keys live in `.env` only; nothing prints them.
- `memoryhub doctor` must report the ONNX embedding model, not `mock`. With mock
  embeddings search ranking is meaningless and the demo falls apart.

## Act 1 — how it works today (3 min)

`agent_explicit.py` is the harness as it exists now: it carries the memory
instruction in its system prompt, makes an extra model call after every turn to
decide what to keep, and writes the memory itself.

What to point at:

- the instruction the script has to ship — multiply it by every harness;
- `6 LLM calls for 3 turns`: the decision call is a real cost;
- the memories it wrote — no thread, no provenance, nothing to audit against.

## Act 2 — the same conversation through the gateway (3 min)

`agent.py` has no MCP tool, no memory instruction, no hooks — the script prints
the grep to prove it. The only difference is `--base-url` pointing at the
LiteLLM proxy.

Run the same three turns. Nothing in the output mentions memory.

## Act 3 — the memory is already there (3 min)

`verify.py search "dark mode"` finds the preference, with `source=extraction`
and a link back to the thread and the message numbers it came from. No manual
extraction step: the proxy called it from the traffic after the second user
turn (`MEMORYHUB_CAPTURE_EXTRACT_EVERY=2`).

`verify.py threads` shows the thread, the session key, the message count and the
extraction cursor.

## Act 4 — what it does not solve (3 min)

This is the part worth the audience's attention, and it is also the answer to
"so client-side config disappears?" — no, it shrinks:

- **Session boundaries.** Without `X-MemoryHub-Session` the proxy has to
  fingerprint the first user message: two conversations that start the same way
  merge, and a compacted history splits. One header versus MCP + rules + hooks
  per harness is still a big win, but it is not zero config.
- **Identity.** The memory belongs to whoever the proxy authenticates as. The
  real speaker is metadata (`observed_actor_id`) — for a shared gateway this is
  the open architectural question.
- **Corrections.** "eu-west-1, not us-east-1" is stored as a second fact, not as
  a correction of the first.
- **Write-path asymmetry (personal edition).** Extraction skips a fact the agent
  already wrote, but an agent write *after* extraction creates a duplicate: the
  local `write` path has no similarity gate. Measured in `RESULTS-local.md`.
- **What the gateway cannot see.** Subagent relationships, and tool calls only as
  serialized blocks.

## If someone asks about Claude Code

With `ANTHROPIC_API_KEY` in `.env`, point Claude Code at the proxy:

```bash
ANTHROPIC_BASE_URL=http://localhost:4030 ANTHROPIC_AUTH_TOKEN=$LITELLM_MASTER_KEY \
ANTHROPIC_CUSTOM_HEADERS="X-MemoryHub-Session: demo-cc" \
MEMORYHUB_CAPTURE_IGNORE_MODELS=haiku claude
```

Without a key, `tools/harness_probe.py` sends the same shape of traffic
(Anthropic Messages API, `tool_use`/`tool_result`, an injected
`<system-reminder>`) so the capture path can still be shown.

## If something goes wrong

- Search returns nothing → check `verify.py threads` for the extraction cursor,
  then `verify.py extract`; a failed extraction window still moves the cursor.
- `embeddings: mock` in the output → the ONNX model was not downloaded; stop and
  fix it before demoing.
- Port busy → `PORT=4031 ./demo.sh real`.
