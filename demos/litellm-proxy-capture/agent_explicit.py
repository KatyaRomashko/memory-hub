"""The baseline agent: it manages its own memory, the way harnesses do today.

This is the "explicit" arm of the comparison. The agent decides after every
turn whether something is worth remembering and writes it itself -- the
equivalent of an MCP ``write_memory`` call, plus the instructions that tell it
to make one. It writes into the same personal-edition database the proxy uses,
so both arms can be measured against each other.

    python agent_explicit.py --scenario preferences

The decision is made by the agent's own model (one extra LLM call per turn,
which is part of what this approach costs). If that model does not answer with
usable JSON -- mock models in the offline run do not -- the agent falls back to
a keyword rule, and the fallback is reported so the numbers stay honest.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from openai import OpenAI

sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))

from agent import SCENARIOS, SYSTEM_PROMPT  # noqa: E402

# The instruction a harness has to ship today (rules file, system prompt, MCP
# tool description). Its cost in tokens is charged to this arm.
MEMORY_INSTRUCTION = (
    "After answering, decide whether the exchange contains something worth "
    "remembering for future sessions (a preference, a decision, a durable fact). "
    'Reply with JSON only: {"save": true|false, "memory": "self-contained statement"}.'
)

KEEP = ("i prefer", "i always", "we decided", "decided", "target is", "moves to", "i use", "i live")


def fallback_decision(user_text: str) -> str | None:
    low = user_text.lower()
    if any(k in low for k in KEEP):
        return re.sub(r"^(sorry,\s*correction:\s*|hi!\s*quick context about me:\s*)", "", user_text, flags=re.I).rstrip(".")
    return None


async def write_memory(content: str) -> str:
    from _local import open_backend

    from memoryhub_local.services.memory import create_memory

    state, _ = await open_backend(announce=False)
    async with state.session_factory() as db:
        node = await create_memory(
            db, content, state.embedding_service,
            weight=0.8, content_type="declarative",
            metadata={"written_by": "agent_explicit", "poc": "WRIG-1482"},
        )
        await db.commit()
        return str(node.id)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL", "http://localhost:4010"))
    ap.add_argument("--api-key", default=os.environ.get("LITELLM_KEY", "sk-poc-local"))
    ap.add_argument("--model", default=os.environ.get("LLM_MODEL", "poc-model"))
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), default="preferences")
    ap.add_argument("--stats", help="write a JSON summary of this run to this path")
    args = ap.parse_args()

    client = OpenAI(base_url=args.base_url, api_key=args.api_key)
    messages = [{"role": "system", "content": SYSTEM_PROMPT + "\n\n" + MEMORY_INSTRUCTION}]
    stats = {"turns": 0, "llm_calls": 0, "prompt_tokens": 0, "memories": [], "fallbacks": 0}

    for text in SCENARIOS[args.scenario]:
        messages.append({"role": "user", "content": text})
        reply = client.chat.completions.create(model=args.model, messages=messages)
        stats["llm_calls"] += 1
        stats["prompt_tokens"] += getattr(reply.usage, "prompt_tokens", 0) or 0
        answer = reply.choices[0].message.content or ""
        messages.append({"role": "assistant", "content": answer})
        stats["turns"] += 1
        print(f"\nuser> {text}\nagent> {answer}")

        # the extra "should I remember this?" call every harness has to make
        # the instruction goes in as a system message: the capture proxy skips
        # system content, so this harness scaffolding never becomes a "memory"
        decision = client.chat.completions.create(
            model=args.model,
            messages=messages + [{"role": "system", "content": MEMORY_INSTRUCTION}],
        )
        stats["llm_calls"] += 1
        stats["prompt_tokens"] += getattr(decision.usage, "prompt_tokens", 0) or 0
        raw = decision.choices[0].message.content or ""
        memory = None
        try:
            parsed = json.loads(raw)
            if parsed.get("save"):
                memory = str(parsed.get("memory") or "").strip() or None
        except (json.JSONDecodeError, AttributeError):
            memory = fallback_decision(text)
            stats["fallbacks"] += 1

        if memory:
            memory_id = asyncio.run(write_memory(memory))
            stats["memories"].append(memory)
            print(f"  [agent wrote memory {memory_id[:8]}] {memory}")

    print(f"\nexplicit run: {stats['turns']} turns, {stats['llm_calls']} LLM calls, "
          f"{len(stats['memories'])} memories, {stats['fallbacks']} rule-based fallback(s)")
    if args.stats:
        Path(args.stats).write_text(json.dumps(stats, indent=1))


if __name__ == "__main__":
    main()
