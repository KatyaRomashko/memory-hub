"""Score a memory store against what the conversation actually said.

Ray Carroll's objection to the existing memory benchmarks, 25.09.2026:

    "Most of it tends to be in the space of recall end-to-end... it doesn't
     break down the extraction piece versus the retrieval piece and evaluate
     those separately."

This does break them down, and adds a third number that end-to-end recall
cannot see at all:

    extraction    did the fact reach the store?
    preservation  is it still the CURRENT version, or did a later merge
                  retire it? A chain of false updates can delete a true fact
                  while every extraction reports success.
    retrieval     does a query for it bring it back, and at what rank? With
                  no relevance floor, the noise that comes back with it is
                  the precision cost.
    leakage       did a credential reach the store? Not a quality metric --
                  a release blocker for anything calling itself an
                  enterprise resource with an audit trail.

Usage (after `source live/env.sh`):

    python live/facts.py check                 # MemoryHub, from SQLite, instant
    python live/facts.py check --hindsight     # also score the shadow store
    python live/facts.py check --search        # also measure retrieval (loads
                                               # the embedding model, ~10 s)

Ground truth lives in live/facts.json and is meant to be edited: it describes
the scripted conversation, not MemoryHub.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from peek import BAR, connect, short  # noqa: E402

TOP_K = 5
OK, BAD, MEH = "PASS", "FAIL", "warn"


# ------------------------------------------------------------------ matching


def norm(text: str | None) -> str:
    return " ".join((text or "").lower().split())


def matches(fact: dict, text: str) -> bool:
    """Keyword match, deliberately crude.

    An LLM judge would be kinder and less reproducible. The point of the
    fixture is that a human wrote down what the store is supposed to contain
    and the check is mechanical.
    """
    t = norm(text)
    for bad in fact.get("exclude_if_any_of") or []:
        if bad in t:
            return False
    all_of = [k.lower() for k in fact.get("all_of") or []]
    any_of = [k.lower() for k in fact.get("any_of") or []]
    if all_of and not all(k in t for k in all_of):
        return False
    if any_of and not any(k in t for k in any_of):
        return False
    return bool(all_of or any_of)


def load_fixture(path: str) -> dict:
    return json.loads(Path(path).read_text())


# ------------------------------------------------- MemoryHub (SQLite, direct)


def memoryhub_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("""
        select n.id, n.content, n.is_current, n.version, n.previous_version_id,
               e.source_messages, t.a2a_context_id
          from memory_nodes n
          left join conversation_extractions e on e.memory_node_id = n.id
          left join conversation_threads t on t.id = e.thread_id
         where n.deleted_at is null
         order by n.created_at
    """))


def retirement_reason(conn: sqlite3.Connection, content: str) -> str | None:
    """Why a memory stopped being current, straight from the decision log."""
    for r in conn.execute("""
        select d.action, d.similarity_score, d.candidate_stub, n.content as nearest
          from reconciliation_decisions d
          left join memory_nodes n on n.id = d.nearest_match_id
         where d.action = 'update'
         order by d.created_at
    """):
        if norm(r["nearest"]) and norm(r["nearest"]) in norm(content):
            return (f"replaced at similarity {r['similarity_score']:.4f} by "
                    f"{short(r['candidate_stub'], 44)}")
    return None


def score_memoryhub(fixture: dict, conn: sqlite3.Connection) -> dict:
    rows = memoryhub_rows(conn)
    current = [r for r in rows if r["is_current"]]
    retired = [r for r in rows if not r["is_current"]]
    out = {"system": "memoryhub", "rows": len(rows), "current": len(current),
           "retired": len(retired), "facts": [], "leaks": []}

    for fact in fixture["facts"]:
        hit_current = [r for r in current if matches(fact, r["content"])]
        hit_retired = [r for r in retired if matches(fact, r["content"])]
        entry = {"id": fact["id"], "kind": fact["kind"],
                 "in_store": bool(hit_current or hit_retired),
                 "current": bool(hit_current),
                 "text": hit_current[0]["content"] if hit_current else
                         (hit_retired[0]["content"] if hit_retired else None),
                 "provenance": (hit_current or hit_retired)[0]["source_messages"]
                               if (hit_current or hit_retired) else None}
        if fact["kind"] == "must_not_be_current":
            entry["verdict"] = OK if not hit_current else BAD
        elif fact["kind"] == "noise":
            entry["verdict"] = MEH if hit_current else OK
        else:
            if hit_current:
                entry["verdict"] = OK
            elif hit_retired:
                entry["verdict"] = BAD
                entry["lost_to"] = retirement_reason(conn, hit_retired[0]["content"])
            else:
                entry["verdict"] = BAD
        out["facts"].append(entry)

    for secret in fixture.get("must_never_appear", []):
        found = [r for r in rows if secret.lower() in norm(r["content"])]
        if found:
            out["leaks"].append({"secret": secret[:8] + "...", "where": "memory_nodes",
                                 "count": len(found)})
    # messages are stored too, and a leak there is just as real
    for secret in fixture.get("must_never_appear", []):
        n = conn.execute(
            "select count(*) from conversation_messages where lower(content) like ?",
            (f"%{secret.lower()}%",)).fetchone()[0]
        if n:
            out["leaks"].append({"secret": secret[:8] + "...", "where": "conversation_messages",
                                 "count": n})
    return out


async def memoryhub_retrieval(fixture: dict, results: dict) -> None:
    """Rank each fact's own query, and count the noise that rides along."""
    from memoryhub_local.services.memory import search_memories
    from memoryhub_local.startup import initialize_backend

    state = await initialize_backend(quiet=True)
    async with state.session_factory() as db:
        for fact, entry in zip(fixture["facts"], results["facts"]):
            if not fact.get("query"):
                continue
            res = await search_memories(db, fact["query"], state.embedding_service,
                                        state.recall_backend)
            rows = (res.get("results") or [])[:TOP_K]
            rank, score = None, None
            for i, r in enumerate(rows, 1):
                if matches(fact, str(r.get("content"))):
                    rank, score = i, r.get("relevance_score")
                    break
            entry["rank"] = rank
            entry["score"] = score
            entry["returned"] = len(rows)
            entry["noise_in_topk"] = sum(
                1 for r in rows
                for n in fixture["facts"] if n["kind"] == "noise" and matches(n, str(r.get("content")))
            )


# ----------------------------------------------------------------- Hindsight


async def score_hindsight(fixture: dict) -> dict:
    import httpx

    base = (os.environ.get("MEMORYHUB_CAPTURE_HINDSIGHT_URL") or "http://localhost:8888").rstrip("/")
    bank = os.environ.get("MEMORYHUB_CAPTURE_HINDSIGHT_BANK") or "memoryhub-proxy-demo"
    key = os.environ.get("MEMORYHUB_CAPTURE_HINDSIGHT_API_KEY")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    out = {"system": "hindsight", "bank": bank, "facts": [], "leaks": []}

    async with httpx.AsyncClient(base_url=base, timeout=120.0, headers=headers) as client:
        listed = await client.get(f"/v1/default/banks/{bank}/memories/list", params={"limit": 200})
        listed.raise_for_status()
        items = listed.json().get("items") or []
        out["rows"] = listed.json().get("total", len(items))
        texts = [str(i.get("text") or i.get("content") or "") for i in items]

        for fact in fixture["facts"]:
            hit = [t for t in texts if matches(fact, t)]
            entry = {"id": fact["id"], "kind": fact["kind"], "in_store": bool(hit),
                     "current": bool(hit), "text": hit[0] if hit else None}
            if fact["kind"] == "must_not_be_current":
                entry["verdict"] = OK if not hit else BAD
            elif fact["kind"] == "noise":
                entry["verdict"] = MEH if hit else OK
            else:
                entry["verdict"] = OK if hit else BAD

            if fact.get("query"):
                r = await client.post(f"/v1/default/banks/{bank}/memories/recall",
                                      json={"query": fact["query"], "budget": "mid",
                                            "max_tokens": 2048})
                r.raise_for_status()
                rows = (r.json().get("results") or [])[:TOP_K]
                entry["returned"] = len(rows)
                for i, res in enumerate(rows, 1):
                    if matches(fact, str(res.get("text"))):
                        entry["rank"] = i
                        entry["score"] = (res.get("scores") or {}).get("final")
                        break
                entry["noise_in_topk"] = sum(
                    1 for res in rows
                    for n in fixture["facts"]
                    if n["kind"] == "noise" and matches(n, str(res.get("text")))
                )
            out["facts"].append(entry)

        for secret in fixture.get("must_never_appear", []):
            if any(secret.lower() in norm(t) for t in texts):
                out["leaks"].append({"secret": secret[:8] + "...", "where": "hindsight memories"})
    return out


# -------------------------------------------------------------------- report


def render(result: dict) -> None:
    facts = result["facts"]
    expected = [f for f in facts if f["kind"] == "expected"]
    kept = [f for f in expected if f["verdict"] == OK]
    reached = [f for f in expected if f["in_store"]]
    lost = [f for f in expected if f["in_store"] and not f["current"]]

    print(f"\n{BAR}\n{result['system']}   memories: {result.get('rows', '?')}"
          + (f" (current {result['current']}, retired {result['retired']})"
             if "current" in result else "") + f"\n{BAR}")
    for f in facts:
        mark = {OK: "  ok  ", BAD: " FAIL ", MEH: " warn "}[f["verdict"]]
        rank = f.get("rank")
        where = ""
        if rank:
            score = f.get("score")
            where = f"   rank {rank}/{f.get('returned', '?')}" + (f" score={score:.3f}"
                                                                  if isinstance(score, float) else "")
        elif f.get("query") is not None and "returned" in f:
            where = f"   NOT in top {TOP_K}"
        print(f"[{mark}] {f['id']:<16} {f['kind']:<20}{where}")
        if f.get("text"):
            tag = ""
            if f["kind"] == "must_not_be_current":
                tag = "correctly retired: " if f["verdict"] == OK else "STILL CURRENT: "
            print(f"          {tag}{short(f['text'], 64 - len(tag))}")
        if f.get("lost_to"):
            print(f"          lost: {f['lost_to']}")
        if f.get("provenance"):
            print(f"          from messages {f['provenance']}")

    print(f"\n{BAR}")
    print(f"extraction    {len(reached)}/{len(expected)} expected facts reached the store")
    print(f"preservation  {len(kept)}/{len(expected)} are still the current version"
          + (f"   ({len(lost)} extracted then retired by a merge)" if lost else ""))
    ranked = [f for f in expected if f.get("rank")]
    if any("returned" in f for f in facts):
        asked = [f for f in expected if "returned" in f]
        print(f"retrieval     {len(ranked)}/{len(asked)} come back in the top {TOP_K} for their own query")
        noise = sum(f.get("noise_in_topk", 0) for f in facts if "returned" in f)
        print(f"precision     {noise} ungrounded result(s) returned across those queries")
    stale = [f for f in facts if f["kind"] == "must_not_be_current" and f["verdict"] == BAD]
    if stale:
        print(f"CONTRADICTION {len(stale)} superseded fact(s) are still current: "
              + ", ".join(f["id"] for f in stale))
    if result["leaks"]:
        print("\nLEAKAGE  a credential reached the store:")
        for leak in result["leaks"]:
            print(f"   {leak['secret']} in {leak['where']}"
                  + (f" x{leak['count']}" if leak.get("count") else ""))
    else:
        print("leakage       none of the watched credentials reached the store")
    print(BAR)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", nargs="?", default="check", choices=["check"])
    ap.add_argument("--fixture", default=str(Path(__file__).with_name("facts.json")))
    ap.add_argument("--hindsight", action="store_true", help="also score the shadow store")
    ap.add_argument("--search", action="store_true",
                    help="also measure retrieval in MemoryHub (loads the embedding model)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    fixture = load_fixture(args.fixture)
    results = []

    conn = connect()
    mh = score_memoryhub(fixture, conn)
    if args.search:
        await memoryhub_retrieval(fixture, mh)
    results.append(mh)

    if args.hindsight:
        try:
            results.append(await score_hindsight(fixture))
        except Exception as exc:
            print(f"\nhindsight: not scored -- {type(exc).__name__}: {exc}", file=sys.stderr)

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        for r in results:
            render(r)


if __name__ == "__main__":
    asyncio.run(main())
