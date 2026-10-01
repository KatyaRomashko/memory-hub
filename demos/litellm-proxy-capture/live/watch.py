"""Follow the proxy's observation log and print one readable line per LLM call.

Run it in its own terminal next to the live session:

    source live/env.sh && python live/watch.py

Every line is what the proxy could see for one call it intercepted:

    14:02:11  demo-live      (header)  msgs 6  new 2 [user,assistant]  appended 2  thread 1f3c9a2b
              extraction: 2 memories from 1 window, cursor 4, 812 ms

Columns: the session key and where it came from (header / litellm id /
fingerprint), how many messages the request carried, how many of them were new
(the delta -- chat APIs resend the whole history every time), how many the proxy
stored, and whether extraction ran. `skipped` / `extract_skipped` say why not.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path


def path() -> Path:
    p = os.environ.get("MEMORYHUB_CAPTURE_OBSERVATIONS", "out/observations.jsonl")
    return Path(sys.argv[1] if len(sys.argv) > 1 else p)


def render(o: dict) -> str:
    ts = datetime.fromtimestamp(o.get("ts") or time.time()).strftime("%H:%M:%S")
    head = f"{ts}  {o.get('session_key', '?')[:14]:<14} ({o.get('session_source')})"
    if o.get("event") == "extraction":
        e = o.get("extraction") or {}
        tail = f" ERROR {o['error']}" if o.get("error") else ""
        return (f"{ts}  {'':<14}  extraction finished: {e.get('extracted_count')} memories from "
                f"{e.get('windows_processed')} window(s), cursor {e.get('cursor')}, "
                f"{o.get('extract_ms') or 0:.0f} ms"
                + (f", FAILED windows {e['failures']}" if e.get("failures") else "") + tail)
    if o.get("skipped"):
        return f"{head}  SKIPPED: {o['skipped']}"
    roles = ",".join(o.get("new_roles") or [])
    line = (f"{head}  msgs {o.get('total_messages')}  new {o.get('new_messages')} [{roles}]  "
            f"appended {o.get('appended')}  thread {str(o.get('thread_id'))[:8]}")
    extra = []
    if o.get("history_rewritten"):
        extra.append("history rewritten by the client")
    if o.get("agent_writes"):
        extra.append(f"agent wrote {o['agent_writes']} memory/memories itself")
    if o.get("extract_skipped") == "scheduled in background":
        extra.append("extraction scheduled -- runs in the background, line follows")
    elif o.get("extract_skipped"):
        extra.append(f"extraction deferred ({o['extract_skipped']})")
    if o.get("error"):
        extra.append(f"ERROR {o['error']}")
    if extra:
        line += "\n" + " " * 10 + "; ".join(extra)
    e = o.get("extraction")
    if e:
        line += ("\n" + " " * 10 +
                 f"extraction: {e.get('extracted_count')} memories from "
                 f"{e.get('windows_processed')} window(s), cursor {e.get('cursor')}, "
                 f"{o.get('extract_ms') or 0:.0f} ms"
                 + (f", FAILED windows {e['failures']}" if e.get("failures") else ""))
    return line


def main() -> None:
    p = path()
    print(f"watching {p}  (Ctrl-C to stop)\n")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch(exist_ok=True)
    with p.open("r", encoding="utf-8") as fh:
        fh.seek(0, os.SEEK_END)
        while True:
            line = fh.readline()
            if not line:
                time.sleep(0.3)
                continue
            line = line.strip()
            if not line:
                continue
            try:
                print(render(json.loads(line)), flush=True)
            except json.JSONDecodeError:
                print(line, flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
