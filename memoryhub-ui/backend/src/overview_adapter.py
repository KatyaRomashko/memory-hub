"""User Overview (#711): activity over time, agent usage, categories, requests, getting started.

Everything is limited to the signed-in person and their agents (``identities``):

- ``GET /api/overview?range=1d|7d|30d``: writes and searches per bucket from
  ``audit_log`` (events whose actor is the person or one of their agents, or
  driven by the person); a breakdown of the categories (``domains``) of the
  memories the person may read; their open requests when the requests feature
  is installed (``null`` otherwise, and the page hides the block).
- ``GET /api/usage/summary?days``: do the person's agents use what search hands
  them? From ``memory.served`` events of their agents: a served memory counts as
  used when the same agent reads, updates, links or reports it within 30
  minutes. Memories delivered in full (weight ≥ 0.80) are counted separately.
  The most skipped memories are listed only when the person may read them.
- ``GET /api/me/onboarding`` and ``POST /api/me/onboarding/dismiss``: a
  getting-started checklist computed from the person's own records, with its
  dismissal stored per person (``memoryhub_ui_onboarding``).
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from memoryhub_core.models.audit import AuditLog
from memoryhub_core.models.curation import CuratorRule
from memoryhub_core.models.memory import MemoryNode
from sqlalchemy import Column, DateTime, String, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import declarative_base

from src.authorization import Access, AccessDep
from src.database import get_db

router = APIRouter()
DbDep = Annotated[AsyncSession, Depends(get_db)]
OnboardingBase = declarative_base()

RANGES: dict[str, tuple[timedelta, timedelta]] = {
    # range: (window, bucket)
    "1d": (timedelta(hours=24), timedelta(hours=1)),
    "7d": (timedelta(days=7), timedelta(days=1)),
    "30d": (timedelta(days=30), timedelta(days=1)),
}
WRITE_EVENTS = ("memory.write", "memory.update")
SEARCH_EVENTS = ("memory.search",)
USE_EVENTS = ("memory.read", "memory.update", "memory.relationship_created", "memory.contradiction_reported")
USE_WINDOW = timedelta(minutes=30)
SERVED_LIMIT = 5000
CATEGORY_LIMIT = 12


class OnboardingState(OnboardingBase):
    __tablename__ = "memoryhub_ui_onboarding"
    tenant_id = Column(String(255), primary_key=True)
    user_id = Column(String(255), primary_key=True)
    dismissed_at = Column(DateTime(timezone=True))


def _require_user(access: Access) -> None:
    if access.principal.role != "user":
        raise HTTPException(status_code=403, detail="The personal overview is for users")


def _mine(access: Access):
    """Audit events the person did or drove, themselves or through their agents."""
    return or_(AuditLog.actor_id.in_(access.identities), AuditLog.driver_id == access.principal.username)


def _bucket_start(at: datetime, bucket: timedelta) -> datetime:
    at = at.astimezone(UTC)
    if bucket >= timedelta(days=1):
        return at.replace(hour=0, minute=0, second=0, microsecond=0)
    return at.replace(minute=0, second=0, microsecond=0)


async def _open_requests(db: AsyncSession, access: Access) -> int | None:
    """Open requests (#719) when that table exists in this deployment; else None."""
    installed = (await db.execute(text("select to_regclass('memoryhub_ui_requests') is not null"))).scalar_one()
    if not installed:
        return None
    return int((await db.execute(text(
        "select count(*) from memoryhub_ui_requests "
        "where tenant_id = :tenant and requester = :user and state = 'open'"),
        {"tenant": access.principal.tenant_id, "user": access.principal.username})).scalar_one())


@router.get("/api/overview")
async def overview(db: DbDep, access: AccessDep, response: Response,
                   range: Literal["1d", "7d", "30d"] = "7d"):  # noqa: A002 - the page's query name
    _require_user(access)
    response.headers["Cache-Control"] = "no-store"
    window, bucket = RANGES[range]
    now = datetime.now(UTC)
    since = now - window

    rows = (await db.execute(
        select(AuditLog.timestamp, AuditLog.event_type)
        .where(AuditLog.tenant_id == access.principal.tenant_id, AuditLog.timestamp >= since,
               AuditLog.decision == "allowed", AuditLog.event_type.in_(WRITE_EVENTS + SEARCH_EVENTS), _mine(access))
    )).all()
    counts: dict[datetime, Counter] = defaultdict(Counter)
    for at, event_type in rows:
        counts[_bucket_start(at, bucket)]["writes" if event_type in WRITE_EVENTS else "searches"] += 1
    buckets, cursor = [], _bucket_start(since, bucket)
    while cursor <= now:
        buckets.append({"date": cursor.isoformat(), "writes": counts[cursor]["writes"],
                        "searches": counts[cursor]["searches"]})
        cursor += bucket

    domain = func.unnest(MemoryNode.domains).label("domain")
    categories_q = (select(domain, func.count().label("n"))
                    .where(MemoryNode.is_current.is_(True), MemoryNode.deleted_at.is_(None), access.clause())
                    .group_by(text("domain")).order_by(text("n desc"), text("domain")).limit(CATEGORY_LIMIT + 1))
    categories = [{"name": name, "memory_count": int(n)} for name, n in (await db.execute(categories_q)).all()]

    return {
        "generated_at": now.isoformat(),
        "activity_range": range,
        "activity_bucket_seconds": int(bucket.total_seconds()),
        "activity": buckets,
        "totals": {"writes": sum(b["writes"] for b in buckets), "searches": sum(b["searches"] for b in buckets)},
        "categories": categories[:CATEGORY_LIMIT],
        "categories_truncated": len(categories) > CATEGORY_LIMIT,
        "requests": None if (n := await _open_requests(db, access)) is None else {"open": n},
    }


@router.get("/api/usage/summary")
async def usage_summary(db: DbDep, access: AccessDep, days: Annotated[int, Query(ge=1, le=90)] = 30):
    """How often the person's agents act on what search hands them."""
    _require_user(access)
    tenant = access.principal.tenant_id
    since = datetime.now(UTC) - timedelta(days=days)
    agents = access.agents
    empty = {"days": days, "agents": sorted(agents), "served": 0, "memories_served": 0, "full": 0,
             "used": 0, "skipped": 0, "use_rate": None, "skipped_most": []}
    if not agents:
        return empty
    served_events = (await db.execute(
        select(AuditLog.actor_id, AuditLog.timestamp, AuditLog.event_metadata)
        .where(AuditLog.tenant_id == tenant, AuditLog.event_type == "memory.served",
               AuditLog.actor_id.in_(agents), AuditLog.timestamp >= since)
        .order_by(AuditLog.timestamp).limit(SERVED_LIMIT))).all()
    if not served_events:
        return empty
    uses: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    for actor, memory_id, at in (await db.execute(
        select(AuditLog.actor_id, AuditLog.memory_id, AuditLog.timestamp)
        .where(AuditLog.tenant_id == tenant, AuditLog.actor_id.in_(agents), AuditLog.event_type.in_(USE_EVENTS),
               AuditLog.decision == "allowed", AuditLog.memory_id.is_not(None), AuditLog.timestamp >= since))).all():
        uses[(actor, str(memory_id))].append(at)

    served = full = used = skipped = 0
    per_memory: dict[str, Counter] = defaultdict(Counter)
    for actor, at, meta in served_events:
        meta = meta or {}
        full_ids = set(map(str, meta.get("full_ids") or []))
        for memory_id in map(str, meta.get("result_ids") or []):
            served += 1
            per_memory[memory_id]["served"] += 1
            if memory_id in full_ids:
                full += 1  # delivered in full: there is nothing more to fetch, so not measured
                continue
            if any(at <= when <= at + USE_WINDOW for when in uses.get((actor, memory_id), ())):
                used += 1
                per_memory[memory_id]["used"] += 1
            else:
                skipped += 1
                per_memory[memory_id]["skipped"] += 1

    candidates = sorted((m for m, c in per_memory.items() if c["skipped"]),
                        key=lambda m: (-per_memory[m]["skipped"], m))[:20]
    readable = {}
    ids = []
    for memory_id in candidates:
        try:
            ids.append(uuid.UUID(memory_id))
        except ValueError:
            continue
    if ids:
        for node in (await db.execute(select(MemoryNode).where(MemoryNode.id.in_(ids)))).scalars():
            if access.can_read(node) and node.deleted_at is None:
                readable[str(node.id)] = node
    skipped_most = [{
        "id": m, "stub": readable[m].stub, "scope": readable[m].scope, "weight": readable[m].weight,
        "served": per_memory[m]["served"], "used": per_memory[m]["used"], "skipped": per_memory[m]["skipped"],
        "use_rate": per_memory[m]["used"] / (per_memory[m]["used"] + per_memory[m]["skipped"]),
    } for m in candidates if m in readable][:10]
    measured = used + skipped
    return {**empty, "served": served, "memories_served": len(per_memory), "full": full, "used": used,
            "skipped": skipped, "use_rate": used / measured if measured else None, "skipped_most": skipped_most}


async def _onboarding(db: AsyncSession, access: Access) -> dict:
    tenant, user = access.principal.tenant_id, access.principal.username
    wrote = (await db.execute(select(MemoryNode.id).where(
        MemoryNode.tenant_id == tenant, MemoryNode.deleted_at.is_(None),
        or_(MemoryNode.owner_id.in_(access.identities), MemoryNode.actor_id.in_(access.identities))).limit(1)
    )).scalar_one_or_none() is not None
    searched = bool(access.agents) and (await db.execute(select(AuditLog.id).where(
        AuditLog.tenant_id == tenant, AuditLog.event_type == "memory.search",
        AuditLog.actor_id.in_(access.agents)).limit(1))).scalar_one_or_none() is not None
    has_rule = (await db.execute(select(CuratorRule.id).where(
        CuratorRule.tenant_id == tenant, CuratorRule.layer == "user", CuratorRule.owner_id == user).limit(1)
    )).scalar_one_or_none() is not None
    steps = [
        {"id": "connect_agent", "title": "Connect an agent", "href": "/identities",
         "body": "Create an API key for Claude Code or another MCP client.", "done": bool(access.agents)},
        {"id": "first_memory", "title": "Save a first memory", "href": "/memories",
         "body": "Ask your agent to remember something, or write one here.", "done": wrote},
        {"id": "agent_search", "title": "Let your agent search", "href": "/search",
         "body": "Your agent searches MemoryHub before it answers.", "done": searched},
        {"id": "join_project", "title": "Join a project", "href": "/projects",
         "body": "Share memories with your team.", "done": bool(access.human_projects)},
        {"id": "curation_rule", "title": "Add a personal curation rule", "href": "/curation",
         "body": "Block or flag what your agents should not store.", "done": has_rule},
    ]
    state = await db.get(OnboardingState, (tenant, user))
    done = sum(1 for s in steps if s["done"])
    return {"steps": steps, "done": done, "total": len(steps),
            "dismissed": bool(state and state.dismissed_at),
            "dismissed_at": state.dismissed_at.isoformat() if state and state.dismissed_at else None}


@router.get("/api/me/onboarding")
async def get_onboarding(db: DbDep, access: AccessDep):
    _require_user(access)
    return await _onboarding(db, access)


@router.post("/api/me/onboarding/dismiss")
async def dismiss_onboarding(db: DbDep, access: AccessDep, dismissed: bool = True):
    _require_user(access)
    key = (access.principal.tenant_id, access.principal.username)
    state = await db.get(OnboardingState, key, with_for_update=True)
    if state is None:
        state = OnboardingState(tenant_id=key[0], user_id=key[1])
        db.add(state)
    state.dismissed_at = datetime.now(UTC) if dismissed else None
    await db.commit()
    return await _onboarding(db, access)
