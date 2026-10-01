"""An agent with the MemoryHub MCP server attached, talking through the gateway.

This is the hybrid, with both halves real at the same time:

* the **gateway** captures the conversation — unconditionally, because the agent
  goes through it;
* the **model** reads and writes memories through MCP — electively, because it
  decides when it needs to.

Run it and the proxy sees `tool_call` / `tool_result` blocks in the traffic. It
does two things with them: it keeps them out of the stored thread (only the
conversation is stored), and it uses them to notice that the agent wrote a
memory itself, which is what `MEMORYHUB_CAPTURE_WHEN_AGENT_WRITES=defer` acts on.

The MCP server is the personal edition's own (`python -m memoryhub_local`, stdio)
pointed at the same SQLite database the gateway writes to, so what the model
reads is exactly what the gateway captured.

    source live/env.sh
    python live/mcp_agent.py --interactive --session demo-mcp
    python live/mcp_agent.py --scenario recall --session demo-mcp

Only the `memory` tool is exposed. The `thread` tool is deliberately withheld:
writing the transcript is the gateway's job, and handing it to the agent as well
would mean two writers for one thread.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import OpenAI

SYSTEM_PROMPT = (
    "You are a concise assistant with a persistent memory tool. Answer in at most "
    "three sentences.\n"
    "Before answering a question about the user's project, decisions, preferences or "
    "past conversations, call `memory` with action='search' and use what comes back.\n"
    "When the user states a durable fact, decision or preference, call `memory` with "
    "action='write' to store it.\n"
    "Do not mention the tool in your reply; just use it."
)

SCENARIOS = {
    # assumes Act 3 already captured the Nexus / Go / Friday conversation
    "recall": [
        "What did we decide about the backend language?",
        "Remind me what the API has to stay compatible with.",
    ],
    "write": [
        "New decision, please remember it: the on-call rotation starts every Monday at 09:00 UTC.",
        "And the handover notes go in the team wiki.",
    ],
}


def to_openai_tools(mcp_tools) -> list[dict]:
    """Expose the MCP tools to the model in OpenAI function-calling shape."""
    out = []
    for t in mcp_tools:
        if t.name != "memory":        # see module docstring
            continue
        out.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": (t.description or "")[:1024],
                "parameters": t.inputSchema or {"type": "object", "properties": {}},
            },
        })
    return out


def result_text(result) -> str:
    parts = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts) or "(no content)"


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL", "http://localhost:4000"))
    p.add_argument("--api-key", default=os.environ.get("LITELLM_KEY", "sk-poc-local"))
    p.add_argument("--model", default=os.environ.get("LLM_MODEL", "poc-model"))
    p.add_argument("--session", default="demo-mcp")
    p.add_argument("--actor")
    p.add_argument("--scenario", choices=sorted(SCENARIOS))
    p.add_argument("--interactive", action="store_true")
    p.add_argument("--max-tool-rounds", type=int, default=4)
    args = p.parse_args()

    headers = {"X-MemoryHub-Session": args.session}
    if args.actor:
        headers["X-MemoryHub-Actor"] = args.actor
    client = OpenAI(base_url=f"{args.base_url}/v1", api_key=args.api_key,
                    default_headers=headers)

    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "memoryhub_local"],
        env={**os.environ},          # XDG_DATA_HOME points at the demo database
    )

    async with AsyncExitStack() as stack:
        read, write = await stack.enter_async_context(stdio_client(server))
        mcp = await stack.enter_async_context(ClientSession(read, write))
        await mcp.initialize()
        listed = await mcp.list_tools()
        tools = to_openai_tools(listed.tools)
        print(f"\nMCP server ready — tools exposed to the model: "
              f"{[t['function']['name'] for t in tools]}")
        print(f"gateway: {args.base_url}   session: {args.session}\n")

        messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

        async def turn(text: str) -> None:
            messages.append({"role": "user", "content": text})
            for _ in range(args.max_tool_rounds):
                reply = client.chat.completions.create(
                    model=args.model, messages=messages, tools=tools, tool_choice="auto",
                ).choices[0].message
                messages.append(reply.model_dump(exclude_none=True))
                calls = reply.tool_calls or []
                if not calls:
                    print(f"agent> {reply.content}\n")
                    return
                for call in calls:
                    name = call.function.name
                    try:
                        arguments = json.loads(call.function.arguments or "{}")
                    except json.JSONDecodeError:
                        arguments = {}
                    print(f"   [mcp] {name}({json.dumps(arguments)[:120]})")
                    try:
                        result = await mcp.call_tool(name, arguments)
                        content = result_text(result)
                    except Exception as exc:                 # never kill the demo
                        content = f"tool error: {type(exc).__name__}: {exc}"
                    print(f"   [mcp] -> {content[:160]}")
                    messages.append({"role": "tool", "tool_call_id": call.id,
                                     "content": content[:4000]})
            print("agent> (gave up after the tool-call limit)\n")

        if args.scenario:
            for line in SCENARIOS[args.scenario]:
                print(f"user> {line}")
                await turn(line)
        if args.interactive or not args.scenario:
            print("type a message, Ctrl-D to exit\n")
            while True:
                try:
                    line = input("user> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if line:
                    await turn(line)


if __name__ == "__main__":
    asyncio.run(main())
