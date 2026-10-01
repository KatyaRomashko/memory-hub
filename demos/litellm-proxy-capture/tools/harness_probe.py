"""Send harness-shaped traffic through the proxy: Anthropic /v1/messages with
tool blocks and an injected <system-reminder>.

Unlike a canned transcript, this keeps the conversation the way a real harness
does -- each request carries the previous replies exactly as the model returned
them. A harness that invents its own version of the history is a different
case, and the proxy flags it separately as ``history_rewritten``.

Two scenarios:

  --scenario harness       what Claude Code's traffic looks like: a thinking
                           block, a Bash tool_use/tool_result pair and a
                           <system-reminder>. Shows what the proxy strips
                           (reminders, thinking, tool traffic) and what it keeps.

  --scenario agent-writes  an agent that saves memory ITSELF through its own
                           MemoryHub tool. The proxy sees the tool call in the
                           traffic, still stores the turn, and skips its own
                           extraction pass for it
                           (MEMORYHUB_CAPTURE_WHEN_AGENT_WRITES=defer).
                           Two user turns, so extraction is actually due and
                           the deferral is visible in the log.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.request

SYSTEM = "You are Claude Code, running in /Users/dev/project. Today is Thursday."


def call(args, messages: list[dict]) -> str:
    body = json.dumps({
        "model": args.model,
        "max_tokens": 256,
        "system": SYSTEM,
        "messages": messages,
    }).encode()
    req = urllib.request.Request(
        f"{args.base_url}/v1/messages", data=body, headers={
            "content-type": "application/json",
            "x-api-key": args.key,
            "anthropic-version": "2023-06-01",
            "X-MemoryHub-Session": args.session,
        })
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    text = "".join(b.get("text", "") for b in data.get("content", []))
    print("assistant:", text)
    return text


def scenario_harness(args) -> None:
    history: list[dict] = [{"role": "user", "content": [
        # hooks and reminders the harness injects; not something the user said
        {"type": "text", "text": "<system-reminder>Loaded 3 memories from MemoryHub.</system-reminder>"},
        {"type": "text", "text": "I always run the test suite with pytest -q before pushing."},
    ]}]
    reply = call(args, history)

    # the harness replays the real reply, then its own tool round-trip
    history.append({"role": "assistant", "content": [
        {"type": "thinking", "thinking": "the user wants me to check the repo"},
        {"type": "text", "text": reply},
        {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}},
    ]})
    history.append({"role": "user", "content": [
        {"type": "tool_result", "content": [{"type": "text", "text": "12 passed"}]},
    ]})
    history.append({"role": "user",
                    "content": "We decided to keep the release branch frozen until Friday."})
    call(args, history)


def scenario_agent_writes(args) -> None:
    history: list[dict] = [
        {"role": "user", "content": "Remember that our on-call rotation starts every Monday at 09:00 UTC."},
    ]
    reply = call(args, history)
    history.append({"role": "assistant", "content": [
        {"type": "text", "text": reply},
        {"type": "tool_use", "name": "memoryhub", "input": {
            "action": "write",
            "content": "The on-call rotation starts every Monday at 09:00 UTC.",
        }},
    ]})
    history.append({"role": "user", "content": [
        {"type": "tool_result", "content": [{"type": "text", "text":
            '{"memory": {"id": "3f5c9a21-77b4-4e0e-9a1e-6c2d8f0b1e44"}}'}],
        },
    ]})
    # a second user turn, so the proxy's extraction counter is actually due
    history.append({"role": "user",
                    "content": "Also remember that the handover notes go in the team wiki."})
    reply = call(args, history)
    history.append({"role": "assistant", "content": [
        {"type": "text", "text": reply},
        {"type": "tool_use", "name": "memoryhub", "input": {
            "action": "write",
            "content": "Handover notes are kept in the team wiki.",
        }},
    ]})
    history.append({"role": "user", "content": [
        {"type": "tool_result", "content": [{"type": "text", "text":
            '{"memory": {"id": "b81d4f2c-3a55-4f7d-9d1a-52cc7a9e6f10"}}'}],
        },
    ]})
    history.append({"role": "user", "content": "Thanks, that's all for now."})
    call(args, history)


SCENARIOS = {"harness": scenario_harness, "agent-writes": scenario_agent_writes}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL", "http://localhost:4000"))
    ap.add_argument("--key", default=os.environ.get("LITELLM_KEY", "sk-poc-local"))
    ap.add_argument("--model", default=os.environ.get("LLM_MODEL", "poc-model"))
    ap.add_argument("--session", default="poc-harness")
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), default="harness")
    args = ap.parse_args()
    SCENARIOS[args.scenario](args)


if __name__ == "__main__":
    main()
