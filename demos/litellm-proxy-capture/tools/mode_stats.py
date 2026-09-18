"""Collect the numbers for one comparison mode into a JSON line.

Usage: MODE=A XDG_DATA_HOME=... MEMORYHUB_CAPTURE_OBSERVATIONS=... \
       python tools/mode_stats.py <label> <description> >> out/compare.jsonl
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Facts the scenarios put into the conversation; recall is measured against these.
WANTED = {
    "dark mode": ("dark mode",),
    "python": ("python",),
    "openshift 4.19": ("4.19", "openshift"),
    "eu-west-1": ("eu-west-1",),
}
SMALLTALK = ("egg", "boil")


async def main() -> None:
    from sqlalchemy import select

    from _local import open_backend
    from memoryhub_local.identity import TENANT_ID
    from memoryhub_local.models.conversation import ConversationExtraction, ConversationThread
    from memoryhub_local.models.memory import MemoryNode
    from memoryhub_local.services.memory import search_memories

    label, description = sys.argv[1], sys.argv[2]
    state, embed_kind = await open_backend(announce=False)

    obs_path = Path(os.environ.get("MEMORYHUB_CAPTURE_OBSERVATIONS", "out/observations.jsonl"))
    obs = [json.loads(x) for x in obs_path.read_text().splitlines() if x.strip()] if obs_path.exists() else []
    captured = [o for o in obs if not o.get("skipped")]

    explicit_stats = {}
    stats_path = Path(os.environ.get("EXPLICIT_STATS", ""))
    if stats_path.name and stats_path.exists():
        explicit_stats = json.loads(stats_path.read_text())

    async with state.session_factory() as db:
        nodes = (await db.execute(select(MemoryNode).where(
            MemoryNode.tenant_id == TENANT_ID,
            MemoryNode.is_current.is_(True),
            MemoryNode.status == "active",
        ))).scalars().all()
        contents = [n.content for n in nodes]
        low = [c.lower() for c in contents]

        recall = {k: any(all(t in c for t in terms) for c in low) for k, terms in WANTED.items()}
        false_positives = [c for c in contents if any(t in c.lower() for t in SMALLTALK)]
        dup_pairs = sum(1 for i, a in enumerate(low) for b in low[i + 1:] if a == b)
        near_dupes = [c for c in contents if "us-east-1" in c.lower()] and \
                     [c for c in contents if "eu-west-1" in c.lower()]

        threads = [t for t in (await db.execute(select(ConversationThread).where(
            ConversationThread.tenant_id == TENANT_ID))).scalars().all()
            if (t.metadata_ or {}).get("source") == "litellm-proxy"]
        extracted = (await db.execute(select(ConversationExtraction).where(
            ConversationExtraction.tenant_id == TENANT_ID))).scalars().all()

        top = await search_memories(db, "dark mode", state.embedding_service, state.recall_backend, max_results=3)
        top_hits = [r.get("content", "")[:60] for r in (top.get("results") or [])]

    ext_calls = [o for o in captured if o.get("extraction")]
    row = {
        "mode": label,
        "description": description,
        "embeddings": embed_kind,
        "memories": len(nodes),
        "recall": recall,
        "recall_score": f"{sum(recall.values())}/{len(recall)}",
        "false_positives": len(false_positives),
        "exact_duplicates": dup_pairs,
        "correction_kept_both": bool(near_dupes),
        "threads": len(threads),
        "provenance_links": len(extracted),
        "agent_calls": len(captured) or explicit_stats.get("llm_calls", 0),
        "agent_prompt_tokens": sum(o.get("prompt_tokens") or 0 for o in captured)
        or explicit_stats.get("prompt_tokens", 0),
        "proxy_extractions": len(ext_calls),
        "extract_ms_avg": round(sum(o["extract_ms"] for o in ext_calls) / len(ext_calls)) if ext_calls else None,
        "explicit_fallbacks": explicit_stats.get("fallbacks"),
        "top_hits_dark_mode": top_hits,
    }
    print(json.dumps(row))


if __name__ == "__main__":
    asyncio.run(main())
