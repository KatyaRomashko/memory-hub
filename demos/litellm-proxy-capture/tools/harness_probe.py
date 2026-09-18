"""Send harness-shaped traffic through the proxy: Anthropic /v1/messages with
tool blocks and an injected <system-reminder>.

Claude Code talks to the Anthropic Messages API, wraps hook output and
reminders in <system-reminder> tags, and carries tool_use / tool_result
content blocks. This probe reproduces that shape without needing an
Anthropic key, so the capture path can be checked offline.
"""

from __future__ import annotations

import argparse
import json
import urllib.request

TURNS = [
    [{"role": "user", "content": [
        {"type": "text", "text": "<system-reminder>Loaded 3 memories from MemoryHub.</system-reminder>"},
        {"type": "text", "text": "I always run the test suite with pytest -q before pushing."},
    ]}],
    [{"role": "user", "content": "I always run the test suite with pytest -q before pushing."},
     {"role": "assistant", "content": [
         {"type": "thinking", "thinking": "the user wants me to check the repo"},
         {"type": "text", "text": "Noted."},
         {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}},
     ]},
     {"role": "user", "content": [{"type": "tool_result", "content": [{"type": "text", "text": "12 passed"}]}]},
     {"role": "user", "content": "We decided to keep the release branch frozen until Friday."}],
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:4010")
    ap.add_argument("--key", default="sk-poc-local")
    ap.add_argument("--model", default="claude-mock")
    ap.add_argument("--session", default="poc-harness")
    args = ap.parse_args()

    for messages in TURNS:
        body = json.dumps({
            "model": args.model,
            "max_tokens": 256,
            "system": "You are Claude Code, running in /Users/dev/project. Today is Thursday.",
            "messages": messages,
        }).encode()
        req = urllib.request.Request(
            f"{args.base_url}/v1/messages", data=body, headers={
                "content-type": "application/json",
                "x-api-key": args.key,
                "anthropic-version": "2023-06-01",
                "X-MemoryHub-Session": args.session,
            })
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        print("assistant:", "".join(b.get("text", "") for b in data.get("content", [])))


if __name__ == "__main__":
    main()
