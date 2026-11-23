"""My activity (#718): a person's own projection of the audit log.

Three read models, all scoped to the signed-in person, whatever their role:

- ``GET /api/audit``: what the person and their agents did (and what was
  denied or blocked by a rule), with server-side filters and the counts the
  page shows, so a script gets the same answer as the dashboard.
- ``GET /api/me/reads``: who read, found or changed memories that belong to
  the person or were written by them or their agents. Reader identities and
  times only; other people's content is never returned.
- ``GET /api/operations`` and ``POST /api/operations/{id}/retry``: the
  background work that ran on the person's memories (entity extraction and
  conversation extraction), with a retry for failed entity extraction.

The tenant-wide audit for administrators is a separate screen and is not
served here: an admin calling these routes gets their own activity too.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from memoryhub_core.models.audit import AuditLog
from memoryhub_core.models.conversation import ConversationExtraction, ConversationExtractionFailure, ConversationThread
from memoryhub_core.models.memory import MemoryNode
from memoryhub_core.models.project import ProjectMembership
from memoryhub_core.services.extraction_runner import trigger_extraction
from sqlalchemy import String, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.authorization import Access, AccessDep
from src.database import get_db
from src.human_adapters import EmbeddingDep, _human_can_see_content, _record_human_audit

router = APIRouter()
DbDep = Annotated[AsyncSession, Depends(get_db)]

MAX_HOURS = 24 * 90
# Metadata keys that may leave the server. The person sees their own agents'
# search text; matched secrets or content never appear in audit metadata.
SHOWN_METADATA = ("rule", "blocked_by", "query", "max_results", "reason", "action",
                  "project", "role", "relationship_type", "target_id", "restored_rows")
READ_EVENTS = ("memory.read", "memory.search")
WRITE_EVENTS = ("memory.write", "memory.update")
TOUCH_EVENTS = ("memory.read", "memory.update", "memory.delete", "memory.relationship_created",
                "memory.contradiction_reported", "admin.quarantine", "admin.restore")
STALE_AFTER = timedelta(minutes=15)


def _since(hours: int) -> datetime:
    return datetime.now(UTC) - timedelta(hours=hours)


def _mine(access: Access):
    """Events the person made, or their agents made, or that were made on their behalf."""
    return or_(AuditLog.actor_id.in_(access.identities), AuditLog.driver_id == access.principal.username)


def _actor_kind(access: Access, actor: str) -> str | None:
    if actor == access.principal.username:
        return "user"
    if actor in access.agents:
        return "agent"
    return None


def _meta(row: AuditLog) -> dict:
    return {k: v for k, v in (row.event_metadata or {}).items() if k in SHOWN_METADATA}


async def _nodes(db: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, MemoryNode]:
    if not ids:
        return {}
    return {n.id: n for n in (await db.execute(select(MemoryNode).where(MemoryNode.id.in_(ids)))).scalars()}


async def _successors(db: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, MemoryNode]:
    """The version that replaced each memory: update events name the version they replaced."""
    if not ids:
        return {}
    rows = (await db.execute(select(MemoryNode).where(MemoryNode.previous_version_id.in_(ids)))).scalars()
    return {n.previous_version_id: n for n in rows}


def _stub(access: Access, node: MemoryNode | None) -> str | None:
    """A one-line preview, only of content the person may read now."""
    if node is None or not _human_can_see_content(access, node):
        return None
    text = " ".join((node.content or node.stub or "").split())
    return text if len(text) <= 120 else f"{text[:119]}…"


# --- activity log ------------------------------------------------------------

@router.get("/api/audit")
async def my_activity(
    db: DbDep,
    access: AccessDep,
    hours: int = Query(168, ge=1, le=MAX_HOURS),
    actor: Literal["all", "me", "agents"] = "all",
    operation: Annotated[list[str] | None, Query()] = None,
    scope: Literal["all", "personal", "projects"] = "all",
    decision: Literal["", "permitted", "denied"] = "",
    q: str = Query("", max_length=200),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    """The person's and their agents' events in the period, newest first."""
    tenant = access.principal.tenant_id
    since = _since(hours)
    base = [AuditLog.tenant_id == tenant, AuditLog.timestamp >= since, _mine(access)]

    where = list(base)
    if actor == "me":
        where.append(AuditLog.actor_id == access.principal.username)
    elif actor == "agents":
        where.append(AuditLog.actor_id.in_(access.agents or {""}))
    if operation:
        where.append(AuditLog.event_type.in_(operation))
    if scope == "personal":
        where.append(AuditLog.scope == "user")
    elif scope == "projects":
        where.append(AuditLog.scope == "project")
    if decision == "permitted":
        where.append(AuditLog.decision != "denied")
    elif decision == "denied":
        where.append(AuditLog.decision == "denied")
    if q.strip():
        pattern = f"%{q.strip()}%"
        visible = select(MemoryNode.id).where(access.content_clause(MemoryNode),
                                              or_(MemoryNode.stub.ilike(pattern), MemoryNode.content.ilike(pattern)))
        where.append(or_(cast(AuditLog.memory_id, String).ilike(pattern),
                         cast(AuditLog.event_metadata, String).ilike(pattern),
                         AuditLog.memory_id.in_(visible)))

    total = (await db.execute(select(func.count()).select_from(AuditLog).where(*where))).scalar_one()
    rows = (await db.execute(select(AuditLog).where(*where).order_by(
        AuditLog.timestamp.desc(), AuditLog.id.desc()).offset((page - 1) * per_page).limit(per_page))).scalars().all()
    nodes = await _nodes(db, {r.memory_id for r in rows if r.memory_id})
    after = await _successors(db, {r.memory_id for r in rows if r.memory_id and r.event_type == "memory.update"})

    items = []
    for r in rows:
        node = nodes.get(r.memory_id) if r.memory_id else None
        item = {
            "id": r.id, "timestamp": r.timestamp.isoformat(), "event_type": r.event_type,
            "operation": r.event_type, "actor_id": r.actor_id, "driver_id": r.driver_id,
            "actor_type": _actor_kind(access, r.actor_id), "scope": r.scope, "owner_id": r.owner_id,
            "memory_id": str(r.memory_id) if r.memory_id else None,
            "memory_stub": _stub(access, node), "memory_version": node.version if node else None,
            "decision": r.decision, "metadata": _meta(r),
        }
        successor = after.get(r.memory_id) if r.event_type == "memory.update" else None
        if node is not None and successor is not None:
            item["memory_version"] = successor.version
            item["change"] = {"before": {"version": node.version, "weight": node.weight},
                              "after": {"version": successor.version, "weight": successor.weight}}
        items.append(item)

    # Counts for the whole period, whatever the filters: the page's tiles and side panels.
    counts = (await db.execute(select(
        AuditLog.actor_id, AuditLog.event_type, AuditLog.scope, AuditLog.decision, func.count(),
    ).where(*base).group_by(AuditLog.actor_id, AuditLog.event_type, AuditLog.scope, AuditLog.decision))).all()
    me = access.principal.username
    changes_by_me = agent_writes = blocked = personal = project = 0
    agents_writing: set[str] = set()
    by_actor: dict[str, int] = {}
    event_types: set[str] = set()
    for actor_id, event_type, ev_scope, ev_decision, n in counts:
        event_types.add(event_type)
        if event_type in READ_EVENTS:
            continue
        if ev_decision == "denied":
            if event_type in WRITE_EVENTS:
                blocked += n
            continue
        by_actor[actor_id] = by_actor.get(actor_id, 0) + n
        if actor_id == me:
            changes_by_me += n
        elif event_type in WRITE_EVENTS:
            agent_writes += n
            agents_writing.add(actor_id)
            personal += n if ev_scope == "user" else 0
            project += n if ev_scope == "project" else 0
    blocked_rows = (await db.execute(select(AuditLog).where(
        *base, AuditLog.decision == "denied", AuditLog.event_type.in_(WRITE_EVENTS),
    ).order_by(AuditLog.timestamp.desc()).limit(5))).scalars().all()
    # Deleted memories (latest version of each, branches aside) that the person may restore.
    successor = aliased(MemoryNode)
    deleted = (await db.execute(select(func.count()).select_from(MemoryNode).where(
        MemoryNode.tenant_id == tenant, MemoryNode.deleted_at.is_not(None), MemoryNode.parent_id.is_(None),
        or_(MemoryNode.actor_id.in_(access.identities),
            and_(MemoryNode.scope == "user", MemoryNode.owner_id.in_(access.identities))),
        ~select(successor.id).where(successor.previous_version_id == MemoryNode.id).exists(),
    ))).scalar_one()

    return {
        "items": items, "total": total, "page": page, "per_page": per_page,
        "hours": hours, "identities": sorted(access.identities), "agents": sorted(access.agents),
        "event_types": sorted(event_types),
        "denied_total": sum(n for *_, d, n in counts if d == "denied"),
        "summary": {
            "changes_by_me": changes_by_me, "agent_writes": agent_writes,
            "agents_writing": len(agents_writing), "agent_writes_personal": personal,
            "agent_writes_project": project, "blocked": blocked, "deleted_restorable": deleted,
            "by_actor": [{"actor_id": a, "count": n, "yours": a == me}
                         for a, n in sorted(by_actor.items(), key=lambda x: -x[1])[:6]],
            "blocked_recent": [{"id": r.id, "timestamp": r.timestamp.isoformat(), "actor_id": r.actor_id,
                                "scope": r.scope, "rule": (r.event_metadata or {}).get("rule"),
                                "blocked_by": (r.event_metadata or {}).get("blocked_by")}
                               for r in blocked_rows],
        },
        "retention_days": None,
    }


# --- who read my memories -------------------------------------------------------

def _my_memories(access: Access):
    """Memories that are the person's: personal ones they own, and any they or their agents wrote."""
    return select(MemoryNode.id).where(
        MemoryNode.tenant_id == access.principal.tenant_id,
        or_(and_(MemoryNode.scope == "user", MemoryNode.owner_id.in_(access.identities)),
            MemoryNode.actor_id.in_(access.identities)),
    )


@router.get("/api/me/reads")
async def my_reads(
    db: DbDep,
    access: AccessDep,
    days: int = Query(30, ge=1, le=90),
    who: Literal["others", "all"] = "others",
):
    """Readers of the person's memories: identity, on whose behalf, how many reads, why it was allowed."""
    tenant = access.principal.tenant_id
    where = [AuditLog.tenant_id == tenant, AuditLog.timestamp >= _since(days * 24),
             AuditLog.event_type.in_(TOUCH_EVENTS), AuditLog.memory_id.in_(_my_memories(access))]
    if who == "others":
        where.append(AuditLog.actor_id.not_in(access.identities))
    rows = (await db.execute(select(AuditLog).where(*where).order_by(AuditLog.timestamp.desc()))).scalars().all()
    nodes = await _nodes(db, {r.memory_id for r in rows})

    readers_of = {r.actor_id for r in rows} | {r.driver_id for r in rows if r.driver_id}
    projects = {n.scope_id for n in nodes.values() if n.scope == "project" and n.scope_id}
    member: set[tuple[str, str]] = set()
    if projects and readers_of:
        member = set((await db.execute(select(ProjectMembership.user_id, ProjectMembership.project_id).where(
            ProjectMembership.project_id.in_(projects), ProjectMembership.user_id.in_(readers_of),
        ))).all())

    readers: dict[str, dict] = {}
    top: dict[uuid.UUID, int] = {}
    summary = {"reads_by_others": 0, "personal_by_others": 0, "admin_without_workflow": 0, "incident_grants": 0}
    for r in rows:
        node = nodes.get(r.memory_id)
        yours = r.actor_id in access.identities
        rd = readers.setdefault(r.actor_id, {
            "actor_id": r.actor_id, "driver_id": r.driver_id, "count": 0, "last": r.timestamp.isoformat(),
            "kinds": [], "denied": 0, "yours": yours, "via_dashboard": r.actor_id == r.driver_id and not yours,
            "memories": set(), "reason": None, "reason_kind": None,
        })
        rd["count"] += 1
        if r.event_type not in rd["kinds"]:
            rd["kinds"].append(r.event_type)
        rd["denied"] += r.decision == "denied"
        rd["memories"].add(r.memory_id)
        if yours:
            rd["reason"], rd["reason_kind"] = "Yours", "own"
        elif node is not None and node.scope == "project" and rd["reason_kind"] is None and (
                (r.actor_id, node.scope_id) in member or (r.driver_id, node.scope_id) in member):
            rd["reason"], rd["reason_kind"] = f"Member of {node.scope_id}", "project"
        elif node is not None and node.scope == "organizational" and rd["reason_kind"] is None:
            rd["reason"], rd["reason_kind"] = "Organizational memory", "project"
        if r.event_type == "memory.read" and r.decision != "denied":
            top[r.memory_id] = top.get(r.memory_id, 0) + 1
            if not yours:
                summary["reads_by_others"] += 1
                summary["personal_by_others"] += bool(node is not None and node.scope == "user")

    def shown(node: MemoryNode | None) -> str:
        return _stub(access, node) or "Memory you can no longer read"

    out = sorted(readers.values(), key=lambda x: -x["count"])
    for rd in out:
        rd["memories"] = len(rd["memories"])
    return {
        "summary": summary, "days": days, "who": who, "identities": sorted(access.identities),
        "total": len(rows), "readers": out,
        "top_memories": [{"id": str(i), "stub": shown(nodes.get(i)), "scope": nodes[i].scope if i in nodes else None,
                          "count": n} for i, n in sorted(top.items(), key=lambda x: -x[1])[:8]],
        "events": [{"id": r.id, "timestamp": r.timestamp.isoformat(), "event_type": r.event_type,
                    "actor_id": r.actor_id, "driver_id": r.driver_id, "decision": r.decision,
                    "memory_id": str(r.memory_id), "memory_stub": shown(nodes.get(r.memory_id)),
                    "yours": r.actor_id in access.identities,
                    "break_glass": False} for r in rows[:50]],
    }


# --- background jobs -----------------------------------------------------------

KINDS = {"entity_extraction": "Entity extraction", "conversation_extraction": "Conversation extraction"}
_EXTRACTION = {"pending": "running", "complete": "ok", "partial": "failed", "failed": "failed"}


def _authored(access: Access):
    return or_(MemoryNode.actor_id.in_(access.identities),
               and_(MemoryNode.scope == "user", MemoryNode.owner_id.in_(access.identities)))


async def _extraction_jobs(db: AsyncSession, access: Access, since: datetime) -> list[dict]:
    status = MemoryNode.metadata_.op("->>")("extraction_status")  # JSON column, not JSONB
    rows = (await db.execute(select(MemoryNode).where(
        MemoryNode.tenant_id == access.principal.tenant_id, MemoryNode.deleted_at.is_(None),
        MemoryNode.created_at >= since, status.is_not(None), _authored(access),
    ).order_by(MemoryNode.created_at.desc()).limit(500))).scalars().all()
    now = datetime.now(UTC)
    jobs = []
    for n in rows:
        meta = n.metadata_ or {}
        raw = meta.get("extraction_status")
        state = _EXTRACTION.get(raw, "ok")
        retried = meta.get("extraction_retried_at")
        started = datetime.fromisoformat(retried) if retried else n.created_at
        stale = state == "running" and now - started > STALE_AFTER
        jobs.append({
            "id": f"extract:{n.id}", "owner": n.actor_id or n.owner_id, "kind": "entity_extraction",
            "label": KINDS["entity_extraction"], "status": state,
            "trigger": "retried from the dashboard" if retried else "memory written",
            "started_at": started.isoformat(), "finished_at": None, "duration_ms": None,
            "attempts": 1 + int(meta.get("extraction_retries") or 0),
            "correlation_id": str(n.id), "retry_of": None,
            "error": "Some entities could not be extracted" if raw == "partial" else
                     "Extraction failed; the memory is saved but has no entities" if raw == "failed" else None,
            "retryable": state == "failed" or stale, "stale": stale,
            "detail": {"memory_id": str(n.id), "memory_stub": _stub(access, n),
                       "entities": len((n.metadata_ or {}).get("extracted_entities") or [])},
        })
    return jobs


async def _conversation_jobs(db: AsyncSession, access: Access, since: datetime) -> list[dict]:
    tenant = access.principal.tenant_id
    mine = and_(ConversationThread.tenant_id == tenant,
                or_(ConversationThread.owner_id.in_(access.identities),
                    ConversationThread.actor_id.in_(access.identities)))
    failures = (await db.execute(select(ConversationExtractionFailure, ConversationThread.owner_id).join(
        ConversationThread, ConversationThread.id == ConversationExtractionFailure.thread_id,
    ).where(mine, ConversationExtractionFailure.last_attempt_at >= since))).all()
    done = (await db.execute(select(ConversationExtraction, ConversationThread.owner_id).join(
        ConversationThread, ConversationThread.id == ConversationExtraction.thread_id,
    ).where(mine, ConversationExtraction.created_at >= since).limit(500))).all()
    jobs = [{
        "id": f"conversation-failure:{f.id}", "owner": owner, "kind": "conversation_extraction",
        "label": KINDS["conversation_extraction"], "status": "ok" if f.resolved else "failed",
        "trigger": "conversation messages", "started_at": f.created_at.isoformat(),
        "finished_at": f.last_attempt_at.isoformat(), "duration_ms": None, "attempts": f.attempt_count,
        "correlation_id": str(f.thread_id), "retry_of": None,
        # The extractor's error text can quote the conversation: only its owner sees it.
        "error": None if f.resolved else (f.last_error[:300] if owner in access.identities else "Extraction failed"),
        "retryable": False, "stale": False,
        "detail": {"thread_id": str(f.thread_id), "window": [f.window_start, f.window_end]},
    } for f, owner in failures]
    jobs += [{
        "id": f"conversation:{x.id}", "owner": owner, "kind": "conversation_extraction",
        "label": KINDS["conversation_extraction"], "status": "ok", "trigger": "conversation messages",
        "started_at": x.created_at.isoformat(), "finished_at": x.created_at.isoformat(), "duration_ms": None,
        "attempts": 1, "correlation_id": str(x.thread_id), "retry_of": None, "error": None,
        "retryable": False, "stale": False,
        "detail": {"thread_id": str(x.thread_id), "memory_id": str(x.memory_node_id),
                   "messages": len(x.source_messages or []), "model": x.extraction_model},
    } for x, owner in done]
    return jobs


@router.get("/api/operations")
async def my_operations(
    db: DbDep,
    access: AccessDep,
    days: int = Query(7, ge=1, le=90),
    status: Literal["", "ok", "failed", "running"] = "",
    kind: str = "",
):
    """Background work on the person's memories in the period."""
    since = _since(days * 24)
    jobs = await _extraction_jobs(db, access, since) + await _conversation_jobs(db, access, since)
    kinds = []
    for k, label in KINDS.items():
        of_kind = [j for j in jobs if j["kind"] == k]
        if of_kind:
            kinds.append({"kind": k, "label": label,
                          "ok": sum(j["status"] == "ok" for j in of_kind),
                          "failed": sum(j["status"] == "failed" for j in of_kind),
                          "running": sum(j["status"] == "running" for j in of_kind)})
    items = [j for j in jobs if (not status or j["status"] == status) and (not kind or j["kind"] == kind)]
    items.sort(key=lambda j: j["started_at"], reverse=True)
    return {"items": items[:500], "kinds": kinds, "days": days, "scope": "own"}


@router.post("/api/operations/{job_id}/retry")
async def retry_operation(job_id: str, db: DbDep, access: AccessDep, embedding: EmbeddingDep):
    """Run entity extraction again for a memory the person or their agents wrote."""
    kind, _, raw = job_id.partition(":")
    if kind != "extract":
        raise HTTPException(status_code=409, detail="Only entity extraction can be retried")
    try:
        memory_id = uuid.UUID(raw)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    node = (await db.execute(select(MemoryNode).where(
        MemoryNode.id == memory_id, MemoryNode.tenant_id == access.principal.tenant_id,
        MemoryNode.deleted_at.is_(None), _authored(access),
    ).with_for_update())).scalar_one_or_none()
    if node is None or "extraction_status" not in (node.metadata_ or {}):
        raise HTTPException(status_code=404, detail="Job not found")
    if (node.metadata_ or {}).get("extraction_status") == "complete":
        raise HTTPException(status_code=409, detail="Extraction already succeeded")
    meta = node.metadata_ or {}
    node.metadata_ = {**meta, "extraction_status": "pending",
                      "extraction_retried_at": datetime.now(UTC).isoformat(),
                      "extraction_retries": int(meta.get("extraction_retries") or 0) + 1}
    _record_human_audit(db, access, "job.retry", node.scope, node.owner_id, node.id,
                        {"action": "entity_extraction"})
    await db.commit()
    await trigger_extraction(node.id, node.content, node.tenant_id, node.owner_id, embedding)
    return {"ok": True, "id": job_id}
