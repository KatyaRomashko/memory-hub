"""Dreaming (#716, Labs): housekeeping runs over the memories a person can read.

A run looks at the person's readable memories (``access.clause()``) and proposes:

- ``duplicates``: two of the person's writable memories that say nearly the same
  thing (cosine similarity ≥ 0.92). Accepting keeps the newer, lowers the older
  memory's weight in a new version and links newer ``supersedes`` older.
- ``connections``: two writable memories in the same scope that are related
  (similarity 0.78–0.92) but not linked. Accepting adds ``related_to``.
- ``fading``: the person's weighty memories (weight ≥ 0.5) nobody read, served
  or changed for the fade period. Accepting lowers the weight in a new version.
- ``contradictions``: readable memories with open contradiction reports.
  Accepting marks the insight reviewed; the page opens the memory's reports.
- ``themes``: a domain with many recent readable memories. Accepting writes one
  consolidated memory through the normal write path, so curation rules apply.

Nothing changes without the person's OK, except the reversible kinds they let
run alone (``auto.connections``, ``auto.fading``). Every change is audited.
A dismissed insight is not proposed again.

Runs happen on demand (``POST /api/dreams/run``) or on the person's schedule:
``run_due()`` takes due schedules with ``FOR UPDATE SKIP LOCKED`` (safe with
several replicas) and is called by the BFF's background loop
(``MEMORYHUB_UI_DREAM_SCHEDULER``) or by a CronJob through ``python -m src.dreams_adapter``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from memoryhub_core.models.audit import AuditLog
from memoryhub_core.models.contradiction import ContradictionReport
from memoryhub_core.models.memory import MemoryNode, MemoryRelationship
from memoryhub_core.models.schemas import MemoryNodeCreate, MemoryNodeUpdate, RelationshipCreate, RelationshipType
from memoryhub_core.services.graph import create_relationship
from memoryhub_core.services.memory import create_memory as core_create_memory
from memoryhub_core.services.memory import update_memory as core_update_memory
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import JSON, Column, DateTime, Integer, String, Uuid, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, declarative_base

from src.authorization import Access, AccessDep, Principal, get_access
from src.config import Settings, get_settings
from src.database import get_db
from src.human_adapters import EmbeddingDep, _membership, _record_human_audit, get_embedding_service

logger = logging.getLogger(__name__)
router = APIRouter()
DbDep = Annotated[AsyncSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
DreamBase = declarative_base()

KINDS = ("duplicates", "connections", "fading", "contradictions", "themes")
DUPLICATE_DISTANCE = 0.08   # cosine distance: similarity ≥ 0.92
CONNECTION_DISTANCE = 0.22  # similarity ≥ 0.78
PAIR_POPULATION = 500
POPULATION = 2000
PER_KIND = 25
FADE_DAYS = 60
FADE_MIN_WEIGHT = 0.5
THEME_DAYS = 30
THEME_MIN = 4
HISTORY = 10
DEFAULT_SETTINGS = {
    "schedule": "off", "window": "02:00",
    "kinds": {kind: True for kind in KINDS},
    "auto": {"connections": False, "fading": False},
}


class DreamSettingsRow(DreamBase):
    __tablename__ = "memoryhub_ui_dream_settings"
    tenant_id = Column(String(255), primary_key=True)
    user_id = Column(String(255), primary_key=True)
    schedule = Column(String(16), nullable=False, default="off")
    window = Column(String(5), nullable=False, default="02:00")
    kinds = Column(JSON, nullable=False, default=dict)
    auto = Column(JSON, nullable=False, default=dict)
    next_run_at = Column(DateTime(timezone=True))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


class DreamRun(DreamBase):
    __tablename__ = "memoryhub_ui_dream_runs"
    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id = Column(String(255), nullable=False)
    user_id = Column(String(255), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False)
    duration_ms = Column(Integer, nullable=False, default=0)
    trigger = Column(String(16), nullable=False)
    reviewed = Column(Integer, nullable=False, default=0)
    insights = Column(JSON, nullable=False, default=list)
    counts = Column(JSON, nullable=False, default=dict)


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schedule: Literal["nightly", "weekly", "off"] | None = None
    window: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    kinds: dict[str, bool] | None = None
    auto: dict[str, bool] | None = None

    @field_validator("kinds")
    @classmethod
    def _kinds(cls, value: dict[str, bool] | None) -> dict[str, bool] | None:
        if value is not None and set(value) - set(KINDS):
            raise ValueError(f"kinds are {', '.join(KINDS)}")
        return value

    @field_validator("auto")
    @classmethod
    def _auto(cls, value: dict[str, bool] | None) -> dict[str, bool] | None:
        if value is not None and set(value) - {"connections", "fading"}:
            raise ValueError("only connections and fading may run on their own")
        return value


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["accept", "dismiss"]


class ConsolidateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=20000)
    scope: Literal["user", "project"] = "user"
    scope_id: str | None = Field(default=None, max_length=255)
    weight: float = Field(default=0.7, ge=0, le=1)


# --------------------------------------------------------------------------- settings


def _settings_view(row: DreamSettingsRow | None) -> dict:
    if row is None:
        return {**DEFAULT_SETTINGS, "next_run_at": None}
    return {"schedule": row.schedule, "window": row.window,
            "kinds": {**DEFAULT_SETTINGS["kinds"], **(row.kinds or {})},
            "auto": {**DEFAULT_SETTINGS["auto"], **(row.auto or {})},
            "next_run_at": row.next_run_at.isoformat() if row.next_run_at else None}


def next_run(schedule: str, window: str, now: datetime, last_run: datetime | None) -> datetime | None:
    """The next run time in UTC: the window today or tomorrow; weekly waits six days after the last run."""
    if schedule == "off":
        return None
    hour, minute = map(int, window.split(":"))
    candidate = now.astimezone(UTC).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    if schedule == "weekly" and last_run is not None:
        while candidate <= last_run + timedelta(days=6):
            candidate += timedelta(days=1)
    return candidate


async def _last_run(db: AsyncSession, tenant: str, user: str) -> DreamRun | None:
    return (await db.execute(select(DreamRun).where(DreamRun.tenant_id == tenant, DreamRun.user_id == user)
                             .order_by(DreamRun.started_at.desc()).limit(1))).scalar_one_or_none()


# --------------------------------------------------------------------------- the run


def _brief(node: MemoryNode) -> dict:
    return {"id": str(node.id), "stub": node.stub, "scope": node.scope, "scope_id": node.scope_id,
            "owner_id": node.owner_id, "weight": node.weight, "version": node.version,
            "updated_at": node.updated_at.isoformat() if node.updated_at else None}


def _insight_id(kind: str, ids: list[str], extra: str = "") -> str:
    return hashlib.sha256(f"{kind}:{','.join(sorted(ids))}:{extra}".encode()).hexdigest()[:16]


def _same_space(a: MemoryNode, b: MemoryNode) -> bool:
    """Where the dashboard may link two memories (POST /api/relationships)."""
    return a.scope == b.scope and a.scope_id == b.scope_id and (a.scope != "user" or a.owner_id == b.owner_id)


async def _dismissed(db: AsyncSession, tenant: str, user: str) -> set[str]:
    rows = (await db.execute(select(DreamRun.insights).where(
        DreamRun.tenant_id == tenant, DreamRun.user_id == user))).scalars().all()
    return {i["id"] for insights in rows for i in insights or [] if i.get("status") == "dismissed"}


async def _pairs(db: AsyncSession, access: Access,
                 population: list[MemoryNode]) -> list[tuple[MemoryNode, MemoryNode, float]]:
    writable = [n for n in population if access.can_write(n) and n.embedding is not None][:PAIR_POPULATION]
    if len(writable) < 2:
        return []
    ids = [n.id for n in writable]
    a, b = aliased(MemoryNode), aliased(MemoryNode)
    distance = a.embedding.cosine_distance(b.embedding).label("distance")
    rows = (await db.execute(select(a.id, b.id, distance).where(
        a.id.in_(ids), b.id.in_(ids), a.id < b.id, a.scope == b.scope,
        a.embedding.cosine_distance(b.embedding) < CONNECTION_DISTANCE,
    ).order_by(distance).limit(PER_KIND * 8))).all()
    by_id = {n.id: n for n in writable}
    linked = set()
    if rows:
        pair_ids = {i for row in rows for i in row[:2]}
        edges = select(MemoryRelationship.source_id, MemoryRelationship.target_id).where(
            MemoryRelationship.tenant_id == access.principal.tenant_id, MemoryRelationship.valid_until.is_(None),
            MemoryRelationship.source_id.in_(pair_ids), MemoryRelationship.target_id.in_(pair_ids))
        for source, target in (await db.execute(edges)).all():
            linked.add(frozenset((source, target)))
    return [(by_id[x], by_id[y], float(d)) for x, y, d in rows
            if frozenset((x, y)) not in linked and _same_space(by_id[x], by_id[y])]


async def _compute(db: AsyncSession, access: Access, kinds: dict[str, bool],
                   dismissed: set[str]) -> tuple[int, list[dict]]:
    tenant = access.principal.tenant_id
    now = datetime.now(UTC)
    population = (await db.execute(select(MemoryNode).where(
        MemoryNode.is_current.is_(True), MemoryNode.deleted_at.is_(None), MemoryNode.scope != "entity",
        access.clause()).order_by(MemoryNode.updated_at.desc()).limit(POPULATION))).scalars().all()
    population = [n for n in population if access.can_read(n)]
    insights: list[dict] = []

    def add(kind: str, title: str, detail: str, memories: list[MemoryNode], action: dict,
            confidence: float | None = None, extra: str = "") -> None:
        ids = [str(m.id) for m in memories]
        insight_id = _insight_id(kind, ids, extra)
        if insight_id in dismissed or sum(1 for i in insights if i["kind"] == kind) >= PER_KIND:
            return
        insights.append({"id": insight_id, "kind": kind, "title": title, "detail": detail,
                         "memories": [_brief(m) for m in memories], "action": action,
                         "confidence": confidence, "status": "proposed"})

    if kinds.get("duplicates") or kinds.get("connections"):
        for x, y, distance in await _pairs(db, access, population):
            newer, older = (x, y) if (x.updated_at or now) >= (y.updated_at or now) else (y, x)
            if distance < DUPLICATE_DISTANCE and kinds.get("duplicates"):
                add("duplicates", "These two say nearly the same thing",
                    "Keep the newer one and lower the older one's weight, so agents get one answer.",
                    [newer, older], {"type": "merge", "keep": str(newer.id), "fade": str(older.id)},
                    round(1 - distance, 3))
            elif distance >= DUPLICATE_DISTANCE and kinds.get("connections"):
                add("connections", "Related, but not linked",
                    "Linking them lets agents retrieve one with the other.",
                    [x, y], {"type": "link", "source": str(x.id), "target": str(y.id)}, round(1 - distance, 3))

    if kinds.get("fading"):
        since = now - timedelta(days=FADE_DAYS)
        own = [n for n in population if access.can_write(n) and n.weight >= FADE_MIN_WEIGHT
               and n.updated_at and n.updated_at < since]
        if own:
            used: set[str] = set()
            own_ids = [n.id for n in own]
            used |= {str(m) for m in (await db.execute(select(AuditLog.memory_id).where(
                AuditLog.tenant_id == tenant, AuditLog.memory_id.in_(own_ids), AuditLog.timestamp >= since,
                AuditLog.event_type.in_(("memory.read", "memory.update", "memory.relationship_created")),
            ))).scalars().all()}
            for meta in (await db.execute(select(AuditLog.event_metadata).where(
                AuditLog.tenant_id == tenant, AuditLog.event_type == "memory.served", AuditLog.timestamp >= since,
            ).limit(20000))).scalars().all():
                used |= set(map(str, (meta or {}).get("result_ids") or []))
            for node in own:
                if str(node.id) not in used:
                    add("fading", "Nobody has used this for a while",
                        f"Weight {node.weight:.2f}, unchanged and unread for over {FADE_DAYS} days. "
                        "A lower weight keeps it out of agents' full context.",
                        [node], {"type": "fade", "memory": str(node.id), "to": round(max(0.1, node.weight / 2), 2)})

    if kinds.get("contradictions"):
        readable = {n.id: n for n in population}
        if readable:
            for memory_id, reports in (await db.execute(select(
                    ContradictionReport.memory_id, func.count()).where(
                    ContradictionReport.memory_id.in_(list(readable)), ContradictionReport.resolved.is_(False))
                    .group_by(ContradictionReport.memory_id).order_by(func.count().desc()))).all():
                add("contradictions", "Agents report this as wrong",
                    f"{reports} open contradiction report{'s' if reports != 1 else ''}. "
                    "Review and update or resolve them.",
                    [readable[memory_id]], {"type": "review", "memory": str(memory_id)}, extra=str(reports))

    if kinds.get("themes"):
        recent = [n for n in population if n.updated_at and n.updated_at >= now - timedelta(days=THEME_DAYS)]
        domains = Counter(d for n in recent for d in set(n.domains or []))
        for domain, count in domains.most_common():
            if count < THEME_MIN:
                break
            members = [n for n in recent if domain in (n.domains or [])][:12]
            draft = f"{domain}: what we know\n" + "\n".join(f"- {m.stub}" for m in members)
            add("themes", f"{count} recent memories about {domain}",
                "One consolidated memory gives agents the whole picture at once. Edit the draft before saving.",
                members, {"type": "consolidate", "domain": domain, "draft": draft}, extra=domain)
    return len(population), insights


async def _apply(db: AsyncSession, access: Access, insight: dict, embedding) -> str:
    """Carry out a merge, link or fade. Each step rechecks access on locked rows."""
    action = insight["action"]
    username = access.principal.username

    async def writable(memory_id: str) -> MemoryNode:
        node = await db.get(MemoryNode, uuid.UUID(memory_id), with_for_update=True)
        if node is None or not node.is_current or node.deleted_at is not None or not access.can_write(node):
            raise HTTPException(status_code=409,
                                detail="A memory in this insight changed or is no longer yours to edit")
        return node

    async def lower(node: MemoryNode, weight: float) -> MemoryNode:
        _record_human_audit(db, access, "memory.update", node.scope, node.owner_id, node.id,
                            {"dreaming": insight["id"], "weight": {"before": node.weight, "after": weight}})
        updated = await core_update_memory(node.id, MemoryNodeUpdate(weight=weight), db, embedding,
                                           actor_id=username, driver_id=username)
        return await db.get(MemoryNode, updated.id)

    async def link(source: MemoryNode, target: MemoryNode, kind: RelationshipType) -> None:
        _record_human_audit(db, access, "memory.relationship_created", source.scope, source.owner_id, source.id,
                            {"dreaming": insight["id"], "target_id": str(target.id), "relationship_type": kind.value})
        await create_relationship(RelationshipCreate(source_id=source.id, target_id=target.id,
                                                     relationship_type=kind, created_by=username), db)

    if action["type"] == "link":
        source, target = await writable(action["source"]), await writable(action["target"])
        if not _same_space(source, target):
            raise HTTPException(status_code=409, detail="These memories can no longer be linked")
        await link(source, target, RelationshipType.related_to)
        return "Linked."
    if action["type"] == "fade":
        node = await writable(action["memory"])
        await lower(node, min(node.weight, float(action["to"])))
        return f"Weight lowered to {min(node.weight, float(action['to'])):.2f}."
    if action["type"] == "merge":
        keep, fade = await writable(action["keep"]), await writable(action["fade"])
        faded = await lower(fade, round(min(fade.weight, 0.2), 2))
        if _same_space(keep, faded):
            await link(keep, faded, RelationshipType.supersedes)
        return "Kept the newer memory; the older one now has weight 0.20."
    if action["type"] == "review":
        return "Opened for review."
    raise HTTPException(status_code=422, detail="Write the consolidated memory with the consolidate action")


async def run_dream(db: AsyncSession, access: Access, trigger: str, embedding=None) -> DreamRun:
    tenant, user = access.principal.tenant_id, access.principal.username
    started = datetime.now(UTC)
    clock = time.monotonic()
    row = await db.get(DreamSettingsRow, (tenant, user))
    settings = _settings_view(row)
    reviewed, insights = await _compute(db, access, settings["kinds"], await _dismissed(db, tenant, user))
    for insight in insights:
        kind = insight["kind"]
        if embedding is not None and kind in ("connections", "fading") and settings["auto"].get(kind):
            try:
                insight["result"] = await _apply(db, access, insight, embedding)
                insight["status"], insight["auto"] = "accepted", True
            except HTTPException as exc:
                insight["result"] = exc.detail
    run = DreamRun(id=uuid.uuid4(), tenant_id=tenant, user_id=user, started_at=started, trigger=trigger,
                   reviewed=reviewed, insights=insights, counts=dict(Counter(i["kind"] for i in insights)),
                   duration_ms=int((time.monotonic() - clock) * 1000))
    db.add(run)
    _record_human_audit(db, access, "dream.run", "user", user, None,
                        {"run_id": str(run.id), "trigger": trigger, "reviewed": reviewed, "counts": run.counts})
    if row is not None:
        row.next_run_at = next_run(row.schedule, row.window, datetime.now(UTC), started)
    await db.commit()
    return run


def _report(run: DreamRun) -> dict:
    return {"id": str(run.id), "started_at": run.started_at.isoformat(), "duration_ms": run.duration_ms,
            "trigger": run.trigger, "reviewed": run.reviewed, "insights": run.insights, "counts": run.counts}


async def run_due(sessions, settings: Settings, embedding=None, *, limit: int = 20) -> int:
    """Run every due schedule once. Safe to call from several replicas at once."""
    done = 0
    while done < limit:
        async with sessions() as db:
            row = (await db.execute(select(DreamSettingsRow).where(
                DreamSettingsRow.schedule != "off", DreamSettingsRow.next_run_at <= datetime.now(UTC),
            ).order_by(DreamSettingsRow.next_run_at).limit(1).with_for_update(skip_locked=True))).scalar_one_or_none()
            if row is None:
                return done
            access = await get_access(Principal(row.user_id, "user", row.tenant_id), db, settings)
            try:
                await run_dream(db, access, "scheduled", embedding)
            except Exception:  # noqa: BLE001 - one person's failed run must not stop the others
                logger.exception("Scheduled Dreaming run failed for %s", row.user_id)
                await db.rollback()
                async with sessions() as retry:
                    failed = await retry.get(DreamSettingsRow, (row.tenant_id, row.user_id))
                    failed.next_run_at = next_run(failed.schedule, failed.window, datetime.now(UTC), datetime.now(UTC))
                    await retry.commit()
            done += 1
    return done


async def scheduler_loop(sessions, settings: Settings, interval: int) -> None:
    embedding = None
    if settings.embedding_url:
        try:
            embedding = get_embedding_service(settings)
        except HTTPException:
            embedding = None
    while True:
        try:
            ran = await run_due(sessions, settings, embedding)
            if ran:
                logger.info("Dreaming: %d scheduled run(s)", ran)
        except Exception:  # noqa: BLE001
            logger.exception("Dreaming scheduler pass failed")
        await asyncio.sleep(interval)


# --------------------------------------------------------------------------- routes


def _require_labs(access: Access, settings: Settings) -> None:
    if not settings.ui_labs:
        raise HTTPException(status_code=404, detail="Dreaming is a Labs feature and Labs is off")
    if access.principal.role != "user":
        raise HTTPException(status_code=403, detail="Dreaming runs over a user's own memories")


@router.get("/api/dreams")
async def get_dreams(db: DbDep, access: AccessDep, settings: SettingsDep):
    _require_labs(access, settings)
    tenant, user = access.principal.tenant_id, access.principal.username
    runs = (await db.execute(select(DreamRun).where(DreamRun.tenant_id == tenant, DreamRun.user_id == user)
                             .order_by(DreamRun.started_at.desc()).limit(HISTORY))).scalars().all()
    history = [{**{k: v for k, v in _report(r).items() if k != "insights"},
                "accepted": sum(1 for i in r.insights if i.get("status") == "accepted"),
                "dismissed": sum(1 for i in r.insights if i.get("status") == "dismissed"),
                "total": len(r.insights)} for r in runs]
    return {"settings": _settings_view(await db.get(DreamSettingsRow, (tenant, user))),
            "last": _report(runs[0]) if runs else None, "history": history}


@router.patch("/api/dreams/settings")
async def save_settings(body: SettingsPatch, db: DbDep, access: AccessDep, settings: SettingsDep):
    _require_labs(access, settings)
    key = (access.principal.tenant_id, access.principal.username)
    row = await db.get(DreamSettingsRow, key, with_for_update=True)
    if row is None:
        row = DreamSettingsRow(tenant_id=key[0], user_id=key[1], schedule="off", window="02:00",
                               kinds=dict(DEFAULT_SETTINGS["kinds"]), auto=dict(DEFAULT_SETTINGS["auto"]))
        db.add(row)
    if body.schedule is not None:
        row.schedule = body.schedule
    if body.window is not None:
        row.window = body.window
    if body.kinds is not None:
        row.kinds = {**DEFAULT_SETTINGS["kinds"], **(row.kinds or {}), **body.kinds}
    if body.auto is not None:
        row.auto = {**DEFAULT_SETTINGS["auto"], **(row.auto or {}), **body.auto}
    last = await _last_run(db, *key)
    row.next_run_at = next_run(row.schedule, row.window, datetime.now(UTC), last.started_at if last else None)
    row.updated_at = datetime.now(UTC)
    _record_human_audit(db, access, "dream.settings", "user", key[1], None,
                        {"schedule": row.schedule, "window": row.window, "auto": row.auto})
    await db.commit()
    return _settings_view(row)


@router.post("/api/dreams/run")
async def dream_now(db: DbDep, access: AccessDep, settings: SettingsDep, embedding: EmbeddingDep):
    _require_labs(access, settings)
    return _report(await run_dream(db, access, "manual", embedding))


async def _insight(db: AsyncSession, access: Access, run_id: uuid.UUID, insight_id: str) -> tuple[DreamRun, dict]:
    run = (await db.execute(select(DreamRun).where(
        DreamRun.id == run_id, DreamRun.tenant_id == access.principal.tenant_id,
        DreamRun.user_id == access.principal.username).with_for_update())).scalar_one_or_none()
    insight = next((i for i in (run.insights if run else []) if i["id"] == insight_id), None)
    if run is None or insight is None:
        raise HTTPException(status_code=404, detail="Insight not found")
    if insight["status"] != "proposed":
        raise HTTPException(status_code=409, detail=f"Insight already {insight['status']}")
    return run, insight


def _store(run: DreamRun, insight: dict) -> None:
    run.insights = [insight if i["id"] == insight["id"] else i for i in run.insights]


@router.post("/api/dreams/{run_id}/insights/{insight_id}/decision")
async def decide(run_id: uuid.UUID, insight_id: str, body: DecisionBody, db: DbDep, access: AccessDep,
                 settings: SettingsDep, embedding: EmbeddingDep):
    _require_labs(access, settings)
    run, insight = await _insight(db, access, run_id, insight_id)
    insight = dict(insight)
    if body.decision == "dismiss":
        insight["status"] = "dismissed"
    else:
        insight["result"] = await _apply(db, access, insight, embedding)
        insight["status"] = "accepted"
    insight["decided_at"] = datetime.now(UTC).isoformat()
    _store(run, insight)
    _record_human_audit(db, access, "dream.insight", "user", access.principal.username, None,
                        {"run_id": str(run.id), "insight": insight_id, "kind": insight["kind"],
                         "decision": body.decision})
    await db.commit()
    return insight


@router.post("/api/dreams/{run_id}/insights/{insight_id}/consolidate", status_code=201)
async def consolidate(run_id: uuid.UUID, insight_id: str, body: ConsolidateBody, db: DbDep, access: AccessDep,
                      settings: SettingsDep, embedding: EmbeddingDep):
    """Write one consolidated memory for a theme, through the normal write path and curation."""
    _require_labs(access, settings)
    run, insight = await _insight(db, access, run_id, insight_id)
    if insight["action"]["type"] != "consolidate":
        raise HTTPException(status_code=422, detail="Only a theme is consolidated")
    username = access.principal.username
    if body.scope == "user":
        if body.scope_id is not None:
            raise HTTPException(status_code=422, detail="A personal memory has no scope_id")
    elif not body.scope_id or not await _membership(db, access, body.scope_id):
        raise HTTPException(status_code=403, detail="Project write forbidden")
    sources = [m["id"] for m in insight["memories"]]
    data = MemoryNodeCreate(
        content=body.content, scope=body.scope, scope_id=body.scope_id, weight=body.weight, owner_id=username,
        actor_id=username, driver_id=username, domains=[insight["action"]["domain"]],
        metadata={"consolidated_from": sources, "dream_insight": insight_id},
    )
    _record_human_audit(db, access, "memory.write", body.scope, username,
                        metadata={"scope_id": body.scope_id, "dreaming": insight_id})
    created, curation = await core_create_memory(data, db, embedding, tenant_id=access.principal.tenant_id)
    if created is None:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Curation blocked write: {curation.get('reason') or 'blocked'}")
    new = await db.get(MemoryNode, created.id)
    for source_id in sources:
        source = await db.get(MemoryNode, uuid.UUID(source_id))
        if source is not None and source.is_current and source.deleted_at is None and access.can_read(source) \
                and _same_space(new, source):
            await create_relationship(RelationshipCreate(
                source_id=new.id, target_id=source.id, relationship_type=RelationshipType.derived_from,
                created_by=username), db)
    insight = {**insight, "status": "accepted", "result": "Consolidated memory written.",
               "memory_id": str(new.id), "decided_at": datetime.now(UTC).isoformat()}
    _store(run, insight)
    await db.commit()
    return insight


async def _main() -> None:  # pragma: no cover - CronJob entry point
    from src.database import _get_session_factory

    settings = get_settings()
    if not settings.ui_labs:
        logger.info("Labs is off; no Dreaming runs")
        return
    embedding = get_embedding_service(settings) if settings.embedding_url else None
    ran = await run_due(_get_session_factory(settings), settings, embedding, limit=1000)
    logger.info("Dreaming: %d scheduled run(s)", ran)


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
