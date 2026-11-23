"""My requests (#719): promotion and shared-knowledge requests.

A person asks for one of their memories to reach a wider audience:

- ``promotion``: publish a copy at a broader scope (personal → project or
  organizational, project → organizational). The copy is linked to the
  source with ``derived_from`` (``memoryhub_core.services.promotion``).
- ``shared_knowledge``: the same publication, marked as curated shared
  knowledge (``metadata.shared_knowledge``) for the knowledge pages.

The request keeps a snapshot of the submitted version. The reviewer reads that
snapshot, never the live memory, and only through the request's own detail
route, which writes ``request.read`` so the requester sees the read in My
activity. Approval publishes exactly the submitted version: a memory changed
or deleted since submission is refused with 409.

Who decides: a project's admins for a project target, a dashboard Admin for an
organizational one (and may also decide project targets). Nobody decides their
own request. A withdrawn request leaves every review queue.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from memoryhub_core.models.memory import MemoryNode
from memoryhub_core.models.project import Project
from memoryhub_core.services.exceptions import MemoryNotFoundError
from memoryhub_core.services.promotion import promote_memory
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    and_,
    false,
    func,
    or_,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import declarative_base

from src.authorization import Access, AccessDep
from src.database import get_db
from src.human_adapters import EmbeddingDep, _membership, _record_human_audit

router = APIRouter()
DbDep = Annotated[AsyncSession, Depends(get_db)]
RequestBase = declarative_base()

KINDS = ("promotion", "shared_knowledge")
TARGETS = ("project", "organizational")
# Personal → project → organizational; the core promotion order without enterprise.
BROADER = {"user": {"project", "organizational"}, "project": {"organizational"}}
SCOPE_LABEL = {"user": "Personal", "project": "Project", "organizational": "Organizational"}
TITLE_CHARS = 120


class MemoryRequest(RequestBase):
    __tablename__ = "memoryhub_ui_requests"
    id = Column(String(16), primary_key=True)
    tenant_id = Column(String(255), nullable=False)
    kind = Column(String(32), nullable=False)
    requester = Column(String(255), nullable=False)
    memory_id = Column(Uuid, nullable=False)
    memory_version = Column(Integer, nullable=False)
    snapshot_content = Column(Text, nullable=False)
    from_scope = Column(String(32), nullable=False)
    from_scope_id = Column(String(255))
    target_scope = Column(String(32), nullable=False)
    target_scope_id = Column(String(255))
    reason = Column(Text)
    state = Column(String(16), nullable=False, default="open")
    outcome = Column(String(16))
    decided_by = Column(String(255))
    decided_by_role = Column(String(32))
    decision_reason = Column(Text)
    decided_at = Column(DateTime(timezone=True))
    withdrawn_at = Column(DateTime(timezone=True))
    result_memory_id = Column(Uuid)
    submitted_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    __table_args__ = (
        CheckConstraint("state in ('open', 'decided', 'withdrawn')", name="ck_memoryhub_ui_requests_state"),
        Index("ix_memoryhub_ui_requests_requester", "tenant_id", "requester"),
        Index("ix_memoryhub_ui_requests_open", "tenant_id", "state", "target_scope"),
    )


class NewRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["promotion", "shared_knowledge"]
    memory_id: uuid.UUID
    target_scope: Literal["project", "organizational"]
    project: str | None = Field(default=None, min_length=1, max_length=255)
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _project_for_project_target(self) -> NewRequestBody:
        if (self.target_scope == "project") != (self.project is not None):
            raise ValueError("project is required for a project target and only for it")
        return self


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["approved", "rejected"]
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _reason_for_rejection(self) -> DecisionBody:
        if self.outcome == "rejected" and not (self.reason or "").strip():
            raise ValueError("a rejection needs a reason the requester can read")
        return self


def _title(content: str) -> str:
    line = " ".join(content.split())
    return line if len(line) <= TITLE_CHARS else line[: TITLE_CHARS - 1].rstrip() + "…"


def _scope_label(scope: str, scope_id: str | None) -> str:
    label = SCOPE_LABEL.get(scope, scope)
    return f"{label} · {scope_id}" if scope == "project" and scope_id else label


def _waiting_for(r: MemoryRequest) -> str | None:
    if r.state != "open":
        return None
    return "project_owner" if r.target_scope == "project" else "review"


def _mine(r: MemoryRequest) -> dict:
    """The requester's view: their own snapshot title and the decision."""
    return {
        "id": r.id,
        "kind": r.kind,
        "title": _title(r.snapshot_content),
        "memory_id": str(r.memory_id),
        "memory_version": r.memory_version,
        "from_scope": r.from_scope,
        "from_label": _scope_label(r.from_scope, r.from_scope_id),
        "to_scope": r.target_scope,
        "to_label": _scope_label(r.target_scope, r.target_scope_id),
        "project": r.target_scope_id if r.target_scope == "project" else None,
        "submitted_at": r.submitted_at.isoformat(),
        "submitted_via": "dashboard",
        "state": r.state,
        "waiting_for": _waiting_for(r),
        "note": r.reason,
        "outcome": r.outcome,
        "decided_by": r.decided_by,
        "decided_by_role": r.decided_by_role,
        "decided_at": r.decided_at.isoformat() if r.decided_at else None,
        "reason": r.decision_reason,
        "withdrawn_at": r.withdrawn_at.isoformat() if r.withdrawn_at else None,
        "result_memory_id": str(r.result_memory_id) if r.result_memory_id else None,
    }


def _queue_row(r: MemoryRequest) -> dict:
    """A reviewer's list row: who asks for what, without the memory's content."""
    return {
        "id": r.id,
        "kind": r.kind,
        "requester": r.requester,
        "from_scope": r.from_scope,
        "from_label": _scope_label(r.from_scope, r.from_scope_id),
        "to_scope": r.target_scope,
        "to_label": _scope_label(r.target_scope, r.target_scope_id),
        "project": r.target_scope_id if r.target_scope == "project" else None,
        "submitted_at": r.submitted_at.isoformat(),
        "has_reason": bool(r.reason),
    }


def _can_decide(access: Access, r: MemoryRequest) -> bool:
    if r.tenant_id != access.principal.tenant_id or r.requester == access.principal.username:
        return False
    if r.target_scope == "project" and r.target_scope_id in access.human_project_admins:
        return True
    return access.principal.role == "admin"


def _decidable(access: Access):
    """SQL for the open requests this person may decide."""
    targets = []
    if access.principal.role == "admin":
        targets.append(MemoryRequest.target_scope.in_(TARGETS))
    elif access.human_project_admins:
        targets.append(and_(MemoryRequest.target_scope == "project",
                            MemoryRequest.target_scope_id.in_(access.human_project_admins)))
    return and_(
        MemoryRequest.tenant_id == access.principal.tenant_id,
        MemoryRequest.state == "open",
        MemoryRequest.requester != access.principal.username,
        or_(*targets) if targets else false(),
    )


async def _new_id(db: AsyncSession) -> str:
    for _ in range(5):
        candidate = f"REQ-{secrets.token_hex(3).upper()}"
        if await db.get(MemoryRequest, candidate) is None:
            return candidate
    raise HTTPException(status_code=503, detail="Could not allocate a request id")


def _audit(db: AsyncSession, access: Access, event: str, r: MemoryRequest, **extra) -> None:
    # Owned by the requester, so the event shows in their My activity.
    _record_human_audit(db, access, event, r.from_scope, r.requester, r.memory_id,
                        {"request_id": r.id, "kind": r.kind, "target_scope": r.target_scope,
                         "target_scope_id": r.target_scope_id, **extra})


@router.post("/api/me/requests", status_code=201)
async def create_request(body: NewRequestBody, db: DbDep, access: AccessDep):
    if access.principal.role != "user":
        raise HTTPException(status_code=403, detail="Only users submit requests")
    node = await db.get(MemoryNode, body.memory_id, with_for_update=True)
    # Only a memory the person may write: their own personal memory or one of their projects'.
    if node is None or not access.can_write(node):
        raise HTTPException(status_code=404, detail="Memory not found")
    if body.target_scope not in BROADER.get(node.scope, set()):
        raise HTTPException(status_code=422, detail=f"A {SCOPE_LABEL.get(node.scope, node.scope).lower()} memory "
                                                    f"cannot be promoted to {body.target_scope}")
    if body.target_scope == "project":
        project = (await db.execute(select(Project).where(
            Project.name == body.project, Project.tenant_id == access.principal.tenant_id))).scalar_one_or_none()
        if project is None or not await _membership(db, access, body.project):
            raise HTTPException(status_code=403, detail="You can only promote into a project you belong to")
    duplicate = (await db.execute(select(MemoryRequest.id).where(
        MemoryRequest.tenant_id == access.principal.tenant_id, MemoryRequest.memory_id == node.id,
        MemoryRequest.kind == body.kind, MemoryRequest.state == "open"))).scalar_one_or_none()
    if duplicate:
        raise HTTPException(status_code=409, detail=f"Request {duplicate} for this memory is already open")
    request = MemoryRequest(
        id=await _new_id(db), tenant_id=access.principal.tenant_id, kind=body.kind,
        requester=access.principal.username, memory_id=node.id, memory_version=node.version,
        snapshot_content=node.content, from_scope=node.scope, from_scope_id=node.scope_id,
        target_scope=body.target_scope, target_scope_id=body.project,
        reason=(body.reason or "").strip() or None, state="open", submitted_at=datetime.now(UTC),
    )
    db.add(request)
    _audit(db, access, "request.submitted", request)
    try:
        await db.commit()
    except IntegrityError as exc:  # a concurrent submission won the one-open-request index
        await db.rollback()
        raise HTTPException(status_code=409, detail="A request for this memory is already open") from exc
    return _mine(request)


@router.get("/api/me/requests")
async def list_my_requests(db: DbDep, access: AccessDep,
                           state: Literal["open", "decided", "withdrawn"] | None = None,
                           limit: Annotated[int, Query(ge=1, le=500)] = 200):
    base = (MemoryRequest.tenant_id == access.principal.tenant_id,
            MemoryRequest.requester == access.principal.username)
    counts = dict((await db.execute(select(MemoryRequest.state, func.count()).where(*base)
                                    .group_by(MemoryRequest.state))).all())
    stmt = select(MemoryRequest).where(*base)
    if state:
        stmt = stmt.where(MemoryRequest.state == state)
    rows = (await db.execute(stmt.order_by(MemoryRequest.submitted_at.desc(), MemoryRequest.id)
                             .limit(limit))).scalars().all()
    to_decide = (await db.execute(select(func.count()).select_from(MemoryRequest)
                                  .where(_decidable(access)))).scalar_one()
    return {
        "items": [_mine(r) for r in rows],
        "counts": {s: int(counts.get(s, 0)) for s in ("open", "decided", "withdrawn")},
        "to_decide": int(to_decide),
    }


async def _own_request(db: AsyncSession, access: Access, request_id: str, *, lock: bool = False) -> MemoryRequest:
    stmt = select(MemoryRequest).where(MemoryRequest.id == request_id,
                                       MemoryRequest.tenant_id == access.principal.tenant_id,
                                       MemoryRequest.requester == access.principal.username)
    if lock:
        stmt = stmt.with_for_update()
    request = (await db.execute(stmt)).scalar_one_or_none()
    if request is None:
        raise HTTPException(status_code=404, detail="Request not found")
    return request


@router.post("/api/me/requests/{request_id}/withdraw")
async def withdraw_request(request_id: str, db: DbDep, access: AccessDep):
    request = await _own_request(db, access, request_id, lock=True)
    if request.state != "open":
        raise HTTPException(status_code=409, detail=f"Request is already {request.state}")
    request.state, request.withdrawn_at = "withdrawn", datetime.now(UTC)
    _audit(db, access, "request.withdrawn", request)
    await db.commit()
    return _mine(request)


@router.get("/api/review/requests")
async def review_queue(db: DbDep, access: AccessDep, limit: Annotated[int, Query(ge=1, le=500)] = 200):
    """Open requests this person may decide. Withdrawn and decided ones never appear."""
    rows = (await db.execute(select(MemoryRequest).where(_decidable(access))
                             .order_by(MemoryRequest.submitted_at, MemoryRequest.id).limit(limit))).scalars().all()
    return {"items": [_queue_row(r) for r in rows], "total": len(rows)}


async def _decidable_request(db: AsyncSession, access: Access, request_id: str, *, lock: bool = False) -> MemoryRequest:
    stmt = select(MemoryRequest).where(MemoryRequest.id == request_id,
                                       MemoryRequest.tenant_id == access.principal.tenant_id)
    if lock:
        stmt = stmt.with_for_update()
    request = (await db.execute(stmt)).scalar_one_or_none()
    # Someone else's request, or one this person may not decide, does not exist for them.
    if request is None or request.state != "open" or not _can_decide(access, request):
        raise HTTPException(status_code=404, detail="Request not found")
    return request


@router.get("/api/review/requests/{request_id}")
async def review_request(request_id: str, db: DbDep, access: AccessDep):
    """The submitted snapshot of one memory: the only content a reviewer reads."""
    request = await _decidable_request(db, access, request_id)
    _audit(db, access, "request.read", request, reviewer=access.principal.username)
    await db.commit()
    return {**_queue_row(request), "content": request.snapshot_content, "memory_version": request.memory_version,
            "reason": request.reason}


@router.post("/api/review/requests/{request_id}/decision")
async def decide_request(request_id: str, body: DecisionBody, db: DbDep, access: AccessDep, embedding: EmbeddingDep):
    request = await _decidable_request(db, access, request_id, lock=True)
    now = datetime.now(UTC)
    if body.outcome == "approved":
        source = await db.get(MemoryNode, request.memory_id, with_for_update=True)
        if (source is None or source.tenant_id != request.tenant_id or source.deleted_at is not None
                or not source.is_current or source.status != "active" or source.version != request.memory_version):
            raise HTTPException(status_code=409, detail="The memory changed or was removed after the request was "
                                                        "submitted; the requester can submit it again")
        if request.target_scope == "project" and (await db.execute(select(Project.name).where(
                Project.name == request.target_scope_id,
                Project.tenant_id == request.tenant_id))).scalar_one_or_none() is None:
            raise HTTPException(status_code=409, detail="The target project no longer exists")
        try:
            promoted = await promote_memory(
                request.memory_id, request.target_scope, db, embedding, tenant_id=request.tenant_id,
                promoted_by=request.requester, target_scope_id=request.target_scope_id,
                project_id=request.target_scope_id,
            )
        except (ValueError, MemoryNotFoundError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        published = await db.get(MemoryNode, promoted.id)
        if published is not None:
            meta = dict(published.metadata_ or {})
            meta["request"] = {"id": request.id, "kind": request.kind, "approved_by": access.principal.username}
            if request.kind == "shared_knowledge":
                meta["shared_knowledge"] = {"request_id": request.id, "submitted_by": request.requester,
                                            "approved_at": now.isoformat()}
            published.metadata_ = meta
        request.result_memory_id = promoted.id
    request.state, request.outcome = "decided", body.outcome
    request.decided_by, request.decided_at = access.principal.username, now
    request.decided_by_role = ("project_admin" if request.target_scope == "project"
                               and request.target_scope_id in access.human_project_admins else "admin")
    request.decision_reason = (body.reason or "").strip() or None
    _audit(db, access, "request.decided", request, outcome=body.outcome,
           result_memory_id=str(request.result_memory_id) if request.result_memory_id else None)
    await db.commit()
    return {**_queue_row(request), "state": request.state, "outcome": request.outcome,
            "result_memory_id": str(request.result_memory_id) if request.result_memory_id else None}
