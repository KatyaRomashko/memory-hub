"""Dashboard endpoints for the redesigned UI: overview, memory browser,
memory detail, graph neighbourhood and the search playground.

All reads are tenant-scoped by ``settings.ui_tenant_id``. Writes go through
``memoryhub_core`` services (same curation, versioning and audit semantics as
the MCP tools) and every dashboard action is appended to ``audit_log``.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections import Counter, defaultdict
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from memoryhub_core.models.audit import AuditLog
from memoryhub_core.models.campaign import CampaignMembership
from memoryhub_core.models.contradiction import ContradictionReport
from memoryhub_core.models.memory import MemoryNode, MemoryRelationship
from memoryhub_core.models.project import ProjectMembership
from memoryhub_core.models.role import RoleAssignment
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, exists, func, literal_column, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.config import Settings, get_settings
from src.database import get_db
from src.ui_common import (
    SCOPES,
    SEARCH_EVENTS,
    WRITE_EVENTS,
    audit,
    clean_stub,
    cosine,
    embed,
    live_filters,
    load_tenant_settings,
    now_utc,
    parse_uuid,
    preview,
    probe_components,
    rerank,
    viewer_from_request,
)

logger = logging.getLogger(__name__)
router = APIRouter()

DbDep = Annotated[AsyncSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

TOOL_NAMES = {
    "project.create": "manage_project",
    "project.update": "manage_project",
    "project.member_added": "manage_project",
    "project.member_removed": "manage_project",
    "campaign.create": "manage_campaign",
    "campaign.update": "manage_campaign",
    "campaign.enroll": "manage_campaign",
    "role.assign": "assign_role",
    "settings.update": "update_settings",
    "curation.batch_scan": "curation_scan",
    "curation.scan_match": "curation_scan",
    "review.approve": "approve",
    "review.reject": "reject",
    "review.request_changes": "request_changes",
    "review.restore": "restore",
    "review.promotion_proposed": "promote",
    "memory.write": "write_memory",
    "memory.update": "update_memory",
    "memory.read": "read_memory",
    "memory.search": "search_memory",
    "memory.delete": "delete_memory",
    "memory.contradiction_reported": "report_contradiction",
    "memory.relationship_created": "create_relationship",
    "session.registered": "register_session",
    "session.denied": "register_session",
    "admin.quarantine": "quarantine",
    "admin.restore": "restore",
    "admin.hard_delete": "hard_delete",
}


def tool_name(event_type: str) -> str:
    if event_type in TOOL_NAMES:
        return TOOL_NAMES[event_type]
    return event_type.split(".", 1)[-1]


async def _auth_clients(settings: Settings) -> list[dict] | None:
    """Clients from the auth service, or None when it is unreachable."""
    from src.routes import _admin_request

    try:
        resp = await _admin_request(settings, "GET", "/admin/clients", tenant_id=settings.ui_tenant_id)
        data = resp.json()
        return data if isinstance(data, list) else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Viewer
# ---------------------------------------------------------------------------


@router.get("/api/me")
async def me(request: Request, settings: SettingsDep):
    viewer = viewer_from_request(request, settings)
    return {
        "username": viewer.username,
        "name": viewer.name,
        "email": viewer.email,
        "initials": viewer.initials,
        "source": viewer.source,
        "tenant_id": settings.ui_tenant_id,
    }


# ---------------------------------------------------------------------------
# Overview (screen 01)
# ---------------------------------------------------------------------------


def _event_row(event: AuditLog, memory: MemoryNode | None, identity_types: dict[str, str]) -> dict:
    meta = event.event_metadata or {}
    return {
        "id": event.id,
        "timestamp": event.timestamp,
        "event_type": event.event_type,
        "operation": tool_name(event.event_type),
        "actor_id": event.actor_id,
        "driver_id": event.driver_id,
        "actor_type": identity_types.get(event.actor_id),
        "scope": event.scope,
        "owner_id": event.owner_id,
        "memory_id": str(event.memory_id) if event.memory_id else None,
        "memory_stub": clean_stub(memory.stub) if memory else None,
        "memory_version": memory.version if memory else None,
        "decision": event.decision,
        "metadata": meta,
    }


async def _recent_events(db: AsyncSession, tenant_id: str, limit: int) -> list[tuple[AuditLog, MemoryNode | None]]:
    stmt = (
        select(AuditLog, MemoryNode)
        .outerjoin(MemoryNode, and_(MemoryNode.id == AuditLog.memory_id, MemoryNode.tenant_id == tenant_id))
        .where(AuditLog.tenant_id == tenant_id)
        .order_by(AuditLog.timestamp.desc(), AuditLog.id.desc())
        .limit(limit)
    )
    return [(row[0], row[1]) for row in (await db.execute(stmt)).all()]


@router.get("/api/overview")
async def overview(db: DbDep, settings: SettingsDep):
    tenant_id = settings.ui_tenant_id
    now = now_utc()
    tenant_settings = await load_tenant_settings(db, tenant_id)
    threshold = int(tenant_settings["governance"]["contradiction_threshold"])
    base = live_filters(tenant_id) + [MemoryNode.parent_id.is_(None)]

    total = (await db.execute(select(func.count()).select_from(MemoryNode).where(*base))).scalar_one()
    new_7d = (
        await db.execute(
            select(func.count()).select_from(MemoryNode).where(*base, MemoryNode.created_at >= now - timedelta(days=7))
        )
    ).scalar_one()
    week_ago_total = total - new_7d
    growth_pct = round(new_7d / week_ago_total * 100, 1) if week_ago_total > 0 else None

    scope_rows = (
        await db.execute(select(MemoryNode.scope, func.count()).where(*base).group_by(MemoryNode.scope))
    ).all()
    scope_counts = {row[0]: row[1] for row in scope_rows}
    pending_rows = (
        await db.execute(
            select(MemoryNode.scope, func.count())
            .where(*base, MemoryNode.status == "pending")
            .group_by(MemoryNode.scope)
        )
    ).all()
    pending = {row[0]: row[1] for row in pending_rows}
    scopes = [{"scope": s, "count": scope_counts.get(s, 0), "pending": pending.get(s, 0)} for s in SCOPES] + [
        {"scope": s, "count": c, "pending": pending.get(s, 0)} for s, c in scope_counts.items() if s not in SCOPES
    ]

    # Identities: OAuth clients when the auth service answers, otherwise the
    # distinct writers seen in memory.
    clients = await _auth_clients(settings)
    active_since = now - timedelta(minutes=15)
    active_now = (
        await db.execute(
            select(func.count(func.distinct(AuditLog.actor_id))).where(
                AuditLog.tenant_id == tenant_id, AuditLog.timestamp >= active_since
            )
        )
    ).scalar_one()
    if clients is not None:
        agents_total = sum(1 for c in clients if c.get("active", True))
        agents_source = "auth"
    else:
        agents_total = (
            await db.execute(
                select(func.count(func.distinct(func.coalesce(MemoryNode.actor_id, MemoryNode.owner_id)))).where(
                    *live_filters(tenant_id)
                )
            )
        ).scalar_one()
        agents_source = "memory"

    day_ago = now - timedelta(hours=24)
    searches_24h = (
        await db.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.tenant_id == tenant_id,
                AuditLog.event_type.in_(SEARCH_EVENTS),
                AuditLog.timestamp >= day_ago,
            )
        )
    ).scalar_one()
    p95 = None
    try:
        p95 = (
            await db.execute(
                text(
                    """
                    SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY (metadata->>'latency_ms')::float)
                    FROM audit_log
                    WHERE tenant_id = :t AND event_type = 'memory.search' AND timestamp >= :since
                      AND metadata ? 'latency_ms'
                    """
                ),
                {"t": tenant_id, "since": day_ago},
            )
        ).scalar_one()
    except Exception:
        await db.rollback()

    open_contradictions = (
        await db.execute(
            select(func.count())
            .select_from(ContradictionReport)
            .join(MemoryNode, MemoryNode.id == ContradictionReport.memory_id)
            .where(
                ContradictionReport.tenant_id == tenant_id,
                ContradictionReport.resolved.is_(False),
                MemoryNode.deleted_at.is_(None),
            )
        )
    ).scalar_one()
    per_memory = (
        select(ContradictionReport.memory_id, func.count().label("n"))
        .join(MemoryNode, MemoryNode.id == ContradictionReport.memory_id)
        .where(
            ContradictionReport.tenant_id == tenant_id,
            ContradictionReport.resolved.is_(False),
            MemoryNode.deleted_at.is_(None),
        )
        .group_by(ContradictionReport.memory_id)
        .subquery()
    )
    over_threshold = (
        await db.execute(select(func.count()).select_from(per_memory).where(per_memory.c.n >= threshold))
    ).scalar_one()

    # 14-day activity from the audit log; fall back to memory timestamps when
    # the MCP server has not written audit events yet.
    start = (now - timedelta(days=13)).replace(hour=0, minute=0, second=0, microsecond=0)
    day_col = func.date_trunc("day", AuditLog.timestamp)
    activity_rows = (
        await db.execute(
            select(
                day_col.label("day"),
                func.count().filter(AuditLog.event_type.in_(SEARCH_EVENTS)),
                func.count().filter(AuditLog.event_type.in_(WRITE_EVENTS)),
            )
            .where(AuditLog.tenant_id == tenant_id, AuditLog.timestamp >= start)
            .group_by(day_col)
        )
    ).all()
    activity_source = "audit"
    by_day: dict[str, tuple[int, int]] = {row[0].date().isoformat(): (row[1], row[2]) for row in activity_rows}
    if not any(w or s for s, w in by_day.values()):
        mem_day = func.date_trunc("day", MemoryNode.created_at)
        mem_rows = (
            await db.execute(
                select(mem_day, func.count())
                .where(MemoryNode.tenant_id == tenant_id, MemoryNode.created_at >= start)
                .group_by(mem_day)
            )
        ).all()
        by_day = {row[0].date().isoformat(): (0, row[1]) for row in mem_rows}
        activity_source = "memory"
    activity = []
    for i in range(14):
        day = (start + timedelta(days=i)).date().isoformat()
        searches, writes = by_day.get(day, (0, 0))
        activity.append({"date": day, "searches": searches, "writes": writes})

    identity_types = {c["client_id"]: c.get("identity_type", "user") for c in (clients or [])}
    recent = [_event_row(e, m, identity_types) for e, m in await _recent_events(db, tenant_id, 6)]
    recent_source = "audit"
    if not recent and total:
        nodes = (
            (await db.execute(select(MemoryNode).where(*base).order_by(MemoryNode.updated_at.desc()).limit(6)))
            .scalars()
            .all()
        )
        recent = [
            {
                "id": str(n.id),
                "timestamp": n.updated_at,
                "event_type": "memory.write" if n.version == 1 else "memory.update",
                "operation": "write_memory" if n.version == 1 else "update_memory",
                "actor_id": n.actor_id or n.owner_id,
                "driver_id": n.driver_id,
                "actor_type": identity_types.get(n.actor_id or n.owner_id),
                "scope": n.scope,
                "owner_id": n.owner_id,
                "memory_id": str(n.id),
                "memory_stub": n.stub,
                "memory_version": n.version,
                "decision": "allowed",
                "metadata": {},
            }
            for n in nodes
        ]
        recent_source = "memory"

    health = await probe_components(db, settings)
    curator_last = (
        await db.execute(
            select(func.max(AuditLog.timestamp)).where(
                AuditLog.tenant_id == tenant_id, AuditLog.actor_id.ilike("%curator%")
            )
        )
    ).scalar_one()
    health.insert(
        6,
        {
            "key": "curator",
            "name": "Curator agents",
            "status": "healthy" if curator_last and curator_last >= now - timedelta(hours=24) else "idle",
            "detail": None if curator_last else "No curator activity recorded",
            "latency_ms": None,
        },
    )

    ctx = None
    try:
        ctx = (
            await db.execute(
                text(
                    """
                    SELECT avg((metadata->>'response_tokens')::float),
                           avg(CASE WHEN (metadata->>'cache_prefix_hit')::boolean THEN 1.0 ELSE 0.0 END)
                             FILTER (WHERE metadata ? 'cache_prefix_hit')
                    FROM audit_log
                    WHERE tenant_id = :t AND event_type = 'memory.search' AND timestamp >= :since
                      AND (metadata ? 'response_tokens' OR metadata ? 'cache_prefix_hit')
                    """
                ),
                {"t": tenant_id, "since": now - timedelta(days=7)},
            )
        ).first()
    except Exception:
        await db.rollback()

    return {
        "tenant_id": tenant_id,
        "generated_at": now,
        "memories": {"current": total, "new_7d": new_7d, "growth_pct": growth_pct},
        "agents": {"total": agents_total, "active_now": active_now, "source": agents_source},
        "searches": {"last_24h": searches_24h, "p95_latency_ms": round(p95) if p95 else None},
        "contradictions": {"open": open_contradictions, "over_threshold": over_threshold, "threshold": threshold},
        "approvals": {"enterprise_pending": pending.get("enterprise", 0), "pending_total": sum(pending.values())},
        "scopes": scopes,
        "activity": activity,
        "activity_source": activity_source,
        "recent": recent,
        "recent_source": recent_source,
        "health": health,
        "context_assembly": {
            "avg_tokens": round(ctx[0]) if ctx and ctx[0] is not None else None,
            "prefix_hit_rate": round(ctx[1], 3) if ctx and ctx[1] is not None else None,
        },
    }


@router.get("/api/nav-counts")
async def nav_counts(db: DbDep, settings: SettingsDep):
    """Small payload for the sidebar badge: items waiting in the review queue."""
    tenant_id = settings.ui_tenant_id
    tenant_settings = await load_tenant_settings(db, tenant_id)
    queue = await _review_counts(db, tenant_id, int(tenant_settings["governance"]["contradiction_threshold"]))
    memories = (
        await db.execute(
            select(func.count()).select_from(MemoryNode).where(*live_filters(tenant_id), MemoryNode.parent_id.is_(None))
        )
    ).scalar_one()
    return {"review_queue": sum(queue.values()), "memories": memories}


async def _review_counts(db: AsyncSession, tenant_id: str, threshold: int) -> dict[str, int]:
    pending_rows = (
        await db.execute(
            select(MemoryNode.scope, func.count())
            .where(*live_filters(tenant_id), MemoryNode.status == "pending")
            .group_by(MemoryNode.scope)
        )
    ).all()
    approvals = sum(c for scope, c in pending_rows if scope == "enterprise")
    promotions = sum(c for scope, c in pending_rows if scope != "enterprise")
    per_memory = (
        select(ContradictionReport.memory_id)
        .join(MemoryNode, MemoryNode.id == ContradictionReport.memory_id)
        .where(
            ContradictionReport.tenant_id == tenant_id,
            ContradictionReport.resolved.is_(False),
            MemoryNode.deleted_at.is_(None),
        )
        .group_by(ContradictionReport.memory_id)
        .having(func.count() >= threshold)
        .subquery()
    )
    contradictions = (await db.execute(select(func.count()).select_from(per_memory))).scalar_one()
    quarantine = (
        await db.execute(
            select(func.count())
            .select_from(MemoryNode)
            .where(
                MemoryNode.tenant_id == tenant_id,
                MemoryNode.deleted_at.is_(None),
                MemoryNode.status == "quarantined",
            )
        )
    ).scalar_one()
    return {
        "approvals": approvals,
        "promotions": promotions,
        "contradictions": contradictions,
        "quarantine": quarantine,
    }


# ---------------------------------------------------------------------------
# Memory browser (screen 02)
# ---------------------------------------------------------------------------

_BROWSE_SORTS = {
    "updated": MemoryNode.updated_at,
    "created": MemoryNode.created_at,
    "weight": MemoryNode.weight,
    "version": MemoryNode.version,
}
_SEMANTIC_LIMIT = 200
_NEAR_DUP = 0.90


def _tab_filters(tenant_id: str, tab: str) -> list:
    roots = [MemoryNode.tenant_id == tenant_id, MemoryNode.parent_id.is_(None)]
    if tab == "all":
        return roots + [MemoryNode.deleted_at.is_(None)]
    if tab == "quarantined":
        return roots + [MemoryNode.deleted_at.is_(None), MemoryNode.status == "quarantined"]
    if tab == "deleted":
        newer = aliased(MemoryNode)
        return roots + [
            MemoryNode.deleted_at.is_not(None),
            ~exists().where(newer.previous_version_id == MemoryNode.id),
        ]
    return roots + [
        MemoryNode.deleted_at.is_(None),
        MemoryNode.is_current.is_(True),
        MemoryNode.status != "quarantined",
    ]


def _contradicted_clause(tenant_id: str):
    return exists().where(
        ContradictionReport.memory_id == MemoryNode.id,
        ContradictionReport.tenant_id == tenant_id,
        ContradictionReport.resolved.is_(False),
    )


def _has_branches_clause():
    child = aliased(MemoryNode)
    return exists().where(child.parent_id == MemoryNode.id, child.deleted_at.is_(None))


def _near_dup_clause(tenant_id: str):
    other = aliased(MemoryNode)
    return exists().where(
        other.tenant_id == tenant_id,
        other.id != MemoryNode.id,
        other.is_current.is_(True),
        other.deleted_at.is_(None),
        other.parent_id.is_(None),
        other.embedding.is_not(None),
        MemoryNode.embedding.is_not(None),
        other.embedding.cosine_distance(MemoryNode.embedding) <= 1 - _NEAR_DUP,
    )


@router.get("/api/memories/browse")
async def browse_memories(
    db: DbDep,
    settings: SettingsDep,
    tab: str = Query(default="current", pattern="^(current|all|quarantined|deleted)$"),
    q: str | None = Query(default=None, max_length=1000),
    mode: str = Query(default="semantic", pattern="^(semantic|text)$"),
    scope: list[str] | None = Query(default=None),
    actor: list[str] | None = Query(default=None),
    project: str | None = Query(default=None, max_length=255),
    domain: list[str] | None = Query(default=None),
    min_weight: float | None = Query(default=None, ge=0, le=1),
    flag: list[str] | None = Query(default=None),
    sort: str = Query(default="updated", pattern="^(relevance|updated|created|weight|version)$"),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
):
    tenant_id = settings.ui_tenant_id
    base = _tab_filters(tenant_id, tab)
    filters = list(base)
    if scope:
        filters.append(MemoryNode.scope.in_(scope))
    if actor:
        filters.append(
            or_(MemoryNode.actor_id.in_(actor), and_(MemoryNode.actor_id.is_(None), MemoryNode.owner_id.in_(actor)))
        )
    if project:
        filters.append(
            or_(
                and_(MemoryNode.scope == "project", MemoryNode.scope_id == project),
                MemoryNode.metadata_["project"].as_string() == project,
            )
        )
    if domain:
        filters.append(MemoryNode.domains.overlap(domain))
    if min_weight is not None:
        filters.append(MemoryNode.weight >= min_weight)
    flags = set(flag or [])
    if "contradicted" in flags:
        filters.append(_contradicted_clause(tenant_id))
    if "has_branches" in flags:
        filters.append(_has_branches_clause())
    if "near_duplicate" in flags:
        filters.append(_near_dup_clause(tenant_id))

    query = (q or "").strip()
    search_mode = "none"
    scores: dict[uuid.UUID, float] = {}
    if query:
        vector = await embed(query, settings.embedding_url) if mode == "semantic" else None
        if vector is not None:
            distance = MemoryNode.embedding.cosine_distance(vector)
            ranked = (
                await db.execute(
                    select(MemoryNode.id, (1 - distance).label("score"))
                    .where(*filters, MemoryNode.embedding.is_not(None))
                    .order_by(distance)
                    .limit(_SEMANTIC_LIMIT)
                )
            ).all()
            scores = {row[0]: float(row[1]) for row in ranked}
            filters.append(MemoryNode.id.in_(list(scores) or [uuid.uuid4()]))
            search_mode = "semantic"
        else:
            pattern = f"%{query}%"
            filters.append(or_(MemoryNode.content.ilike(pattern), MemoryNode.stub.ilike(pattern)))
            search_mode = "text"

    total = (await db.execute(select(func.count()).select_from(MemoryNode).where(*filters))).scalar_one()

    descending = order == "desc"
    if sort == "relevance" and scores:
        ordered_ids = sorted(scores, key=lambda k: scores[k], reverse=descending)
        page_ids = ordered_ids[(page - 1) * per_page : page * per_page]
        rows = (
            (await db.execute(select(MemoryNode).where(MemoryNode.id.in_(page_ids or [uuid.uuid4()])))).scalars().all()
        )
        by_id = {r.id: r for r in rows}
        nodes = [by_id[i] for i in page_ids if i in by_id]
    else:
        column = _BROWSE_SORTS.get(sort, MemoryNode.updated_at)
        nodes = (
            (
                await db.execute(
                    select(MemoryNode)
                    .where(*filters)
                    .order_by(column.desc() if descending else column.asc(), MemoryNode.id)
                    .offset((page - 1) * per_page)
                    .limit(per_page)
                )
            )
            .scalars()
            .all()
        )

    ids = [n.id for n in nodes]
    branch_info: dict[uuid.UUID, list[str]] = defaultdict(list)
    contradiction_counts: dict[uuid.UUID, int] = {}
    near_dup: dict[uuid.UUID, float] = {}
    if ids:
        for parent_id, branch_type in (
            await db.execute(
                select(MemoryNode.parent_id, MemoryNode.branch_type).where(
                    MemoryNode.parent_id.in_(ids), MemoryNode.deleted_at.is_(None)
                )
            )
        ).all():
            branch_info[parent_id].append(branch_type or "branch")
        contradiction_counts = {
            row[0]: row[1]
            for row in (
                await db.execute(
                    select(ContradictionReport.memory_id, func.count())
                    .where(
                        ContradictionReport.memory_id.in_(ids),
                        ContradictionReport.tenant_id == tenant_id,
                        ContradictionReport.resolved.is_(False),
                    )
                    .group_by(ContradictionReport.memory_id)
                )
            ).all()
        }
        try:
            dup_rows = (
                await db.execute(
                    text(
                        """
                        SELECT m.id, nn.score
                        FROM memory_nodes m
                        CROSS JOIN LATERAL (
                          SELECT 1 - (o.embedding <=> m.embedding) AS score
                          FROM memory_nodes o
                          WHERE o.tenant_id = :t AND o.id <> m.id AND o.is_current
                            AND o.deleted_at IS NULL AND o.parent_id IS NULL AND o.embedding IS NOT NULL
                          ORDER BY o.embedding <=> m.embedding
                          LIMIT 1
                        ) nn
                        WHERE m.id = ANY(:ids) AND m.embedding IS NOT NULL
                        """
                    ),
                    {"t": tenant_id, "ids": ids},
                )
            ).all()
            near_dup = {row[0]: float(row[1]) for row in dup_rows if row[1] is not None and row[1] >= _NEAR_DUP}
        except Exception as exc:
            logger.debug("near-duplicate lookup failed: %s", exc)
            await db.rollback()

    items = []
    for n in nodes:
        branches = branch_info.get(n.id, [])
        counts = Counter(branches)
        items.append(
            {
                "id": str(n.id),
                "stub": clean_stub(n.stub),
                "content_preview": preview(n.content),
                "scope": n.scope,
                "scope_id": n.scope_id,
                "owner_id": n.owner_id,
                "actor_id": n.actor_id,
                "driver_id": n.driver_id,
                "domains": list(n.domains or []),
                "weight": n.weight,
                "version": n.version,
                "is_current": n.is_current,
                "status": n.status,
                "content_type": n.content_type,
                "branch_count": len(branches),
                "branch_summary": [{"type": t, "count": c} for t, c in counts.most_common()],
                "open_contradictions": contradiction_counts.get(n.id, 0),
                "near_duplicate": round(near_dup[n.id], 2) if n.id in near_dup else None,
                "created_at": n.created_at,
                "updated_at": n.updated_at,
                "deleted_at": n.deleted_at,
                "score": round(scores[n.id], 4) if n.id in scores else None,
            }
        )

    # Tab counts and facets (over the tab base, independent of facet filters).
    tab_counts = {}
    for t in ("current", "all", "quarantined", "deleted"):
        tab_counts[t] = (
            await db.execute(select(func.count()).select_from(MemoryNode).where(*_tab_filters(tenant_id, t)))
        ).scalar_one()
    facet_scope = {
        row[0]: row[1]
        for row in (
            await db.execute(select(MemoryNode.scope, func.count()).where(*base).group_by(MemoryNode.scope))
        ).all()
    }
    writer = func.coalesce(MemoryNode.actor_id, MemoryNode.owner_id)
    writer_rows = (
        await db.execute(select(writer, func.count()).where(*base).group_by(writer).order_by(func.count().desc()))
    ).all()
    project_rows = (
        await db.execute(
            select(MemoryNode.scope_id, func.count())
            .where(*base, MemoryNode.scope == "project", MemoryNode.scope_id.is_not(None))
            .group_by(MemoryNode.scope_id)
            .order_by(func.count().desc())
        )
    ).all()
    domain_col = func.unnest(MemoryNode.domains).label("d")
    domain_sub = select(domain_col).where(*base).subquery()
    domain_rows = (
        await db.execute(
            select(domain_sub.c.d, func.count()).group_by(domain_sub.c.d).order_by(func.count().desc()).limit(40)
        )
    ).all()
    contradicted = (
        await db.execute(select(func.count()).select_from(MemoryNode).where(*base, _contradicted_clause(tenant_id)))
    ).scalar_one()
    with_branches = (
        await db.execute(select(func.count()).select_from(MemoryNode).where(*base, _has_branches_clause()))
    ).scalar_one()
    near_dup_count = None
    if tab_counts["current"] <= 5000:
        try:
            near_dup_count = (
                await db.execute(select(func.count()).select_from(MemoryNode).where(*base, _near_dup_clause(tenant_id)))
            ).scalar_one()
        except Exception:
            await db.rollback()

    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "search_mode": search_mode,
        "tab_counts": tab_counts,
        "facets": {
            "scopes": [{"value": s, "count": facet_scope.get(s, 0)} for s in SCOPES]
            + [{"value": s, "count": c} for s, c in facet_scope.items() if s not in SCOPES],
            "writers": [{"value": r[0], "count": r[1]} for r in writer_rows],
            "projects": [{"value": r[0], "count": r[1]} for r in project_rows],
            "domains": [{"value": r[0], "count": r[1]} for r in domain_rows],
            "flags": {"contradicted": contradicted, "has_branches": with_branches, "near_duplicate": near_dup_count},
        },
    }


@router.get("/api/memories/export")
async def export_memories(
    db: DbDep,
    settings: SettingsDep,
    scope: list[str] | None = Query(default=None),
):
    """JSONL export of current memories (the browser's "Export JSONL")."""
    from fastapi.responses import StreamingResponse

    filters = _tab_filters(settings.ui_tenant_id, "current")
    if scope:
        filters.append(MemoryNode.scope.in_(scope))
    nodes = (
        (await db.execute(select(MemoryNode).where(*filters).order_by(MemoryNode.updated_at.desc()))).scalars().all()
    )

    def lines():
        for n in nodes:
            yield (
                json.dumps(
                    {
                        "id": str(n.id),
                        "scope": n.scope,
                        "scope_id": n.scope_id,
                        "owner_id": n.owner_id,
                        "actor_id": n.actor_id,
                        "weight": n.weight,
                        "version": n.version,
                        "domains": list(n.domains or []),
                        "content": n.content,
                        "metadata": n.metadata_,
                        "updated_at": n.updated_at.isoformat() if n.updated_at else None,
                    },
                    default=str,
                )
                + "\n"
            )

    return StreamingResponse(
        lines(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="memoryhub-{settings.ui_tenant_id}.jsonl"'},
    )


# ---------------------------------------------------------------------------
# Memory detail (screen 03)
# ---------------------------------------------------------------------------


async def _load_memory(db: AsyncSession, tenant_id: str, memory_id: str) -> MemoryNode:
    parsed = parse_uuid(memory_id, "memory ID")
    node = (
        await db.execute(select(MemoryNode).where(MemoryNode.id == parsed, MemoryNode.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if node is None:
        raise HTTPException(status_code=404, detail=f"Memory {memory_id!r} not found")
    return node


def _node_brief(n: MemoryNode) -> dict:
    return {
        "id": str(n.id),
        "stub": clean_stub(n.stub),
        "scope": n.scope,
        "scope_id": n.scope_id,
        "owner_id": n.owner_id,
        "weight": n.weight,
        "version": n.version,
        "is_current": n.is_current,
        "status": n.status,
        "created_at": n.created_at,
        "updated_at": n.updated_at,
    }


@router.get("/api/memory/{memory_id}/context")
async def memory_context(memory_id: str, db: DbDep, settings: SettingsDep):
    tenant_id = settings.ui_tenant_id
    node = await _load_memory(db, tenant_id, memory_id)
    tenant_settings = await load_tenant_settings(db, tenant_id)

    # Versions share a logical_id; fall back to walking the chain.
    logical_id = getattr(node, "logical_id", None)
    if logical_id is not None:
        versions = (
            (
                await db.execute(
                    select(MemoryNode)
                    .where(
                        MemoryNode.tenant_id == tenant_id,
                        MemoryNode.logical_id == logical_id,
                        MemoryNode.parent_id.is_(None),
                    )
                    .order_by(MemoryNode.version.desc())
                )
            )
            .scalars()
            .all()
        )
    else:
        versions = [node]
    version_ids = [v.id for v in versions] or [node.id]

    branches = (
        (
            await db.execute(
                select(MemoryNode)
                .where(
                    MemoryNode.parent_id == node.id, MemoryNode.tenant_id == tenant_id, MemoryNode.deleted_at.is_(None)
                )
                .order_by(MemoryNode.created_at)
            )
        )
        .scalars()
        .all()
    )

    rels = (
        (
            await db.execute(
                select(MemoryRelationship).where(
                    MemoryRelationship.tenant_id == tenant_id,
                    MemoryRelationship.valid_until.is_(None),
                    or_(MemoryRelationship.source_id == node.id, MemoryRelationship.target_id == node.id),
                )
            )
        )
        .scalars()
        .all()
    )
    other_ids = {r.target_id if r.source_id == node.id else r.source_id for r in rels}
    others = {}
    if other_ids:
        others = {
            o.id: o
            for o in (
                await db.execute(
                    select(MemoryNode).where(MemoryNode.id.in_(list(other_ids)), MemoryNode.tenant_id == tenant_id)
                )
            )
            .scalars()
            .all()
        }
    relationships, omitted = [], 0
    for r in rels:
        outgoing = r.source_id == node.id
        other = others.get(r.target_id if outgoing else r.source_id)
        if other is None or other.deleted_at is not None:
            omitted += 1
            continue
        relationships.append(
            {
                "id": str(r.id),
                "type": r.relationship_type,
                "direction": "out" if outgoing else "in",
                "created_by": r.created_by,
                "created_at": r.created_at,
                "other": _node_brief(other),
            }
        )

    reports = (
        (
            await db.execute(
                select(ContradictionReport)
                .where(ContradictionReport.tenant_id == tenant_id, ContradictionReport.memory_id.in_(version_ids))
                .order_by(ContradictionReport.resolved, ContradictionReport.created_at.desc())
            )
        )
        .scalars()
        .all()
    )

    similar = []
    if node.embedding is not None:
        distance = MemoryNode.embedding.cosine_distance(node.embedding)
        rows = (
            await db.execute(
                select(MemoryNode, (1 - distance).label("score"))
                .where(
                    *live_filters(tenant_id),
                    MemoryNode.parent_id.is_(None),
                    MemoryNode.id.not_in(version_ids),
                    MemoryNode.embedding.is_not(None),
                )
                .order_by(distance)
                .limit(5)
            )
        ).all()
        similar = [{**_node_brief(row[0]), "score": round(float(row[1]), 3)} for row in rows if row[1] >= 0.5]

    access_count = (
        await db.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.tenant_id == tenant_id, AuditLog.memory_id.in_(version_ids))
        )
    ).scalar_one()

    dims = None
    if node.embedding is not None:
        try:
            dims = len(node.embedding)
        except TypeError:
            dims = None

    full_threshold = float(tenant_settings["retrieval"]["full_content_threshold"])
    return {
        "memory": {
            **_node_brief(node),
            "content": node.content,
            "logical_id": str(logical_id) if logical_id else None,
            "parent_id": str(node.parent_id) if node.parent_id else None,
            "branch_type": node.branch_type,
            "actor_id": node.actor_id,
            "driver_id": node.driver_id,
            "tenant_id": node.tenant_id,
            "domains": list(node.domains or []),
            "content_type": node.content_type,
            "source": node.source,
            "metadata": node.metadata_,
            "created_at": node.created_at,
            "updated_at": node.updated_at,
            "deleted_at": node.deleted_at,
            "expires_at": node.expires_at,
            "embedding_dims": dims,
            "injection": "full" if node.weight >= full_threshold else "stub",
            "full_content_threshold": full_threshold,
        },
        "versions": [
            {
                "id": str(v.id),
                "version": v.version,
                "is_current": v.is_current,
                "content": v.content,
                "weight": v.weight,
                "actor_id": v.actor_id,
                "driver_id": v.driver_id,
                "owner_id": v.owner_id,
                "created_at": v.created_at,
                "deleted_at": v.deleted_at,
            }
            for v in versions
        ],
        "branches": [
            {
                "id": str(b.id),
                "branch_type": b.branch_type,
                "content": b.content,
                "actor_id": b.actor_id or b.owner_id,
                "created_at": b.created_at,
            }
            for b in branches
        ],
        "relationships": relationships,
        "relationships_omitted": omitted,
        "contradictions": [
            {
                "id": str(c.id),
                "observed_behavior": c.observed_behavior,
                "confidence": c.confidence,
                "reporter": c.reporter,
                "created_at": c.created_at,
                "resolved": c.resolved,
            }
            for c in reports
        ],
        "contradiction_threshold": int(tenant_settings["governance"]["contradiction_threshold"]),
        "similar": similar,
        "access_count": access_count,
    }


@router.get("/api/memory/{memory_id}/access-log")
async def memory_access_log(
    memory_id: str,
    db: DbDep,
    settings: SettingsDep,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=1, le=100),
):
    tenant_id = settings.ui_tenant_id
    node = await _load_memory(db, tenant_id, memory_id)
    ids = [node.id]
    if getattr(node, "logical_id", None):
        ids = list(
            (await db.execute(select(MemoryNode.id).where(MemoryNode.logical_id == node.logical_id))).scalars().all()
        )
    where = [AuditLog.tenant_id == tenant_id, AuditLog.memory_id.in_(ids)]
    total = (await db.execute(select(func.count()).select_from(AuditLog).where(*where))).scalar_one()
    events = (
        (
            await db.execute(
                select(AuditLog)
                .where(*where)
                .order_by(AuditLog.timestamp.desc())
                .offset((page - 1) * per_page)
                .limit(per_page)
            )
        )
        .scalars()
        .all()
    )
    return {"total": total, "items": [_event_row(e, None, {}) for e in events]}


# ---------------------------------------------------------------------------
# Memory writes (dashboard actions)
# ---------------------------------------------------------------------------


class WriteMemoryRequest(BaseModel):
    content: str = Field(min_length=1)
    scope: str = "user"
    weight: float = Field(default=0.7, ge=0, le=1)
    owner_id: str | None = None
    scope_id: str | None = None
    domains: list[str] | None = None
    content_type: str = "experiential"


class NewVersionRequest(BaseModel):
    content: str | None = Field(default=None, min_length=1)
    weight: float | None = Field(default=None, ge=0, le=1)
    domains: list[str] | None = None


class BranchRequest(BaseModel):
    branch_type: str = Field(min_length=1, max_length=50)
    content: str = Field(min_length=1)


class ContradictionRequest(BaseModel):
    observed_behavior: str = Field(min_length=1)
    confidence: float = Field(default=0.7, ge=0, le=1)


class RelationshipRequest(BaseModel):
    source_id: str
    target_id: str
    relationship_type: str


class BulkRequest(BaseModel):
    action: str = Field(pattern="^(weight|delete|promote|relate)$")
    ids: list[str] = Field(min_length=1, max_length=200)
    weight: float | None = Field(default=None, ge=0, le=1)
    target_scope: str | None = None
    target_scope_id: str | None = None
    relationship_type: str | None = None
    target_id: str | None = None


def _embedding_service(settings: Settings):
    if not settings.embedding_url:
        raise HTTPException(
            status_code=503,
            detail=(
                "The embedding service is not configured (MEMORYHUB_EMBEDDING_URL), "
                "so the dashboard cannot write memories."
            ),
        )
    from memoryhub_core.services.embeddings import HttpEmbeddingService

    return HttpEmbeddingService(settings.embedding_url)


@router.post("/api/memories", status_code=201)
async def write_memory(body: WriteMemoryRequest, request: Request, db: DbDep, settings: SettingsDep):
    from memoryhub_core.models.schemas import MemoryNodeCreate
    from memoryhub_core.services.memory import create_memory

    viewer = viewer_from_request(request, settings)
    tenant_id = settings.ui_tenant_id
    owner = body.owner_id or (tenant_id if body.scope in ("organizational", "enterprise") else viewer.username)
    data = MemoryNodeCreate(
        content=body.content,
        scope=body.scope,
        weight=body.weight,
        owner_id=owner,
        actor_id=viewer.username,
        driver_id=viewer.username,
        scope_id=body.scope_id,
        domains=body.domains,
        content_type=body.content_type,
        source="import",
    )
    memory, curation = await create_memory(data, db, _embedding_service(settings), tenant_id=tenant_id)
    if memory is None:
        await audit(
            db,
            tenant_id=tenant_id,
            viewer=viewer,
            event_type="memory.write",
            scope=body.scope,
            owner_id=owner,
            memory_id=None,
            decision="denied",
            metadata={"reason": curation.get("reason"), "detail": curation.get("detail")},
        )
        await db.commit()
        raise HTTPException(status_code=409, detail=curation.get("detail") or "Blocked by a curation rule")
    if body.scope == "enterprise":
        # Enterprise memories never reach agents until approved (review queue).
        await db.execute(update(MemoryNode).where(MemoryNode.id == memory.id).values(status="pending"))
    await audit(
        db,
        tenant_id=tenant_id,
        viewer=viewer,
        event_type="memory.write",
        scope=body.scope,
        owner_id=owner,
        memory_id=memory.id,
    )
    await db.commit()
    return {"id": str(memory.id), "curation": {"flags": curation.get("flags", [])}}


@router.post("/api/memory/{memory_id}/versions", status_code=201)
async def new_version(memory_id: str, body: NewVersionRequest, request: Request, db: DbDep, settings: SettingsDep):
    from memoryhub_core.models.schemas import MemoryNodeUpdate
    from memoryhub_core.services.memory import update_memory

    viewer = viewer_from_request(request, settings)
    node = await _load_memory(db, settings.ui_tenant_id, memory_id)
    if not node.is_current or node.deleted_at is not None:
        raise HTTPException(status_code=409, detail="Only the current version can be edited")
    if body.content is None and body.weight is None and body.domains is None:
        raise HTTPException(status_code=422, detail="Nothing to change")
    updated = await update_memory(
        node.id,
        MemoryNodeUpdate(content=body.content, weight=body.weight, domains=body.domains),
        db,
        _embedding_service(settings),
        actor_id=viewer.username,
        driver_id=viewer.username,
    )
    await audit(
        db,
        tenant_id=settings.ui_tenant_id,
        viewer=viewer,
        event_type="memory.update",
        scope=node.scope,
        owner_id=node.owner_id,
        memory_id=updated.id,
        metadata={"from_version": node.version, "weight": body.weight},
    )
    await db.commit()
    return {"id": str(updated.id), "version": updated.version}


@router.post("/api/memory/{memory_id}/branches", status_code=201)
async def add_branch(memory_id: str, body: BranchRequest, request: Request, db: DbDep, settings: SettingsDep):
    from memoryhub_core.models.schemas import MemoryNodeCreate
    from memoryhub_core.services.memory import create_memory

    viewer = viewer_from_request(request, settings)
    node = await _load_memory(db, settings.ui_tenant_id, memory_id)
    data = MemoryNodeCreate(
        content=body.content,
        scope=node.scope,
        weight=node.weight,
        owner_id=node.owner_id,
        actor_id=viewer.username,
        driver_id=viewer.username,
        scope_id=node.scope_id,
        parent_id=node.id,
        branch_type=body.branch_type,
    )
    branch, curation = await create_memory(
        data, db, _embedding_service(settings), tenant_id=settings.ui_tenant_id, skip_curation=False, force=True
    )
    if branch is None:
        raise HTTPException(status_code=409, detail=curation.get("detail") or "Blocked by a curation rule")
    await audit(
        db,
        tenant_id=settings.ui_tenant_id,
        viewer=viewer,
        event_type="memory.write",
        scope=node.scope,
        owner_id=node.owner_id,
        memory_id=branch.id,
        metadata={"branch_type": body.branch_type, "parent": str(node.id)},
    )
    await db.commit()
    return {"id": str(branch.id)}


@router.post("/api/memory/{memory_id}/contradictions", status_code=201)
async def add_contradiction(
    memory_id: str, body: ContradictionRequest, request: Request, db: DbDep, settings: SettingsDep
):
    from memoryhub_core.services.memory import report_contradiction

    viewer = viewer_from_request(request, settings)
    node = await _load_memory(db, settings.ui_tenant_id, memory_id)
    count = await report_contradiction(node.id, body.observed_behavior, body.confidence, viewer.username, db)
    await audit(
        db,
        tenant_id=settings.ui_tenant_id,
        viewer=viewer,
        event_type="memory.contradiction_reported",
        scope=node.scope,
        owner_id=node.owner_id,
        memory_id=node.id,
        metadata={"confidence": body.confidence},
    )
    await db.commit()
    return {"unresolved": count}


async def _relate(db: AsyncSession, settings: Settings, viewer, source_id: str, target_id: str, rel_type: str) -> str:
    from memoryhub_core.models.schemas import RelationshipCreate
    from memoryhub_core.services.graph import create_relationship

    source = await _load_memory(db, settings.ui_tenant_id, source_id)
    target = await _load_memory(db, settings.ui_tenant_id, target_id)
    try:
        rel = await create_relationship(
            RelationshipCreate(
                source_id=source.id, target_id=target.id, relationship_type=rel_type, created_by=viewer.username
            ),
            db,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"Could not create relationship: {exc}") from None
    await audit(
        db,
        tenant_id=settings.ui_tenant_id,
        viewer=viewer,
        event_type="memory.relationship_created",
        scope=source.scope,
        owner_id=source.owner_id,
        memory_id=source.id,
        metadata={"type": rel_type, "target": str(target.id)},
    )
    return str(rel.id)


@router.post("/api/relationships", status_code=201)
async def create_relationship_route(body: RelationshipRequest, request: Request, db: DbDep, settings: SettingsDep):
    viewer = viewer_from_request(request, settings)
    rel_id = await _relate(db, settings, viewer, body.source_id, body.target_id, body.relationship_type)
    await db.commit()
    return {"id": rel_id}


PROMOTION_ORDER = ["user", "project", "campaign", "role", "organizational", "enterprise"]


async def _propose_promotion(
    db: AsyncSession, settings: Settings, viewer, node: MemoryNode, target_scope: str, target_scope_id: str | None
) -> uuid.UUID:
    if target_scope not in PROMOTION_ORDER or node.scope not in PROMOTION_ORDER:
        raise HTTPException(status_code=422, detail=f"Cannot promote {node.scope} → {target_scope}")
    if PROMOTION_ORDER.index(target_scope) <= PROMOTION_ORDER.index(node.scope):
        raise HTTPException(status_code=422, detail="Promotion must move to a broader scope")
    tenant_id = settings.ui_tenant_id
    new_id = uuid.uuid4()
    promoted = MemoryNode(
        id=new_id,
        logical_id=new_id,
        content=node.content,
        stub=node.stub,
        storage_type="inline",
        weight=node.weight,
        scope=target_scope,
        scope_id=target_scope_id,
        owner_id=target_scope_id or tenant_id,
        actor_id=viewer.username,
        driver_id=viewer.username,
        tenant_id=tenant_id,
        domains=node.domains,
        content_type=node.content_type,
        status="pending",
        source=node.source or "agent",
        is_current=True,
        version=1,
        embedding=node.embedding,
        metadata_={**(node.metadata_ or {}), "promoted_from": str(node.id), "proposed_by": viewer.username},
    )
    db.add(promoted)
    await db.flush()
    db.add(
        MemoryRelationship(
            source_id=new_id,
            target_id=node.id,
            relationship_type="derived_from",
            created_by=viewer.username,
            tenant_id=tenant_id,
        )
    )
    await audit(
        db,
        tenant_id=tenant_id,
        viewer=viewer,
        event_type="review.promotion_proposed",
        scope=target_scope,
        owner_id=promoted.owner_id,
        memory_id=new_id,
        metadata={"source": str(node.id), "from_scope": node.scope},
    )
    return new_id


@router.post("/api/memories/bulk")
async def bulk_action(body: BulkRequest, request: Request, db: DbDep, settings: SettingsDep):
    viewer = viewer_from_request(request, settings)
    tenant_id = settings.ui_tenant_id
    done: list[str] = []
    errors: list[dict] = []
    for raw_id in body.ids:
        try:
            node = await _load_memory(db, tenant_id, raw_id)
            if body.action == "weight":
                if body.weight is None:
                    raise HTTPException(status_code=422, detail="weight is required")
                from memoryhub_core.models.schemas import MemoryNodeUpdate
                from memoryhub_core.services.memory import update_memory

                updated = await update_memory(
                    node.id,
                    MemoryNodeUpdate(weight=body.weight),
                    db,
                    _embedding_service(settings),
                    actor_id=viewer.username,
                    driver_id=viewer.username,
                )
                await audit(
                    db,
                    tenant_id=tenant_id,
                    viewer=viewer,
                    event_type="memory.update",
                    scope=node.scope,
                    owner_id=node.owner_id,
                    memory_id=updated.id,
                    metadata={"weight": body.weight},
                )
                done.append(str(updated.id))
            elif body.action == "delete":
                from src.routes import delete_memory as delete_route

                await delete_route(raw_id, db, settings)
                await audit(
                    db,
                    tenant_id=tenant_id,
                    viewer=viewer,
                    event_type="memory.delete",
                    scope=node.scope,
                    owner_id=node.owner_id,
                    memory_id=node.id,
                )
                done.append(raw_id)
            elif body.action == "promote":
                new_id = await _propose_promotion(
                    db, settings, viewer, node, body.target_scope or "organizational", body.target_scope_id
                )
                done.append(str(new_id))
            elif body.action == "relate":
                if not body.target_id or not body.relationship_type:
                    raise HTTPException(status_code=422, detail="target_id and relationship_type are required")
                done.append(await _relate(db, settings, viewer, raw_id, body.target_id, body.relationship_type))
        except HTTPException as exc:
            errors.append({"id": raw_id, "detail": exc.detail})
    await db.commit()
    return {"done": done, "errors": errors}


@router.post("/api/memory/{memory_id}/promote", status_code=201)
async def promote(
    memory_id: str,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    target_scope: str = Query(default="organizational"),
    target_scope_id: str | None = Query(default=None),
):
    viewer = viewer_from_request(request, settings)
    node = await _load_memory(db, settings.ui_tenant_id, memory_id)
    new_id = await _propose_promotion(db, settings, viewer, node, target_scope, target_scope_id)
    await db.commit()
    return {"id": str(new_id)}


# ---------------------------------------------------------------------------
# Graph neighbourhood (screen 04)
# ---------------------------------------------------------------------------


@router.get("/api/graph/neighbourhood")
async def graph_neighbourhood(
    db: DbDep,
    settings: SettingsDep,
    focus: str | None = Query(default=None),
    depth: int = Query(default=2, ge=1, le=4),
    types: list[str] | None = Query(default=None),
    max_nodes: int = Query(default=80, ge=5, le=300),
):
    tenant_id = settings.ui_tenant_id
    visible = live_filters(tenant_id) + [MemoryNode.parent_id.is_(None)]

    if focus:
        focus_node = await _load_memory(db, tenant_id, focus)
    else:
        # Default focus: the most connected current memory, else the newest one.
        degree = (
            select(MemoryRelationship.source_id.label("nid"))
            .where(MemoryRelationship.tenant_id == tenant_id, MemoryRelationship.valid_until.is_(None))
            .union_all(
                select(MemoryRelationship.target_id).where(
                    MemoryRelationship.tenant_id == tenant_id, MemoryRelationship.valid_until.is_(None)
                )
            )
            .subquery()
        )
        row = (
            (
                await db.execute(
                    select(MemoryNode)
                    .join(degree, degree.c.nid == MemoryNode.id)
                    .where(*visible)
                    .group_by(MemoryNode.id)
                    .order_by(func.count().desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            row = (
                (await db.execute(select(MemoryNode).where(*visible).order_by(MemoryNode.updated_at.desc()).limit(1)))
                .scalars()
                .first()
            )
        if row is None:
            return {"focus": None, "nodes": [], "edges": [], "hidden_count": 0, "provenance": [], "conflicts": []}
        focus_node = row

    rel_types = types or None
    nodes: dict[uuid.UUID, MemoryNode] = {focus_node.id: focus_node}
    node_depth: dict[uuid.UUID, int] = {focus_node.id: 0}
    edges: dict[str, dict] = {}
    hidden: set[uuid.UUID] = set()
    frontier = {focus_node.id}
    for level in range(1, depth + 1):
        if not frontier or len(nodes) >= max_nodes:
            break
        rel_stmt = select(MemoryRelationship).where(
            MemoryRelationship.tenant_id == tenant_id,
            MemoryRelationship.valid_until.is_(None),
            or_(MemoryRelationship.source_id.in_(frontier), MemoryRelationship.target_id.in_(frontier)),
        )
        if rel_types:
            rel_stmt = rel_stmt.where(MemoryRelationship.relationship_type.in_(rel_types))
        rels = (await db.execute(rel_stmt)).scalars().all()
        neighbour_ids = {r.target_id for r in rels} | {r.source_id for r in rels}
        neighbour_ids -= set(nodes)
        found = {}
        if neighbour_ids:
            found = {
                n.id: n
                for n in (await db.execute(select(MemoryNode).where(MemoryNode.id.in_(list(neighbour_ids)))))
                .scalars()
                .all()
            }
        next_frontier = set()
        for nid in neighbour_ids:
            n = found.get(nid)
            if n is None or n.tenant_id != tenant_id or n.deleted_at is not None:
                hidden.add(nid)
                continue
            if len(nodes) >= max_nodes:
                hidden.add(nid)
                continue
            nodes[nid] = n
            node_depth[nid] = level
            next_frontier.add(nid)
        for r in rels:
            if r.source_id in nodes and r.target_id in nodes:
                edges[str(r.id)] = {
                    "id": str(r.id),
                    "source": str(r.source_id),
                    "target": str(r.target_id),
                    "type": r.relationship_type,
                }
        frontier = next_frontier

    # Provenance chain: follow derived_from from the focus.
    provenance = [_node_brief(focus_node)]
    cursor = focus_node.id
    seen = {cursor}
    for _ in range(6):
        nxt = (
            (
                await db.execute(
                    select(MemoryNode)
                    .join(MemoryRelationship, MemoryRelationship.target_id == MemoryNode.id)
                    .where(
                        MemoryRelationship.source_id == cursor,
                        MemoryRelationship.relationship_type == "derived_from",
                        MemoryRelationship.valid_until.is_(None),
                        MemoryNode.tenant_id == tenant_id,
                        MemoryNode.deleted_at.is_(None),
                    )
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if nxt is None or nxt.id in seen:
            break
        provenance.append(_node_brief(nxt))
        seen.add(nxt.id)
        cursor = nxt.id
    provenance.reverse()

    conflicts = []
    for e in edges.values():
        if e["type"] == "conflicts_with" and str(focus_node.id) in (e["source"], e["target"]):
            other_id = uuid.UUID(e["target"] if e["source"] == str(focus_node.id) else e["source"])
            if other_id in nodes:
                conflicts.append(_node_brief(nodes[other_id]))

    focus_degree = sum(1 for e in edges.values() if str(focus_node.id) in (e["source"], e["target"]))
    focus_hidden = (
        await db.execute(
            select(func.count())
            .select_from(MemoryRelationship)
            .outerjoin(
                MemoryNode,
                MemoryNode.id
                == case(
                    (MemoryRelationship.source_id == focus_node.id, MemoryRelationship.target_id),
                    else_=MemoryRelationship.source_id,
                ),
            )
            .where(
                MemoryRelationship.tenant_id == tenant_id,
                MemoryRelationship.valid_until.is_(None),
                or_(MemoryRelationship.source_id == focus_node.id, MemoryRelationship.target_id == focus_node.id),
                or_(MemoryNode.id.is_(None), MemoryNode.deleted_at.is_not(None), MemoryNode.tenant_id != tenant_id),
            )
        )
    ).scalar_one()

    return {
        "focus": {
            **_node_brief(focus_node),
            "content": focus_node.content,
            "degree": focus_degree,
            "hidden_degree": focus_hidden,
        },
        "nodes": [{**_node_brief(n), "depth": node_depth[n.id]} for n in nodes.values()],
        "edges": list(edges.values()),
        "hidden_count": len(hidden),
        "provenance": provenance if len(provenance) > 1 else [],
        "conflicts": conflicts,
    }


# ---------------------------------------------------------------------------
# Search playground (screen 05)
# ---------------------------------------------------------------------------


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    focus: str | None = Field(default=None, max_length=2000)
    identity: str | None = Field(default=None, max_length=255)
    project: str | None = Field(default=None, max_length=255)
    mode: str = Field(default="full", pattern="^(full|index|full_only)$")
    max_results: int = Field(default=10, ge=1, le=50)
    session_focus_weight: float = Field(default=0.4, ge=0, le=1)
    weight_threshold: float = Field(default=0.0, ge=0, le=1)
    max_response_tokens: int = Field(default=4000, ge=100, le=100_000)
    current_only: bool = True
    include_branches: bool = False
    rerank: bool = True


async def _visible_predicate(db: AsyncSession, tenant_id: str, identity: str | None, project: str | None):
    """Python predicate reproducing the MCP read RBAC for one identity.

    Returns (predicate, description). Organizational and enterprise memories
    are tenant-wide; user memories need ownership; project, role and campaign
    memories need a membership.
    """
    if not identity:
        return (lambda n: True), "dashboard (admin)"
    projects = set(
        (await db.execute(select(ProjectMembership.project_id).where(ProjectMembership.user_id == identity)))
        .scalars()
        .all()
    )
    if project and project not in projects:
        # Open projects enroll an agent on first use, so the declared project
        # context is readable; invite-only projects still need a membership.
        from memoryhub_core.models.project import Project

        invite_only = (
            await db.execute(select(Project.invite_only).where(Project.name == project, Project.tenant_id == tenant_id))
        ).scalar_one_or_none()
        if invite_only is False:
            projects.add(project)
    roles = set(
        (
            await db.execute(
                select(RoleAssignment.role_name).where(
                    RoleAssignment.user_id == identity, RoleAssignment.tenant_id == tenant_id
                )
            )
        )
        .scalars()
        .all()
    )
    campaigns = set()
    if projects:
        campaigns = {
            str(c)
            for c in (
                await db.execute(
                    select(CampaignMembership.campaign_id).where(CampaignMembership.project_id.in_(list(projects)))
                )
            )
            .scalars()
            .all()
        }

    def visible(n: MemoryNode) -> bool:
        if n.scope in ("organizational", "enterprise"):
            return True
        if n.scope == "user":
            return n.owner_id == identity
        if n.scope == "project":
            return (n.scope_id or n.owner_id) in projects
        if n.scope == "role":
            return (n.scope_id or n.owner_id) in roles
        if n.scope == "campaign":
            return (n.scope_id or n.owner_id) in campaigns
        return False

    return visible, identity


@router.post("/api/search")
async def search_playground(body: SearchRequest, db: DbDep, settings: SettingsDep):
    tenant_id = settings.ui_tenant_id
    tenant_settings = await load_tenant_settings(db, tenant_id)
    full_threshold = float(tenant_settings["retrieval"]["full_content_threshold"])
    stages: list[dict] = []

    def stage(name: str, started: float, **extra):
        stages.append({"name": name, "ms": round((time.perf_counter() - started) * 1000, 1), **extra})

    filters = [MemoryNode.tenant_id == tenant_id, MemoryNode.deleted_at.is_(None), MemoryNode.status == "active"]
    if body.current_only:
        filters.append(MemoryNode.is_current.is_(True))
    if not body.include_branches:
        filters.append(MemoryNode.parent_id.is_(None))

    t0 = time.perf_counter()
    qvec = await embed(body.query, settings.embedding_url)
    stage("embed", t0)

    candidates: list[tuple[MemoryNode, float | None]] = []
    retrieval = "vector"
    t0 = time.perf_counter()
    if qvec is not None:
        distance = MemoryNode.embedding.cosine_distance(qvec)
        rows = (
            await db.execute(
                select(MemoryNode, (1 - distance).label("score"))
                .where(*filters, MemoryNode.embedding.is_not(None))
                .order_by(distance)
                .limit(50)
            )
        ).all()
        candidates = [(r[0], float(r[1])) for r in rows]
        stage("pgvector", t0, detail="top-50")
    else:
        retrieval = "text"
        tsq = func.plainto_tsquery("english", body.query)
        rank = func.ts_rank(literal_column("search_vector"), tsq)
        rows = (
            await db.execute(
                select(MemoryNode, rank.label("score"))
                .where(*filters, literal_column("search_vector").op("@@")(tsq))
                .order_by(rank.desc())
                .limit(50)
            )
        ).all()
        if not rows:
            pattern = f"%{body.query}%"
            rows = (
                await db.execute(
                    select(MemoryNode, literal_column("0.0"))
                    .where(*filters, or_(MemoryNode.content.ilike(pattern), MemoryNode.stub.ilike(pattern)))
                    .limit(50)
                )
            ).all()
        candidates = [(r[0], float(r[1])) for r in rows]
        stage("full-text", t0, detail="embedding service unavailable")

    t0 = time.perf_counter()
    visible, run_as = await _visible_predicate(db, tenant_id, body.identity, body.project)
    allowed = [(n, s) for n, s in candidates if visible(n)]
    omitted = len(candidates) - len(allowed)
    stage("rbac", t0)

    focus_scores: dict[uuid.UUID, float] = {}
    if body.focus and body.session_focus_weight > 0 and allowed:
        t0 = time.perf_counter()
        fvec = await embed(body.focus, settings.embedding_url)
        if fvec is not None:
            for n, _ in allowed:
                if n.embedding is not None:
                    focus_scores[n.id] = cosine(list(n.embedding), fvec)
        stage("focus", t0)

    t0 = time.perf_counter()
    k = 60
    w = body.session_focus_weight if focus_scores else 0.0
    by_query = sorted(allowed, key=lambda x: x[1] or 0, reverse=True)
    q_rank = {n.id: i + 1 for i, (n, _) in enumerate(by_query)}
    f_rank = {nid: i + 1 for i, nid in enumerate(sorted(focus_scores, key=lambda x: focus_scores[x], reverse=True))}
    rrf = {}
    for n, _ in allowed:
        score = (1 - w) / (k + q_rank[n.id])
        if w:
            score += w / (k + f_rank.get(n.id, len(allowed) + 1))
        rrf[n.id] = score * 2
    fused = sorted(allowed, key=lambda x: rrf[x[0].id], reverse=True)
    stage("rrf", t0)

    rerank_scores: dict[uuid.UUID, float] = {}
    rerank_status = "off"
    if body.rerank and fused:
        if settings.reranker_url:
            t0 = time.perf_counter()
            top = fused[: min(len(fused), 25)]
            scores = await rerank(body.query, [n.content for n, _ in top], settings.reranker_url)
            if scores is not None:
                rerank_scores = {n.id: s for (n, _), s in zip(top, scores, strict=False)}
                fused = sorted(fused, key=lambda x: rerank_scores.get(x[0].id, -1e9), reverse=True)
                rerank_status = "on"
            else:
                rerank_status = "unavailable"
            stage("rerank", t0)
        else:
            rerank_status = "not_configured"

    t0 = time.perf_counter()
    weight_filtered = [(n, s) for n, s in fused if n.weight >= body.weight_threshold]
    selected = weight_filtered[: body.max_results]
    results = []
    tokens_used = 0
    full_count = 0
    for rank_pos, (n, vscore) in enumerate(selected, start=1):
        if body.mode == "index":
            delivery = "stub"
        elif body.mode == "full_only":
            delivery = "full"
        else:
            delivery = "full" if n.weight >= full_threshold else "stub"
        text_value = n.content if delivery == "full" else (n.stub or n.content[:80])
        tokens = max(1, len(text_value) // 4)
        if tokens_used + tokens > body.max_response_tokens and delivery == "full":
            delivery, text_value = "stub", (n.stub or n.content[:80])
            tokens = max(1, len(text_value) // 4)
        if tokens_used + tokens > body.max_response_tokens:
            break
        tokens_used += tokens
        full_count += delivery == "full"
        results.append(
            {
                "rank": rank_pos,
                "id": str(n.id),
                "scope": n.scope,
                "scope_id": n.scope_id,
                "owner_id": n.owner_id,
                "stub": clean_stub(n.stub),
                "content": n.content if delivery == "full" else None,
                "delivery": delivery,
                "weight": n.weight,
                "version": n.version,
                "scores": {
                    "vector": round(vscore, 4) if vscore is not None and retrieval == "vector" else None,
                    "text": round(vscore, 4) if vscore is not None and retrieval == "text" else None,
                    "focus": round(focus_scores[n.id], 4) if n.id in focus_scores else None,
                    "rrf": round(rrf[n.id], 4),
                    "rerank": round(rerank_scores[n.id], 4) if n.id in rerank_scores else None,
                    "weight": n.weight,
                },
                "tokens": tokens,
            }
        )
    assembled = "\n\n".join(f"[{r['scope']}] {r['content'] if r['delivery'] == 'full' else r['stub']}" for r in results)
    stage("assemble", t0)

    return {
        "run_as": run_as,
        "retrieval": retrieval,
        "rerank": rerank_status,
        "stages": stages,
        "total_ms": round(sum(s["ms"] for s in stages), 1),
        "candidates": len(candidates),
        "omitted_count": omitted,
        "weight_filtered": len(fused) - len(weight_filtered),
        "returned": len(results),
        "full_count": full_count,
        "stub_count": len(results) - full_count,
        "tokens": tokens_used,
        "results": results,
        "assembled_context": assembled,
    }


# ---------------------------------------------------------------------------
# Utilities shared with routes_admin
# ---------------------------------------------------------------------------


def stub_for(text_value: str) -> str:
    text_value = re.sub(r"\s+", " ", text_value or "").strip()
    return text_value if len(text_value) <= 120 else text_value[:119] + "…"


def as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}
