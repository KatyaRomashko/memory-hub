"""Preferences & privacy and notifications (#723).

- ``GET/PATCH /api/me/prefs``: the person's preferences, stored in
  ``memoryhub_user_prefs`` (``memoryhub_core.services.user_prefs``). MCP
  applies the writing defaults and never-store topics to the person's agents.
- ``POST /api/me/data/delete``: delete every personal memory of the person and
  their agents (soft delete, audited). Export is ``GET /api/memories/export?mine=true``.
- ``GET /api/notifications`` and ``POST /api/notifications/seen``: what needs
  the person's attention, computed from the records it is about:
  decisions on their requests (``request.decided`` audit events), contradiction
  reports others filed on their memories, and incident (break-glass) access to
  their memories. ``unread`` counts items newer than the last time they opened
  the bell.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from memoryhub_core.models.audit import AuditLog
from memoryhub_core.models.contradiction import ContradictionReport
from memoryhub_core.models.memory import MemoryNode
from memoryhub_core.services import user_prefs
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.authorization import Access, AccessDep
from src.config import Settings, get_settings
from src.database import get_db
from src.human_adapters import _record_human_audit

router = APIRouter()
DbDep = Annotated[AsyncSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

NOTIFICATION_DAYS = 30
NOTIFICATION_LIMIT = 50
INCIDENT_EVENTS = ("incident.access", "break_glass.read", "break_glass.granted")
APPLIED_BY = [
    "write_memory (MCP): default weight when an agent sends none",
    "write_memory (MCP): writes mentioning a never-store topic are refused and audited",
    "Dreaming (conversation extraction): skipped for your threads while Dreaming is off",
    "Dashboard: default scope and weight of a new memory",
]


class DeleteMyData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: str


def _personal(access: Access):
    return (MemoryNode.tenant_id == access.principal.tenant_id, MemoryNode.scope == "user",
            MemoryNode.owner_id.in_(access.identities))


async def _payload(db: AsyncSession, access: Access, settings: Settings) -> dict:
    row = await user_prefs.load(db, access.principal.tenant_id, access.principal.username)
    prefs = user_prefs.effective(row.prefs if row else None)
    behavior = {**prefs["behavior"], "schema": user_prefs.PROFILE_SCHEMA, "revision": row.revision if row else 0,
                "updated_at": prefs.get("behavior_updated_at")}
    own = 0
    if access.principal.role == "user":
        own = (await db.execute(select(func.count()).select_from(MemoryNode).where(
            *_personal(access), MemoryNode.is_current.is_(True), MemoryNode.deleted_at.is_(None)))).scalar_one()
    source = "local" if getattr(settings, "ui_auth_mode", "openshift") == "local" else "oauth-proxy"
    return {
        "prefs": {
            "write": prefs["write"],
            "dreaming": prefs["dreaming"],
            "onboarding_dismissed": prefs["onboarding_dismissed"],
            "behavior": behavior,
            "behavior_history": [{**h, "schema": user_prefs.PROFILE_SCHEMA} for h in (row.history if row else [])],
        },
        "profile": {
            "schema": user_prefs.PROFILE_SCHEMA,
            "templates": {name: {**t, "custom_exclude": [], "schema": user_prefs.PROFILE_SCHEMA, "revision": 0,
                                 "updated_at": None} for name, t in user_prefs.TEMPLATES.items()},
            "categories": user_prefs.CATEGORIES,
            "excludes": user_prefs.EXCLUDES,
            "choices": user_prefs.CHOICES,
            "applied_by": APPLIED_BY,
        },
        "account": {
            "username": access.principal.username,
            "role": access.principal.role,
            "source": source,
            "own_memories": int(own),
            "agents": sorted(access.agents),
            "projects": sorted(access.human_projects),
            "roles": sorted(access.human_roles),
        },
    }


@router.get("/api/me/prefs")
async def get_prefs(db: DbDep, access: AccessDep, settings: SettingsDep, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return await _payload(db, access, settings)


@router.patch("/api/me/prefs")
async def patch_prefs(body: user_prefs.PrefsPatch, db: DbDep, access: AccessDep, settings: SettingsDep):
    if body.write is not None and body.write.default_scope == "user" and access.principal.role != "user":
        raise HTTPException(status_code=422, detail="Admins write no personal memories")
    row = await user_prefs.apply_patch(db, access.principal.tenant_id, access.principal.username, body)
    _record_human_audit(db, access, "prefs.updated", "user", access.principal.username, None,
                        {"sections": sorted(body.model_dump(exclude_none=True)), "revision": row.revision})
    await db.commit()
    return await _payload(db, access, settings)


@router.post("/api/me/data/delete")
async def delete_my_data(body: DeleteMyData, db: DbDep, access: AccessDep):
    """Delete every personal memory of the person and their agents (every version)."""
    if access.principal.role != "user":
        raise HTTPException(status_code=403, detail="Only users have personal memories")
    if body.confirm != access.principal.username:
        raise HTTPException(status_code=422, detail="Type your username to confirm")
    now = datetime.now(UTC)
    result = await db.execute(update(MemoryNode).where(*_personal(access), MemoryNode.deleted_at.is_(None))
                              .values(deleted_at=now).execution_options(synchronize_session=False))
    current = int(result.rowcount or 0)
    _record_human_audit(db, access, "memory.delete_all", "user", access.principal.username, None,
                        {"versions_deleted": current, "identities": sorted(access.identities)})
    await db.commit()
    return {"deleted": current, "deleted_at": now.isoformat()}


async def _notifications(db: AsyncSession, access: Access) -> list[dict]:
    tenant, user = access.principal.tenant_id, access.principal.username
    since = datetime.now(UTC) - timedelta(days=NOTIFICATION_DAYS)
    items: list[dict] = []

    decided = (await db.execute(select(AuditLog).where(
        AuditLog.tenant_id == tenant, AuditLog.owner_id == user, AuditLog.event_type == "request.decided",
        AuditLog.timestamp >= since).order_by(AuditLog.timestamp.desc()).limit(NOTIFICATION_LIMIT))).scalars().all()
    for event in decided:
        meta = event.event_metadata or {}
        approved = meta.get("outcome") == "approved"
        items.append({
            "id": f"request-{event.id}", "kind": "review", "severity": "info" if approved else "warning",
            "title": f"Request {meta.get('request_id', '')} {'approved' if approved else 'rejected'}".strip(),
            "body": f"Decided by {event.actor_id}" + (f" · published to {meta['target_scope']}"
                                                       if approved and meta.get("target_scope") else ""),
            "at": event.timestamp, "href": "/review?tab=decided",
        })

    if access.principal.role == "user":
        reports = (await db.execute(
            select(ContradictionReport.id, ContradictionReport.memory_id, ContradictionReport.reporter,
                   ContradictionReport.created_at)
            .join(MemoryNode, MemoryNode.id == ContradictionReport.memory_id)
            .where(MemoryNode.tenant_id == tenant, MemoryNode.owner_id.in_(access.identities),
                   ContradictionReport.resolved.is_(False), ContradictionReport.created_at >= since,
                   ContradictionReport.reporter.not_in(access.identities))
            .order_by(ContradictionReport.created_at.desc()).limit(NOTIFICATION_LIMIT))).all()
        for report_id, memory_id, reporter, created_at in reports:
            items.append({
                "id": f"contradiction-{report_id}", "kind": "contradiction", "severity": "warning",
                "title": "A memory of yours was reported as contradicted",
                "body": f"Reported by {reporter}", "at": created_at, "href": f"/memory/{memory_id}",
            })

    incidents = (await db.execute(select(AuditLog).where(
        AuditLog.tenant_id == tenant, AuditLog.owner_id == user, AuditLog.event_type.in_(INCIDENT_EVENTS),
        AuditLog.timestamp >= since).order_by(AuditLog.timestamp.desc()).limit(NOTIFICATION_LIMIT))).scalars().all()
    for event in incidents:
        meta = event.event_metadata or {}
        items.append({
            "id": f"incident-{event.id}", "kind": "break_glass", "severity": "danger",
            "title": "Incident access to your memories",
            "body": f"{event.actor_id}" + (f" · incident {meta['incident']}" if meta.get("incident") else ""),
            "at": event.timestamp, "href": "/audit?view=incidents",
        })
    items.sort(key=lambda n: n["at"], reverse=True)
    return items[:NOTIFICATION_LIMIT]


@router.get("/api/notifications")
async def list_notifications(db: DbDep, access: AccessDep, response: Response):
    response.headers["Cache-Control"] = "no-store"
    row = await user_prefs.load(db, access.principal.tenant_id, access.principal.username)
    seen_at = row.notifications_seen_at if row else None
    items = await _notifications(db, access)
    for item in items:
        item["unread"] = seen_at is None or item["at"] > seen_at
        item["at"] = item["at"].isoformat()
    return {"items": items, "unread": sum(1 for i in items if i["unread"]),
            "seen_at": seen_at.isoformat() if seen_at else None}


@router.post("/api/notifications/seen")
async def mark_notifications_seen(db: DbDep, access: AccessDep):
    row = await user_prefs.load(db, access.principal.tenant_id, access.principal.username, lock=True)
    if row is None:
        row = user_prefs.UserPreference(tenant_id=access.principal.tenant_id, user_id=access.principal.username,
                                        prefs={}, revision=0, history=[])
        db.add(row)
    row.notifications_seen_at = datetime.now(UTC)
    await db.commit()
    return {"ok": True, "seen_at": row.notifications_seen_at.isoformat()}

