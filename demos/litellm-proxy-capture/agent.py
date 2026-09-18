"""A deliberately memory-unaware agent for the proxy-capture PoC.

It has no MemoryHub MCP tool, no memory instructions and no hooks. The only
thing pointing at MemoryHub is that ``--base-url`` goes to the LiteLLM proxy.

    python agent.py --scenario preferences          # scripted demo
    python agent.py --interactive                   # chat REPL
    python agent.py --scenario preferences --session demo-1   # explicit session header
"""

from __future__ import annotations

import argparse
import os
import uuid

from openai import OpenAI

SCENARIOS: dict[str, list[str]] = {
    "preferences": [
        "Hi! Quick context about me: I prefer dark mode everywhere and I always use Python for scripting.",
        "We decided today that the team's staging cluster moves to OpenShift 4.19 next Monday.",
        "Can you suggest a name for a small CLI that cleans up old container images?",
    ],
    "preferences-followup": [
        "One more thing: I always deploy from the main branch.",
    ],
    "correction": [
        "Our deployment target is us-east-1.",
        "Sorry, correction: the deployment target is eu-west-1, not us-east-1.",
    ],
    "smalltalk": [
        "What's a good way to boil an egg?",
        "Thanks!",
    ],
}

SYSTEM_PROMPT = "You are a concise, helpful assistant."


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL", "http://localhost:4000"))
    p.add_argument("--api-key", default=os.environ.get("LITELLM_KEY", "sk-poc-local"))
    p.add_argument("--model", default=os.environ.get("LLM_MODEL", "poc-model"))
    p.add_argument("--scenario", choices=sorted(SCENARIOS), default="preferences")
    p.add_argument("--interactive", action="store_true")
    p.add_argument("--stream", action="store_true", help="use streaming responses")
    p.add_argument("--session", help="send X-MemoryHub-Session (omit to test fingerprinting)")
    p.add_argument("--actor", help="send X-MemoryHub-Actor")
    p.add_argument("--user", help="send the OpenAI `user` field (LiteLLM end_user)")
    args = p.parse_args()

    headers = {}
    if args.session:
        headers["X-MemoryHub-Session"] = args.session if args.session != "auto" else str(uuid.uuid4())
    if args.actor:
        headers["X-MemoryHub-Actor"] = args.actor
    client = OpenAI(base_url=args.base_url, api_key=args.api_key, default_headers=headers or None)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    def turn(text: str) -> None:
        messages.append({"role": "user", "content": text})
        extra = {"user": args.user} if args.user else {}
        if args.stream:
            chunks = client.chat.completions.create(model=args.model, messages=messages, stream=True, **extra)
            reply = "".join((c.choices[0].delta.content or "") for c in chunks if c.choices)
        else:
            resp = client.chat.completions.create(model=args.model, messages=messages, **extra)
            reply = resp.choices[0].message.content or ""
        messages.append({"role": "assistant", "content": reply})
        print(f"\nuser> {text}\nagent> {reply}")

    if args.interactive:
        while True:
            try:
                text = input("\nuser> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if text:
                turn(text)
    else:
        for text in SCENARIOS[args.scenario]:
            turn(text)


if __name__ == "__main__":
    main()
