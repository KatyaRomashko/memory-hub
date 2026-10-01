"""Look inside MemoryHub while the live demo runs -- read-only, instant.

Opens the personal edition's SQLite file directly: no backend start-up, no
embedding model, no log noise, so it answers in the second terminal without
interrupting the session in the first one.

    python live/peek.py state                # one screen: threads, messages, memories, cursor
    python live/peek.py thread [REF]         # every stored message, and where the cursor is
    python live/peek.py next [REF]           # exactly what the NEXT extraction will read
    python live/peek.py memories [--all]     # current memories, provenance, what they replaced
    python live/peek.py decisions            # create / update / skip, with similarity scores
    python live/peek.py raw [REF]            # the thread row as MemoryHub stores it
    python live/peek.py watch [SECONDS]      # re-print `state` every N seconds (default 2)

REF is a thread id (a prefix is enough) or a session key such as "demo-live".
With one thread in the database REF can be omitted.

The database is $XDG_DATA_HOME/memoryhub/memoryhub.db -- live/env.sh sets that.
Semantic search needs the embedding model, so it stays where it was:

    python verify.py search "dark mode"
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

# MemoryHub's own extraction settings, mirrored here so the preview matches
# what the pipeline will actually do (memoryhub_local/services/extraction.py).
WINDOW_SIZE = 10
SKIP_THRESHOLD = 0.98
UPDATE_THRESHOLD = 0.85

BAR = "-" * 78


def db_path() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "memoryhub" / "memoryhub.db"


def connect() -> sqlite3.Connection:
    p = db_path()
    if not p.exists():
        if os.environ.get("MEMORYHUB_CAPTURE_SINK") != "local":
            sys.exit("run `source live/env.sh` in this terminal first")
        # a clean start, not an error: MemoryHub simply has nothing yet
        print(f"{BAR}\n{p}\n\nno database yet -- MemoryHub is empty.\n"
              f"It is created the first time the proxy stores a message.\n{BAR}")
        raise SystemExit(0)
    # read-only: peeking must never lock or change the demo database
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def meta(row: sqlite3.Row, key: str, default=None):
    try:
        return (json.loads(row["metadata"] or "{}") or {}).get(key, default)
    except (ValueError, IndexError):
        return default


def threads(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "select * from conversation_threads where status = 'active' order by created_at"
    ))


def pick(conn: sqlite3.Connection, ref: str | None) -> sqlite3.Row:
    rows = threads(conn)
    if not rows:
        sys.exit("no threads yet -- send the agent a message first")
    if ref:
        hit = [t for t in rows if t["id"].startswith(ref) or (t["a2a_context_id"] or "") == ref]
        if not hit:
            hit = [t for t in rows if ref in (t["a2a_context_id"] or "")]
        if not hit:
            sys.exit(f"no thread matches {ref!r}; known: " +
                     ", ".join(t["a2a_context_id"] or t["id"][:8] for t in rows))
        return hit[0]
    if len(rows) > 1:
        sys.exit("several threads; pass one: " +
                 ", ".join(t["a2a_context_id"] or t["id"][:8] for t in rows))
    return rows[0]


def messages(conn: sqlite3.Connection, thread_id: str) -> list[sqlite3.Row]:
    return list(conn.execute(
        "select * from conversation_messages where thread_id = ? order by sequence_number",
        (thread_id,),
    ))


def compute_windows(msgs: list[sqlite3.Row], size: int = WINDOW_SIZE) -> list[list[sqlite3.Row]]:
    """Same rule as memoryhub_local.services.extraction.compute_windows."""
    windows: list[list[sqlite3.Row]] = []
    current: list[sqlite3.Row] = []
    for m in msgs:
        if current and m["role"] == "user" and current[-1]["role"] == "assistant":
            windows.append(current)
            current = []
        current.append(m)
        if len(current) >= size:
            windows.append(current)
            current = []
    if current:
        windows.append(current)
    return windows


def short(text: str | None, width: int = 68) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= width else t[: width - 1] + "…"


# ---------------------------------------------------------------- commands


def cmd_state(conn: sqlite3.Connection) -> None:
    rows = threads(conn)
    n_msg = conn.execute("select count(*) from conversation_messages").fetchone()[0]
    n_mem = conn.execute(
        "select count(*) from memory_nodes where deleted_at is null and is_current = 1").fetchone()[0]
    n_dec = conn.execute("select count(*) from reconciliation_decisions").fetchone()[0]
    n_fail = conn.execute("select count(*) from conversation_extraction_failures").fetchone()[0]
    print(f"{BAR}\n{db_path()}")
    print(f"threads {len(rows)}   messages {n_msg}   current memories {n_mem}   "
          f"decisions {n_dec}   failed windows {n_fail}\n{BAR}")
    for t in rows:
        msgs = messages(conn, t["id"])
        pending = [m for m in msgs if m["sequence_number"] > t["extraction_cursor"]]
        mem = conn.execute(
            "select count(*) from conversation_extractions where thread_id = ?", (t["id"],)
        ).fetchone()[0]
        print(f"\nsession  {t['a2a_context_id']}   thread {t['id'][:8]}   owner {t['owner_id']}")
        print(f"  source={meta(t, 'source')}  session_source={meta(t, 'session_source')}  "
              f"observed_actor={meta(t, 'observed_actor_id')} ({meta(t, 'observed_actor_source')})")
        print(f"  messages={len(msgs)}  cursor={t['extraction_cursor']}  "
              f"waiting for extraction={len(pending)}  extractions={mem} "
              f"(rows from this thread, superseded versions included)")
    print()


def cmd_thread(conn: sqlite3.Connection, ref: str | None) -> None:
    t = pick(conn, ref)
    msgs = messages(conn, t["id"])
    print(f"{BAR}\nthread {t['id']}   session {t['a2a_context_id']}   "
          f"cursor {t['extraction_cursor']}\n{BAR}")
    for m in msgs:
        mark = "  " if m["sequence_number"] <= t["extraction_cursor"] else ">>"
        src = meta(m, "source") or "-"
        print(f"{mark} {m['sequence_number']:>3} {m['role']:<9} [{src}] {short(m['content'])}")
    print(f"\n   = already extracted (seq <= cursor {t['extraction_cursor']})")
    print("  >> = still waiting; `python live/peek.py next` shows what it will look like\n")


def cmd_next(conn: sqlite3.Connection, ref: str | None) -> None:
    t = pick(conn, ref)
    pending = [m for m in messages(conn, t["id"])
               if m["sequence_number"] > t["extraction_cursor"]]
    print(f"{BAR}\nthread {t['id'][:8]}  cursor {t['extraction_cursor']}  "
          f"unprocessed messages {len(pending)}\n{BAR}")
    if not pending:
        print("nothing pending: the next extraction would find no new messages.\n")
        return
    wins = compute_windows(pending)
    print(f"MemoryHub would split them into {len(wins)} window(s) "
          f"(max {WINDOW_SIZE} messages, or a new user turn after an assistant reply).")
    print("Each window is sent to the extraction model exactly like this:\n")
    for i, w in enumerate(wins, 1):
        print(f"--- window {i}  (seq {w[0]['sequence_number']}..{w[-1]['sequence_number']}) ---")
        for m in w:
            print(f"[{m['role'].upper()}] (seq={m['sequence_number']}): {m['content']}")
        print()


def cmd_memories(conn: sqlite3.Connection, ref: str | None = None) -> None:
    """Memories with provenance and version history.

    ``update`` during reconciliation does not edit a memory in place: it writes
    a NEW version and flips the old row's is_current to 0. Listing every row
    therefore makes superseded facts look live, so they are marked here.
    """
    show_all = ref in ("--all", "all")
    rows = list(conn.execute("""
        select n.id, n.content, n.source, n.weight, n.created_at, n.version,
               n.is_current, n.previous_version_id, n.logical_id,
               e.thread_id, e.source_messages, t.a2a_context_id
          from memory_nodes n
          left join conversation_extractions e on e.memory_node_id = n.id
          left join conversation_threads t on t.id = e.thread_id
         where n.deleted_at is null
         order by n.created_at
    """))
    by_id = {r["id"]: r for r in rows}
    replaced_by = {r["previous_version_id"]: r for r in rows if r["previous_version_id"]}
    current = [r for r in rows if r["is_current"]]
    old_rows = [r for r in rows if not r["is_current"]]

    print(f"{BAR}\ncurrent memories: {len(current)}"
          + (f"   superseded: {len(old_rows)}" if old_rows else "") + f"\n{BAR}")
    for r in current:
        prov = (f"thread {str(r['thread_id'])[:8]} ({r['a2a_context_id']}) "
                f"messages {r['source_messages']}" if r["thread_id"] else
                "NO thread provenance (written directly, not by extraction)")
        print(f"\n[{r['source']}] v{r['version']} weight={r['weight']}  {r['created_at']}")
        print(f"  {r['content']}")
        print(f"  <- {prov}")
        prev = by_id.get(r["previous_version_id"]) if r["previous_version_id"] else None
        if prev:
            print(f"  ** this version REPLACED an earlier memory, which is no longer current:")
            print(f"     {short(prev['content'], 70)}")

    if old_rows and not show_all:
        print(f"\n{len(old_rows)} superseded row(s) hidden -- `peek.py memories --all` to see them")
    elif old_rows:
        print(f"\n{BAR}\nsuperseded (is_current = 0)\n{BAR}")
        for r in old_rows:
            nxt = replaced_by.get(r["id"])
            print(f"\n[{r['source']}] v{r['version']}  {short(r['content'], 70)}")
            print(f"  replaced by: {short(nxt['content'], 66) if nxt else '?'}")
    print()


def cmd_decisions(conn: sqlite3.Connection) -> None:
    rows = list(conn.execute("""
        select d.*, n.content as nearest
          from reconciliation_decisions d
          left join memory_nodes n on n.id = d.nearest_match_id
         order by d.created_at
    """))
    print(f"{BAR}\nreconciliation: every candidate the extractor produced")
    print(f"skip >= {SKIP_THRESHOLD}   update >= {UPDATE_THRESHOLD}   "
          f"below that -> create")
    print("update writes a NEW version of the nearest memory and retires the old one;\n"
          f"two different facts about one topic can land above {UPDATE_THRESHOLD} and be merged\n{BAR}")
    if not rows:
        print("no decisions yet -- extraction has not run on this database.\n")
        return
    run = None
    for r in rows:
        if r["extraction_run_id"] != run:
            run = r["extraction_run_id"]
            print(f"\nrun {run}")
        score = "   -  " if r["similarity_score"] is None else f"{r['similarity_score']:.3f}"
        print(f"  {r['action']:<7} score={score}  {short(r['candidate_stub'], 52)}")
        print(f"          reason: {r['reason']}")
        if r["nearest"]:
            print(f"          nearest: {short(r['nearest'], 60)}")
    print()


def cmd_raw(conn: sqlite3.Connection, ref: str | None) -> None:
    t = pick(conn, ref)
    print(json.dumps({k: t[k] for k in t.keys()}, indent=2, default=str))


def cmd_watch(conn_unused, every: float) -> None:
    try:
        while True:
            os.system("clear")
            print(time.strftime("%H:%M:%S"))
            with connect() as conn:
                cmd_state(conn)
            time.sleep(every)
    except KeyboardInterrupt:
        pass


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "state"
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    if cmd == "watch":
        cmd_watch(None, float(arg or 2))
        return
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return
    with connect() as conn:
        if cmd == "state":
            cmd_state(conn)
        elif cmd == "thread":
            cmd_thread(conn, arg)
        elif cmd == "next":
            cmd_next(conn, arg)
        elif cmd == "memories":
            cmd_memories(conn, arg)
        elif cmd == "decisions":
            cmd_decisions(conn)
        elif cmd == "raw":
            cmd_raw(conn, arg)
        else:
            print(__doc__)


if __name__ == "__main__":
    main()
