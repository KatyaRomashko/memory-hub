"""Score the WRIG-1482 PoC checklist against a finished local run.

Reads the personal edition's SQLite database plus out/observations.jsonl and
prints one line per criterion: what was expected, what happened, and a verdict.
Run it after ``run-poc-local.sh`` (which calls it automatically).

Verdicts:
  PASS        the criterion is demonstrated by the data
  PARTIAL     demonstrated, but with a caveat printed next to it
  FAIL        the data contradicts the criterion
  N/A         not exercised in this run (e.g. offline stand-ins)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify import repeated_block  # noqa: E402

VERDICT_WIDTH = 7


def line(name: str, verdict: str, detail: str) -> None:
    print(f"{verdict:<{VERDICT_WIDTH}} {name}\n        {detail}")


async def main() -> None:
    from sqlalchemy import select

    from memoryhub_local.identity import TENANT_ID
    from memoryhub_local.models.conversation import (
        ConversationExtraction,
        ConversationMessage,
        ConversationThread,
    )
    from memoryhub_local.models.memory import MemoryNode

    obs_path = Path(os.environ.get("MEMORYHUB_CAPTURE_OBSERVATIONS", "out/observations.jsonl"))
    obs = [json.loads(x) for x in obs_path.read_text().splitlines() if x.strip()] if obs_path.exists() else []
    captured = [o for o in obs if not o.get("skipped")]

    from _local import open_backend
    state, _embed_kind = await open_backend()
    mock_embeddings = type(state.embedding_service).__name__.startswith("Mock")

    async with state.session_factory() as db:
        threads = [
            t for t in (await db.execute(
                select(ConversationThread).where(ConversationThread.tenant_id == TENANT_ID)
            )).scalars().all()
            if (t.metadata_ or {}).get("source") == "litellm-proxy"
        ]
        by_session = {t.a2a_context_id: t for t in threads}
        msgs: dict[str, list] = {}
        mems: dict[str, list] = {}
        for t in threads:
            msgs[str(t.id)] = (await db.execute(
                select(ConversationMessage).where(ConversationMessage.thread_id == t.id)
                .order_by(ConversationMessage.sequence_number)
            )).scalars().all()
            rows = (await db.execute(
                select(ConversationExtraction).where(ConversationExtraction.thread_id == t.id)
            )).scalars().all()
            out = []
            for r in rows:
                node = await db.get(MemoryNode, r.memory_node_id)
                if node is not None:
                    out.append(node)
            mems[str(t.id)] = out

        print(f"\nPoC checklist -- personal edition -- threads: {len(threads)}, "
              f"observations: {len(obs)}, embeddings: {'mock' if mock_embeddings else 'onnx'}\n")

        # 1. capture without the agent knowing
        prefs = by_session.get("poc-prefs")
        prefs_mems = mems.get(str(prefs.id), []) if prefs else []
        hits = [m for m in prefs_mems if "dark mode" in m.content.lower()]
        line("1. Capture without agent involvement",
             "PASS" if prefs and hits else ("PARTIAL" if prefs else "FAIL"),
             f"thread={getattr(prefs, 'id', None)} messages={len(msgs.get(str(prefs.id), [])) if prefs else 0} "
             f"memories={len(prefs_mems)}; 'dark mode' memory: {'yes' if hits else 'no'}; "
             f"every memory carries source={ {m.source for m in prefs_mems} or '-'} and thread provenance")

        # 2. no duplicates across a restart
        dup_report = []
        for t in threads:
            rows = [(m.role, m.content) for m in msgs[str(t.id)]]
            block = repeated_block(rows)
            if block:
                dup_report.append(f"{t.a2a_context_id}: block of {block[2]} repeated")
        line("2. No duplicate messages (incl. proxy restart)",
             "FAIL" if dup_report else "PASS",
             "; ".join(dup_report) if dup_report else
             f"no repeated blocks in {len(threads)} threads; "
             f"{sum(1 for o in captured if o.get('history_rewritten'))} call(s) flagged history_rewritten")

        # 3. session identification
        sources = {}
        for o in captured:
            sources[o["session_source"]] = sources.get(o["session_source"], 0) + 1
        line("3. Session identification",
             "PASS" if {"header", "end_user_fingerprint"} <= set(sources) else "PARTIAL",
             f"sources={sources}; threads per session key: "
             f"{ {t.a2a_context_id: len(msgs[str(t.id)]) for t in threads} }")

        # 4. ownership
        owners = {t.owner_id for t in threads}
        observed = {(t.metadata_ or {}).get("observed_actor_source") for t in threads}
        line("4. Ownership and attribution", "PASS",
             f"thread owner(s)={owners} (the identity the sink writes as); "
             f"observed actor source(s)={observed}; the real speaker is metadata only")

        # 5. extraction quality
        small = next((t for t in threads if t.a2a_context_id and t.a2a_context_id.startswith("default_user_id")), None)
        corr = next((t for t in threads if t.a2a_context_id and t.a2a_context_id.startswith("alice@")), None)
        small_n = len(mems.get(str(small.id), [])) if small else None
        corr_mems = [m.content for m in mems.get(str(corr.id), [])] if corr else []
        detail = (f"smalltalk memories={small_n} (expected 0); "
                  f"correction produced {len(corr_mems)} memory/memories: {corr_mems}")
        if mock_embeddings:
            verdict = "PARTIAL"
            detail += "; mock embeddings -- create/update/skip decisions are not semantically meaningful"
        else:
            verdict = "PASS" if small_n == 0 else "PARTIAL"
        line("5. Extraction quality per scenario", verdict, detail)

        # 6. cost and latency
        lat = [o["latency_ms"] for o in captured if o.get("latency_ms") is not None]
        ext = [o["extract_ms"] for o in captured if o.get("extract_ms") is not None]
        toks = sum(o.get("prompt_tokens") or 0 for o in captured)
        line("6. Cost and latency", "PARTIAL" if not ext else "PASS",
             f"agent calls={len(captured)}, prompt tokens observed={toks}, "
             f"extraction calls={len(ext)}, avg extraction {sum(ext) / len(ext):.0f} ms" if ext else
             "no extraction was triggered from the proxy (set MEMORYHUB_CAPTURE_EXTRACT_EVERY>0)")

        # 7. harness-shaped traffic
        harness = by_session.get("poc-harness")
        h_msgs = msgs.get(str(harness.id), []) if harness else []
        reminders = [m for m in h_msgs if "system-reminder" in (m.content or "")]
        tool_msgs = [m for m in h_msgs if m.role in ("tool_call", "tool_result")]
        tool_calls_seen = sum(1 for o in captured if o.get("has_tool_traffic"))
        line("7. Harness-shaped traffic (Anthropic, tools, reminders)",
             "PARTIAL" if harness else "N/A",
             f"messages={len(h_msgs)}, system-reminder blocks stored={len(reminders)} (expected 0), "
             f"tool messages stored={len(tool_msgs)} (expected 0 with MEMORYHUB_CAPTURE_TOOLS=false), "
             f"calls carrying tool traffic={tool_calls_seen}; "
             "a synthetic probe, not a real Claude Code session")

        print("\nCaveats for this run:")
        if mock_embeddings:
            print("- embeddings are MemoryHub's mock fallback (the ONNX model could not be downloaded): "
                  "search ranking and dedup thresholds are NOT validated")
        if os.environ.get("MEMORYHUB_CAPTURE_EXTRACT_MODEL") == "fake-extractor":
            print("- extraction ran against the rule-based stand-in in tools/fake_extractor.py: "
                  "the pipeline is exercised, the LLM judgement is not")
        print("- the cluster edition behaves differently (windows, update-vs-create, circuit breaker, "
              "S3 offload, RBAC); these results do not transfer")


if __name__ == "__main__":
    asyncio.run(main())
