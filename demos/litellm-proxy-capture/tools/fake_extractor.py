"""A deterministic stand-in for the extraction LLM (offline runs only).

The personal edition calls an OpenAI-compatible ``/v1/chat/completions``
endpoint for extraction. On a machine with no model and no network there is
nothing to call, which blocks the whole PoC checklist. This server speaks just
enough of that API to keep the real pipeline running: MemoryHub still does the
windowing, reconciliation, provenance and cursor work — only the "which facts
are in this window" judgement is replaced by rules.

It is a test double, never part of a real demo:

    python tools/fake_extractor.py --port 4100
    MEMORYHUB_CAPTURE_EXTRACT_MODEL_URL=http://localhost:4100/v1

For a real local run point that variable at Ollama
(``http://localhost:11434/v1``) or any hosted OpenAI-compatible endpoint.
"""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, HTTPServer

# A line of a formatted window: "[USER] (seq=3): text"
LINE_RE = re.compile(r"^\[(?P<role>[A-Z_]+)\]\s*\(seq=(?P<seq>\d+)\):\s*(?P<text>.*)$")

# Phrases that mark a sentence as worth remembering. Deliberately crude — the
# point is determinism, not extraction quality.
KEEP = (
    "i prefer", "i always", "i use", "i live", "my ", "we decided", "decided",
    "moves to", "target is", "correction", "remember",
)
DROP = ("thanks", "thank you", "hi!", "hello", "good way to", "can you suggest")


def facts_from_window(window: str) -> list[dict]:
    facts: list[dict] = []
    for raw_line in window.splitlines():
        m = LINE_RE.match(raw_line.strip())
        if not m or m.group("role") != "USER":
            continue
        text = m.group("text").strip()
        low = text.lower()
        if any(d in low for d in DROP) and not any(k in low for k in KEEP):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            s = sentence.strip()
            low_s = s.lower()
            if len(s) < 8 or not any(k in low_s for k in KEEP):
                continue
            s = re.sub(r"^(sorry,\s*correction:\s*|quick context about me:\s*)", "", s, flags=re.I)
            facts.append({"content": s.rstrip("."), "weight": 0.8, "domains": []})
    return facts


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        length = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        window = ""
        for msg in body.get("messages", []):
            if msg.get("role") == "user":
                window = msg.get("content", "")
        payload = {"extractions": facts_from_window(window)}
        response = {
            "id": "fake-extraction",
            "object": "chat.completion",
            "model": body.get("model", "fake-extractor"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(payload)},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": len(window.split()), "completion_tokens": 0, "total_tokens": 0},
        }
        data = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:  # keep the PoC output readable
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=4100)
    args = ap.parse_args()
    print(f"fake extraction LLM on http://localhost:{args.port}/v1 (offline test double)")
    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
