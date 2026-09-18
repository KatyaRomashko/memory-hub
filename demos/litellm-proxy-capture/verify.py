"""Check what the proxy captured, trigger extraction, and search for the memories.

    python verify.py report                  # summarize out/observations.jsonl (no MemoryHub needed)
    python verify.py threads                 # list proxy-captured threads with cursor / message count
    python verify.py extract [THREAD_ID]     # run MemoryHub extraction (all proxy threads if omitted)
    python verify.py reextract THREAD_ID     # re-run extraction over ALL messages (ignores the cursor)
    python verify.py messages THREAD_ID      # dump a thread and flag duplicate messages
    python verify.py search "dark mode"      # search memories (source=dreaming only with --dreaming)

Works against both editions. With ``MEMORYHUB_CAPTURE_SINK=local`` it reads the
personal edition's SQLite database directly; otherwise it needs MEMORYHUB_URL +
MEMORYHUB_API_KEY (or OAuth vars). `report` needs neither.

Reading extraction results:
  extracted_count=0, failures>0  -> the LLM call failed (key, model, params); the
                                    cursor still moved, use `reextract`.
  extracted_count=0, failures=0  -> nothing new after the cursor, or the LLM found
                                    nothing worth keeping.
  circuit_breaker_tripped        -> run stopped early; call `extract` again.
"""

from __future__ import annotations

import asyncio
import collections
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))


def report(path: str) -> None:
    p = Path(path)
    if not p.exists():
        print(f"no observations file at {p}")
        return
    rows = [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    if not rows:
        print("no observations")
        return
    captured = [r for r in rows if not r.get("skipped")]
    print(f"calls observed: {len(rows)}   captured: {len(captured)}   skipped: {len(rows) - len(captured)}")
    print("skip reasons:", dict(collections.Counter(r["skipped"] for r in rows if r.get("skipped"))))
    print("session source:", dict(collections.Counter(r["session_source"] for r in rows)))
    print("actor source:", dict(collections.Counter(r.get("actor_source") or "none" for r in rows)))
    print("sessions:", len({r["session_key"] for r in captured}))
    print("history rewrites:", sum(bool(r["history_rewritten"]) for r in captured))
    print("streaming calls:", sum(bool(r.get("stream")) for r in rows))
    print("calls with tool traffic:", sum(bool(r["has_tool_traffic"]) for r in rows))
    print("messages appended:", sum(r["appended"] for r in captured))
    errors = [r for r in rows if r.get("error")]
    print("errors:", len(errors))
    for msg, n in collections.Counter(r["error"] for r in errors).most_common(5):
        print(f"   {n} x {msg[:120]}")
    ext = [r["extraction"] for r in captured if r.get("extraction")]
    if ext:
        print(
            "extractions:", len(ext),
            " memories:", sum(e.get("extracted_count", 0) for e in ext),
            " failed windows:", sum(e.get("failures", 0) or 0 for e in ext),
            " circuit breaker:", sum(bool(e.get("circuit_breaker_tripped")) for e in ext),
        )
    print("prompt tokens observed (sum):", sum(r.get("prompt_tokens") or 0 for r in captured))
    lat = sorted(r["latency_ms"] for r in captured if r.get("latency_ms") is not None)
    if lat:
        print(f"LLM latency ms: p50={lat[len(lat) // 2]:.0f} max={lat[-1]:.0f}")
    ext_ms = [r["extract_ms"] for r in captured if r.get("extract_ms") is not None]
    if ext_ms:
        print(f"extraction call ms: avg={sum(ext_ms) / len(ext_ms):.0f} max={max(ext_ms):.0f}")
    print("note: with the cluster edition the extraction LLM runs inside the MCP pod, "
          "so its tokens are not visible here")


def repeated_block(seq: list[tuple]) -> tuple[int, int, int] | None:
    """Find a contiguous block of >= 2 messages that appears twice.

    A single repeated line is normal (an assistant may answer "Got it" twice);
    a repeated *block* is the signature of history being appended again.
    Returns (start_of_first, start_of_second, length) or None.
    """
    n = len(seq)
    best = None
    for i in range(n):
        for j in range(i + 1, n):
            k = 0
            while j + k < n and seq[i + k] == seq[j + k] and i + k < j:
                k += 1
            if k >= 2 and (best is None or k > best[2]):
                best = (i, j, k)
    return best


def _print_messages(rows: list[tuple[int, str, str]], header: str) -> None:
    print(header)
    for seq, role, content in rows:
        print(f"{seq:>4} {role:<11} {(content or '[no inline content]')[:90]}")
    block = repeated_block([(r, c) for _, r, c in rows])
    if block:
        i, j, k = block
        print(f"! repeated block of {k} messages: seq {rows[i][0]}.. appears again at seq {rows[j][0]}..")
    else:
        print("no repeated message block (history was not appended twice)")
    print(f"messages: {len(rows)}")


def _print_extraction(res, elapsed: float) -> None:
    data = res.model_dump()
    print(f"   extraction ({elapsed:.1f}s):", data)
    if data.get("failures"):
        print("   ! failed windows: the cursor moved past them. Check the MCP pod logs "
              "(`Extraction failed for thread`), fix the cause, then run `reextract`.")
    if data.get("circuit_breaker_tripped"):
        print("   ! circuit breaker stopped the run early; call `extract` again for the rest.")


# ── personal edition (memoryhub-local, SQLite) ───────────────────────────


async def local_main(cmd: str) -> None:
    from memoryhub_local.identity import TENANT_ID, get_owner_id
    from memoryhub_local.models.conversation import ConversationExtraction, ConversationMessage, ConversationThread
    from memoryhub_local.models.memory import MemoryNode
    from memoryhub_local.services.extraction import extract_from_thread, make_http_llm_fn
    from memoryhub_local.services.memory import search_memories
    from sqlalchemy import select

    model = os.environ.get("MEMORYHUB_CAPTURE_EXTRACT_MODEL") or "local-extractor"
    model_url = os.environ.get("MEMORYHUB_CAPTURE_EXTRACT_MODEL_URL")
    api_key = os.environ.get("MEMORYHUB_CAPTURE_EXTRACT_API_KEY") or None

    from _local import open_backend
    state, _embed_kind = await open_backend()

    async def proxy_threads(db):
        rows = (await db.execute(
            select(ConversationThread).where(
                ConversationThread.tenant_id == TENANT_ID,
                ConversationThread.status == "active",
            ).order_by(ConversationThread.created_at)
        )).scalars().all()
        return [t for t in rows if (t.metadata_ or {}).get("source") == "litellm-proxy"]

    async def run_extract(db, thread_id: str, *, whole_thread: bool = False) -> None:
        if not model_url:
            print("set MEMORYHUB_CAPTURE_EXTRACT_MODEL_URL to an OpenAI-compatible endpoint")
            return
        if whole_thread:
            # the local edition has no turn_range: rewind the cursor instead
            thread = await db.get(ConversationThread, uuid.UUID(thread_id))
            print(f"rewinding cursor {thread.extraction_cursor} -> 0")
            thread.extraction_cursor = 0
            await db.commit()
        t0 = time.monotonic()
        res = await extract_from_thread(
            db, thread_id,
            llm_fn=make_http_llm_fn(model, model_url, api_key),
            embedding_service=state.embedding_service,
            recall_backend=state.recall_backend,
            extraction_model=model,
        )
        print(f"   extraction ({time.monotonic() - t0:.1f}s): {res}")
        if res.get("failures"):
            print("   ! failed windows: the cursor moved past them; re-run with `reextract`.")

    async with state.session_factory() as db:
        if cmd in ("threads", "extract"):
            threads = await proxy_threads(db)
            target = sys.argv[2] if len(sys.argv) > 2 else None
            if not threads:
                print("no proxy-captured threads in the local database")
            for t in threads:
                if target and str(t.id) != target:
                    continue
                count = (await db.execute(
                    select(ConversationMessage).where(ConversationMessage.thread_id == t.id)
                )).scalars().all()
                mems = (await db.execute(
                    select(ConversationExtraction).where(ConversationExtraction.thread_id == t.id)
                )).scalars().all()
                meta = t.metadata_ or {}
                print(f"{t.id}  session={t.a2a_context_id}  owner={t.owner_id}  memories={len(mems)}  "
                      f"observed_actor={meta.get('observed_actor_id')} ({meta.get('observed_actor_source')})  "
                      f"messages={len(count)}  cursor={t.extraction_cursor}")
                if cmd == "extract":
                    await run_extract(db, str(t.id))
        elif cmd == "reextract":
            if len(sys.argv) < 3:
                print("usage: verify.py reextract THREAD_ID")
                return
            await run_extract(db, sys.argv[2], whole_thread=True)
        elif cmd == "messages":
            if len(sys.argv) < 3:
                print("usage: verify.py messages THREAD_ID")
                return
            thread = await db.get(ConversationThread, uuid.UUID(sys.argv[2]))
            msgs = (await db.execute(
                select(ConversationMessage)
                .where(ConversationMessage.thread_id == thread.id)
                .order_by(ConversationMessage.sequence_number)
            )).scalars().all()
            rows = [(m.sequence_number, m.role, m.content or "") for m in msgs]
            _print_messages(rows, f"thread {thread.id}  owner={thread.owner_id}  "
                                  f"cursor={thread.extraction_cursor}")
        elif cmd == "search":
            query = " ".join(a for a in sys.argv[2:] if a != "--dreaming") or "preferences"
            res = await search_memories(db, query, state.embedding_service, state.recall_backend)
            rows = res.get("results") or []
            print(f"query {query!r}: {len(rows)} result(s), owner={get_owner_id()}")
            for r in rows:
                node = await db.get(MemoryNode, uuid.UUID(r["id"])) if "id" in r else None
                prov = (await db.execute(
                    select(ConversationExtraction).where(ConversationExtraction.memory_node_id == uuid.UUID(r["id"]))
                )).scalars().first() if node else None
                src = getattr(node, "source", None)
                thread_ref = f"thread={prov.thread_id} msgs={prov.source_messages}" if prov else "no thread provenance"
                print(f"- [{src}] {thread_ref}\n  {str(r.get('content'))[:160]}")
        else:
            print(__doc__)


async def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "report":
        report(sys.argv[2] if len(sys.argv) > 2 else os.environ.get("MEMORYHUB_CAPTURE_OBSERVATIONS", "out/observations.jsonl"))
        return

    if os.environ.get("MEMORYHUB_CAPTURE_SINK", "").lower() == "local":
        await local_main(cmd)
        return

    from memoryhub import MemoryHubClient

    scope = os.environ.get("MEMORYHUB_CAPTURE_SCOPE", "user")
    model = os.environ.get("MEMORYHUB_CAPTURE_EXTRACT_MODEL") or None
    model_url = os.environ.get("MEMORYHUB_CAPTURE_EXTRACT_MODEL_URL") or None

    async with MemoryHubClient.from_env(auto_discover_config=False) as client:
        if cmd in ("threads", "extract"):
            listing = await client.list_threads(scope=scope, limit=100)
            threads = [t for t in listing.threads if (t.metadata or {}).get("source") == "litellm-proxy"]
            if cmd == "extract" and len(sys.argv) > 2:
                threads = [t for t in listing.threads if t.id == sys.argv[2]]
            if not threads:
                print("no proxy-captured threads found in scope", scope)
            for t in threads:
                expires = f"{t.expires_at:%Y-%m-%d}" if t.expires_at else "-"
                print(f"{t.id}  session={getattr(t, 'a2a_context_id', None)}  owner={t.owner_id}  "
                      f"observed_actor={(t.metadata or {}).get('observed_actor_id')}  "
                      f"cursor={t.extraction_cursor}  expires={expires}")
                if cmd == "extract":
                    t0 = time.monotonic()
                    res = await client.extract_thread(t.id, model=model, model_url=model_url)
                    _print_extraction(res, time.monotonic() - t0)
        elif cmd == "reextract":
            if len(sys.argv) < 3:
                print("usage: verify.py reextract THREAD_ID")
                return
            got = await client.get_thread(sys.argv[2], limit=5000)
            seqs = [m.sequence_number for m in (got.messages or [])]
            if not seqs:
                print("thread has no messages")
                return
            print(f"messages {min(seqs)}..{max(seqs)}, cursor={got.thread.extraction_cursor}")
            t0 = time.monotonic()
            res = await client.extract_thread(
                sys.argv[2], turn_range=(min(seqs), max(seqs)), model=model, model_url=model_url)
            _print_extraction(res, time.monotonic() - t0)
        elif cmd == "messages":
            if len(sys.argv) < 3:
                print("usage: verify.py messages THREAD_ID")
                return
            got = await client.get_thread(sys.argv[2], limit=5000)
            rows = [(m.sequence_number, m.role, m.content or "") for m in (got.messages or [])]
            _print_messages(rows, f"thread {got.thread.id}  owner={got.thread.owner_id}  "
                                  f"cursor={got.thread.extraction_cursor}")
        elif cmd == "search":
            args = [a for a in sys.argv[2:] if a != "--dreaming"]
            query = " ".join(args) or "preferences"
            kwargs = {"source": "dreaming"} if "--dreaming" in sys.argv else {}
            try:
                results = await client.search(query, **kwargs)
            except TypeError:  # older SDK without source filter
                results = await client.search(query)
            data = results.model_dump(mode="json") if hasattr(results, "model_dump") else results
            for r in (data.get("results") or []) if isinstance(data, dict) else []:
                mem = r.get("memory", r) if isinstance(r, dict) else {}
                src = (mem.get("metadata") or {}).get("extraction_source") or {}
                print(f"- [{mem.get('source')}] owner={mem.get('owner_id')} "
                      f"thread={src.get('thread_id')} msgs={src.get('source_messages')}\n  {str(mem.get('content'))[:160]}")
            print(json.dumps(data, indent=2, default=str)[:4000])
        else:
            print(__doc__)


if __name__ == "__main__":
    asyncio.run(main())
