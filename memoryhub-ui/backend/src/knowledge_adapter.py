"""Knowledge (#715, Labs): documents, context packs and knowledge pages.

- **Documents** (``/api/documents``): text documents a person uploads (owner
  edits; members of the document's project read). New versions keep the old
  ones; memories written from spans of a document are linked to their passage,
  and each new version checks every link: the passage is in place (``ok``),
  elsewhere (``moved``), or gone (``changed``). Memories are written through
  the normal write path, so curation rules apply.
- **Context packs** (``/api/context-packs``): pinned memories and saved
  searches rendered within a token budget (``memoryhub_core.services.context_pack``).
  The owner attaches packs to their own agents; agents receive them through
  MCP ``memory(action="packs")`` with their own read checks.
- **Knowledge pages** (``/api/knowledge``): a page for the person and for each
  of their projects, built from the memories they may read when it is opened:
  facts grouped by topic (identical ones folded), highlights, entities,
  recent changes, open questions (contradictions) and stale facts.
- **Shared knowledge** (``/api/knowledge/shared``): published shared-knowledge
  memories the person may read (requests from #719).
"""

from __future__ import annotations

import re
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from memoryhub_core.models.contradiction import ContradictionReport
from memoryhub_core.models.knowledge import ContextPack, Document, DocumentLink, DocumentVersion
from memoryhub_core.models.memory import MemoryNode, MemoryRelationship
from memoryhub_core.models.schemas import MemoryNodeCreate
from memoryhub_core.services.context_pack import MAX_PINNED, MAX_QUERIES, render_pack
from memoryhub_core.services.memory import create_memory as core_create_memory
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.authorization import Access, AccessDep
from src.config import Settings, get_settings
from src.database import get_db
from src.human_adapters import EmbeddingDep, _membership, _record_human_audit, get_embedding_service

router = APIRouter()
DbDep = Annotated[AsyncSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

MAX_DOCUMENT_CHARS = 2_000_000  # the page accepts files up to 2 MB
MAX_SPANS = 50
PAGE_SOURCES = 500
RECENT_DAYS = 14
STALE_DAYS = 90
SECTION_FACTS = 12


def _labs(access: Access, settings: Settings) -> None:
    if not settings.ui_labs:
        raise HTTPException(status_code=404, detail="Knowledge is a Labs feature and Labs is off")
    if access.principal.role != "user":
        raise HTTPException(status_code=403, detail="Knowledge works over a user's own and shared memories")


def _optional_embedding(settings: Settings):
    try:
        return get_embedding_service(settings) if settings.embedding_url else None
    except HTTPException:
        return None


# =========================================================================== documents


class DocumentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=MAX_DOCUMENT_CHARS)
    mime: str = Field(default="text/plain", pattern=r"^(text/[\w.+-]+|application/json)$", max_length=100)
    project: str | None = Field(default=None, max_length=255)


class DocumentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=500)
    body: str | None = Field(default=None, min_length=1, max_length=MAX_DOCUMENT_CHARS)


class Span(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    content: str | None = Field(default=None, max_length=20000)


class MemoriesFromDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spans: list[Span] = Field(min_length=1, max_length=MAX_SPANS)
    scope: Literal["user", "project"] = "user"
    scope_id: str | None = Field(default=None, max_length=255)
    weight: float = Field(default=0.7, ge=0, le=1)
    domains: list[str] = Field(default_factory=list, max_length=20)


def _passages(body: str) -> list[dict]:
    """Blocks of text separated by blank lines, with their offsets."""
    out = []
    for match in re.finditer(r"[^\n]+(?:\n(?![ \t]*\n)[^\n]*)*", body):
        text = match.group(0)
        stripped = text.rstrip()
        if not stripped.strip():
            continue
        out.append({"i": len(out), "start": match.start(), "end": match.start() + len(stripped), "text": stripped})
    return out


def _doc_readable(access: Access, doc: Document) -> bool:
    return (doc.tenant_id == access.principal.tenant_id and doc.deleted_at is None
            and (doc.owner == access.principal.username or (doc.project is not None
                                                            and doc.project in access.human_projects)))


async def _document(db: AsyncSession, access: Access, doc_id: uuid.UUID, *, edit: bool = False) -> Document:
    doc = await db.get(Document, doc_id, with_for_update=edit)
    if doc is None or not _doc_readable(access, doc):
        raise HTTPException(status_code=404, detail="Document not found")
    if edit and doc.owner != access.principal.username:
        raise HTTPException(status_code=403, detail="Only the owner changes a document")
    return doc


def _doc_row(doc: Document, memories: int, changed: int, access: Access) -> dict:
    return {"id": str(doc.id), "owner": doc.owner, "title": doc.title, "mime": doc.mime, "size": len(doc.body),
            "version": doc.version, "project": doc.project, "created_at": doc.created_at.isoformat(),
            "updated_at": doc.updated_at.isoformat(), "memories": memories, "changed": changed,
            "mine": doc.owner == access.principal.username}


def _visible_docs(access: Access):
    return (Document.tenant_id == access.principal.tenant_id, Document.deleted_at.is_(None),
            or_(Document.owner == access.principal.username,
                Document.project.in_(sorted(access.human_projects))))


@router.get("/api/documents")
async def list_documents(db: DbDep, access: AccessDep, settings: SettingsDep):
    _labs(access, settings)
    docs = (await db.execute(select(Document).where(*_visible_docs(access))
                             .order_by(Document.updated_at.desc()))).scalars().all()
    stats: dict[uuid.UUID, Counter] = defaultdict(Counter)
    if docs:
        for doc_id, status, n in (await db.execute(select(DocumentLink.document_id, DocumentLink.status, func.count())
                                                   .where(DocumentLink.document_id.in_([d.id for d in docs]))
                                                   .group_by(DocumentLink.document_id, DocumentLink.status))).all():
            stats[doc_id][status] += n
    return {"items": [_doc_row(d, sum(stats[d.id].values()), stats[d.id]["changed"], access) for d in docs]}


@router.post("/api/documents", status_code=201)
async def create_document(body: DocumentBody, db: DbDep, access: AccessDep, settings: SettingsDep):
    _labs(access, settings)
    if body.project is not None and not await _membership(db, access, body.project):
        raise HTTPException(status_code=403, detail="You can only share a document with a project you belong to")
    now = datetime.now(UTC)
    doc = Document(id=uuid.uuid4(), tenant_id=access.principal.tenant_id, owner=access.principal.username,
                   title=body.title.strip(), mime=body.mime, body=body.body, version=1, project=body.project,
                   created_at=now, updated_at=now)
    db.add(doc)
    _record_human_audit(db, access, "document.created", "user", doc.owner, None,
                        {"document_id": str(doc.id), "title": doc.title, "project": doc.project})
    await db.commit()
    return {"id": str(doc.id)}


@router.get("/api/documents/{doc_id}")
async def document_detail(doc_id: uuid.UUID, db: DbDep, access: AccessDep, settings: SettingsDep):
    _labs(access, settings)
    doc = await _document(db, access, doc_id)
    links = (await db.execute(select(DocumentLink).where(DocumentLink.document_id == doc.id)
                              .order_by(DocumentLink.start))).scalars().all()
    nodes = {}
    if links:
        nodes = {n.id: n for n in (await db.execute(select(MemoryNode).where(
            MemoryNode.id.in_([link.memory_id for link in links])))).scalars().all()}
    visible = []
    for link in links:
        node = nodes.get(link.memory_id)
        if node is None or node.deleted_at is not None or not access.can_read(node):
            continue  # memories the viewer may not read are not shown, not even counted
        visible.append({"memory_id": str(link.memory_id), "start": link.start, "end": link.end, "status": link.status,
                        "doc_version": link.doc_version, "stub": node.stub, "scope": node.scope,
                        "is_current": node.is_current})
    passages = _passages(doc.body)
    for passage in passages:
        passage["memories"] = [v["memory_id"] for v in visible if v["status"] != "changed"
                               and v["start"] < passage["end"] and v["end"] > passage["start"]]
    return {"id": str(doc.id), "owner": doc.owner, "title": doc.title, "mime": doc.mime, "body": doc.body,
            "version": doc.version, "project": doc.project, "created_at": doc.created_at.isoformat(),
            "updated_at": doc.updated_at.isoformat(), "passages": passages, "links": visible,
            "mine": doc.owner == access.principal.username}


@router.put("/api/documents/{doc_id}")
async def update_document(doc_id: uuid.UUID, body: DocumentUpdate, db: DbDep, access: AccessDep,
                          settings: SettingsDep):
    """A new title, or a new version of the text; links to the old text are checked again."""
    _labs(access, settings)
    doc = await _document(db, access, doc_id, edit=True)
    reprocessed = None
    if body.body is not None and body.body != doc.body:
        db.add(DocumentVersion(document_id=doc.id, version=doc.version, title=doc.title, body=doc.body))
        doc.body, doc.version = body.body, doc.version + 1
        counts = Counter()
        for link in (await db.execute(select(DocumentLink).where(DocumentLink.document_id == doc.id)
                                      .with_for_update())).scalars().all():
            if doc.body[link.start:link.end] == link.passage:
                link.status = "ok"
            elif (found := doc.body.find(link.passage)) >= 0:
                link.start, link.end, link.status = found, found + len(link.passage), "moved"
            else:
                link.status = "changed"
            counts[link.status] += 1
        reprocessed = {"ok": counts["ok"], "moved": counts["moved"], "changed": counts["changed"]}
    if body.title is not None:
        doc.title = body.title.strip()
    doc.updated_at = datetime.now(UTC)
    _record_human_audit(db, access, "document.updated", "user", doc.owner, None,
                        {"document_id": str(doc.id), "version": doc.version, "reprocessed": reprocessed})
    await db.commit()
    return {"version": doc.version, "reprocessed": reprocessed}


@router.delete("/api/documents/{doc_id}", status_code=204)
async def delete_document(doc_id: uuid.UUID, db: DbDep, access: AccessDep, settings: SettingsDep):
    _labs(access, settings)
    doc = await _document(db, access, doc_id, edit=True)
    doc.deleted_at = datetime.now(UTC)
    _record_human_audit(db, access, "document.deleted", "user", doc.owner, None, {"document_id": str(doc.id)})
    await db.commit()
    return Response(status_code=204)


@router.post("/api/documents/{doc_id}/memories")
async def memories_from_document(doc_id: uuid.UUID, body: MemoriesFromDocument, db: DbDep, access: AccessDep,
                                 settings: SettingsDep, embedding: EmbeddingDep):
    """Write one memory per span, through curation; each memory is linked to its passage."""
    _labs(access, settings)
    doc = await _document(db, access, doc_id)
    if body.scope == "user":
        if body.scope_id is not None:
            raise HTTPException(status_code=422, detail="A personal memory has no scope_id")
    elif not body.scope_id or not await _membership(db, access, body.scope_id):
        raise HTTPException(status_code=403, detail="Project write forbidden")
    username = access.principal.username
    created, failed = [], []
    for span in body.spans:
        if span.end > len(doc.body) or span.start >= span.end:
            failed.append({"start": span.start, "detail": "The span is outside the document"})
            continue
        passage = doc.body[span.start:span.end]
        content = (span.content or passage).strip()
        if not content:
            failed.append({"start": span.start, "detail": "The span is empty"})
            continue
        data = MemoryNodeCreate(
            content=content, scope=body.scope, scope_id=body.scope_id, weight=body.weight, owner_id=username,
            actor_id=username, driver_id=username, domains=body.domains or None,
            metadata={"source": {"document_id": str(doc.id), "title": doc.title, "version": doc.version,
                                 "start": span.start, "end": span.end}},
        )
        node, curation = await core_create_memory(data, db, embedding, tenant_id=access.principal.tenant_id)
        if node is None:
            reason = curation.get("reason") or "blocked"
            failed.append({"start": span.start, "detail": f"Curation blocked it: {reason}"})
            continue
        db.add(DocumentLink(id=uuid.uuid4(), tenant_id=access.principal.tenant_id, document_id=doc.id,
                            memory_id=node.id, start=span.start, end=span.end, passage=passage,
                            doc_version=doc.version, status="ok"))
        _record_human_audit(db, access, "memory.write", body.scope, username, node.id,
                            {"scope_id": body.scope_id, "document_id": str(doc.id)})
        created.append(str(node.id))
    await db.commit()
    return {"created": created, "failed": failed}


# =========================================================================== context packs


class PackQueryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=500)
    max_results: int = Field(default=5, ge=1, le=50)
    weight_threshold: float = Field(default=0, ge=0, le=1)
    project: str | None = Field(default=None, max_length=255)


class PackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    pinned: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_PINNED)
    queries: list[PackQueryBody] = Field(default_factory=list, max_length=MAX_QUERIES)
    token_budget: int = Field(default=2000, ge=200, le=32000)


class PackPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    pinned: list[uuid.UUID] | None = Field(default=None, max_length=MAX_PINNED)
    queries: list[PackQueryBody] | None = Field(default=None, max_length=MAX_QUERIES)
    token_budget: int | None = Field(default=None, ge=200, le=32000)
    visibility: Literal["private", "project"] | None = None
    project: str | None = Field(default=None, max_length=255)
    agents: list[str] | None = Field(default=None, max_length=50)

    @field_validator("agents")
    @classmethod
    def _unique(cls, value: list[str] | None) -> list[str] | None:
        return sorted(set(value)) if value is not None else None


def _visible_packs(access: Access):
    return (ContextPack.tenant_id == access.principal.tenant_id,
            or_(ContextPack.owner == access.principal.username,
                (ContextPack.visibility == "project")
                & ContextPack.project.in_(sorted(access.human_projects))))


def _pack_row(pack: ContextPack, access: Access) -> dict:
    mine = pack.owner == access.principal.username
    return {"id": str(pack.id), "owner": pack.owner, "name": pack.name, "description": pack.description,
            "pinned": list(pack.pinned or []), "queries": list(pack.queries or []), "token_budget": pack.token_budget,
            "visibility": pack.visibility, "project": pack.project, "read_only": not mine,
            "agents": list(pack.agents or []) if mine else [], "created_at": pack.created_at.isoformat(),
            "updated_at": pack.updated_at.isoformat(), "last_render": pack.last_render, "mine": mine,
            "can_edit": mine}


async def _pack(db: AsyncSession, access: Access, pack_id: uuid.UUID, *, edit: bool = False) -> ContextPack:
    pack = (await db.execute(select(ContextPack).where(ContextPack.id == pack_id, *_visible_packs(access))
                             .with_for_update(of=ContextPack) if edit else
                             select(ContextPack).where(ContextPack.id == pack_id, *_visible_packs(access)))
            ).scalar_one_or_none()
    if pack is None:
        raise HTTPException(status_code=404, detail="Context pack not found")
    if edit and pack.owner != access.principal.username:
        raise HTTPException(status_code=403, detail="Only the owner changes a context pack")
    return pack


async def _check_pinned(db: AsyncSession, access: Access, pinned: list[uuid.UUID]) -> list[str]:
    if not pinned:
        return []
    nodes = {n.id: n for n in (await db.execute(select(MemoryNode).where(MemoryNode.id.in_(pinned)))).scalars()}
    for memory_id in pinned:
        node = nodes.get(memory_id)
        if node is None or node.deleted_at is not None or not access.can_read(node):
            raise HTTPException(status_code=422, detail=f"Memory {memory_id} is not one you can read")
    return [str(m) for m in dict.fromkeys(pinned)]


def _check_queries(access: Access, queries: list[PackQueryBody]) -> list[dict]:
    for query in queries:
        if query.project is not None and query.project not in access.human_projects:
            raise HTTPException(status_code=422, detail=f"You are not a member of project {query.project}")
    return [q.model_dump() for q in queries]


@router.get("/api/context-packs")
async def list_packs(db: DbDep, access: AccessDep, settings: SettingsDep):
    _labs(access, settings)
    packs = (await db.execute(select(ContextPack).where(*_visible_packs(access))
                              .order_by(ContextPack.updated_at.desc()))).scalars().all()
    return {"items": [_pack_row(p, access) for p in packs]}


@router.post("/api/context-packs", status_code=201)
async def create_pack(body: PackBody, db: DbDep, access: AccessDep, settings: SettingsDep):
    _labs(access, settings)
    now = datetime.now(UTC)
    pack = ContextPack(id=uuid.uuid4(), tenant_id=access.principal.tenant_id, owner=access.principal.username,
                       name=body.name.strip(), description=body.description,
                       pinned=await _check_pinned(db, access, body.pinned),
                       queries=_check_queries(access, body.queries), token_budget=body.token_budget,
                       visibility="private", agents=[], created_at=now, updated_at=now)
    db.add(pack)
    _record_human_audit(db, access, "pack.created", "user", pack.owner, None,
                        {"pack_id": str(pack.id), "name": pack.name})
    await db.commit()
    return {"id": str(pack.id)}


async def _render(db: AsyncSession, access: Access, pack: ContextPack, settings: Settings) -> dict:
    async def readable(node: MemoryNode) -> bool:
        return access.can_read(node)
    return await render_pack(db, pack, readable, _optional_embedding(settings), rendered_for=access.principal.username)


@router.get("/api/context-packs/{pack_id}")
async def pack_detail(pack_id: uuid.UUID, db: DbDep, access: AccessDep, settings: SettingsDep):
    _labs(access, settings)
    pack = await _pack(db, access, pack_id)
    render = await _render(db, access, pack, settings)
    if pack.owner == access.principal.username:
        pack.last_render = {"at": render["rendered_at"], "tokens": render["tokens"], "items": len(render["items"])}
        await db.commit()
    return {**_pack_row(pack, access), "render": render, "my_agents": sorted(access.agents)}


@router.get("/api/context-packs/{pack_id}/markdown")
async def pack_markdown(pack_id: uuid.UUID, db: DbDep, access: AccessDep, settings: SettingsDep):
    _labs(access, settings)
    pack = await _pack(db, access, pack_id)
    render = await _render(db, access, pack, settings)
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", pack.name).strip("-") or "context-pack"
    return Response(render["markdown"], media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}.md"', "Cache-Control": "no-store"})


@router.patch("/api/context-packs/{pack_id}")
async def update_pack(pack_id: uuid.UUID, body: PackPatch, db: DbDep, access: AccessDep, settings: SettingsDep):
    _labs(access, settings)
    pack = await _pack(db, access, pack_id, edit=True)
    changes = body.model_dump(exclude_unset=True)
    if "name" in changes:
        pack.name = body.name.strip()
    if "description" in changes:
        pack.description = body.description
    if body.pinned is not None:
        pack.pinned = await _check_pinned(db, access, body.pinned)
    if body.queries is not None:
        pack.queries = _check_queries(access, body.queries)
    if body.token_budget is not None:
        pack.token_budget = body.token_budget
    if "visibility" in changes or "project" in changes:
        visibility = body.visibility or pack.visibility
        project = body.project if "project" in changes else pack.project
        if visibility == "project" and (not project or not await _membership(db, access, project)):
            raise HTTPException(status_code=403, detail="Share a pack only with a project you belong to")
        pack.visibility, pack.project = visibility, project if visibility == "project" else None
    if body.agents is not None:
        # Only the owner's own agents; agents read the pack through MCP with their own grants.
        foreign = set(body.agents) - access.agents
        if foreign:
            raise HTTPException(status_code=422, detail=f"Not your agents: {', '.join(sorted(foreign))}")
        pack.agents = body.agents
    pack.updated_at = datetime.now(UTC)
    _record_human_audit(db, access, "pack.updated", "user", pack.owner, None,
                        {"pack_id": str(pack.id), "fields": sorted(changes), "agents": pack.agents})
    await db.commit()
    return {"ok": True}


@router.delete("/api/context-packs/{pack_id}", status_code=204)
async def delete_pack(pack_id: uuid.UUID, db: DbDep, access: AccessDep, settings: SettingsDep):
    _labs(access, settings)
    pack = await _pack(db, access, pack_id, edit=True)
    await db.delete(pack)
    _record_human_audit(db, access, "pack.deleted", "user", access.principal.username, None,
                        {"pack_id": str(pack_id)})
    await db.commit()
    return Response(status_code=204)


# =========================================================================== knowledge pages


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


async def _subject_nodes(db: AsyncSession, access: Access, kind: str, subject_id: str) -> tuple[list[MemoryNode], bool]:
    stmt = select(MemoryNode).where(MemoryNode.is_current.is_(True), MemoryNode.deleted_at.is_(None),
                                    MemoryNode.status == "active", access.clause())
    if kind == "me":
        stmt = stmt.where(MemoryNode.scope == "user", MemoryNode.owner_id.in_(access.identities))
    else:
        if subject_id not in access.human_projects:
            raise HTTPException(status_code=404, detail="Project not found")
        stmt = stmt.where(MemoryNode.scope == "project", MemoryNode.scope_id == subject_id)
    rows = (await db.execute(stmt.order_by(MemoryNode.weight.desc(), MemoryNode.updated_at.desc())
                             .limit(PAGE_SOURCES + 1))).scalars().all()
    rows = [n for n in rows if access.can_read(n)]
    return rows[:PAGE_SOURCES], len(rows) > PAGE_SOURCES


async def _build_page(db: AsyncSession, access: Access, kind: str, subject_id: str) -> dict:
    now = datetime.now(UTC)
    nodes, truncated = await _subject_nodes(db, access, kind, subject_id)
    contradicted: dict[uuid.UUID, int] = {}
    if nodes:
        contradicted = dict((await db.execute(select(ContradictionReport.memory_id, func.count()).where(
            ContradictionReport.memory_id.in_([n.id for n in nodes]), ContradictionReport.resolved.is_(False))
            .group_by(ContradictionReport.memory_id))).all())
    groups: dict[str, list[MemoryNode]] = defaultdict(list)
    for node in nodes:
        groups[_norm(node.content)[:300]].append(node)
    facts_by_section: dict[str, list[dict]] = defaultdict(list)
    all_facts = []
    for members in groups.values():
        lead = max(members, key=lambda n: (n.weight, n.updated_at or now))
        fact = {"text": lead.stub or lead.content[:200], "memory_id": str(lead.id),
                "memory_ids": [str(m.id) for m in members], "similar": len(members) - 1, "scope": lead.scope,
                "weight": lead.weight, "updated_at": (lead.updated_at or now).isoformat(),
                "stale": bool(lead.updated_at and lead.updated_at < now - timedelta(days=STALE_DAYS)
                              and lead.weight >= 0.5),
                "contradicted": sum(contradicted.get(m.id, 0) for m in members)}
        all_facts.append(fact)
        section = sorted(lead.domains)[0] if lead.domains else "General"
        facts_by_section[section].append(fact)
    sections = [{"title": title, "facts": sorted(facts, key=lambda f: -f["weight"])[:SECTION_FACTS]}
                for title, facts in sorted(facts_by_section.items(), key=lambda kv: (-len(kv[1]), kv[0]))]
    entities = []
    if nodes:
        rows = (await db.execute(select(MemoryNode.id, MemoryNode.content, MemoryNode.branch_type, func.count())
                                 .join(MemoryRelationship, MemoryRelationship.target_id == MemoryNode.id)
                                 .where(MemoryRelationship.source_id.in_([n.id for n in nodes]),
                                        MemoryRelationship.relationship_type == "mentions",
                                        MemoryRelationship.valid_until.is_(None), MemoryNode.scope == "entity",
                                        MemoryNode.deleted_at.is_(None))
                                 .group_by(MemoryNode.id).order_by(func.count().desc()).limit(12))).all()
        entities = [{"id": str(i), "name": name, "type": (bt or "").removeprefix("entity:"), "count": n}
                    for i, name, bt, n in rows]
    recent = sorted((n for n in nodes if n.updated_at and n.updated_at >= now - timedelta(days=RECENT_DAYS)),
                    key=lambda n: n.updated_at, reverse=True)[:10]
    questions = [{"kind": "contradiction", "memory_id": str(n.id), "text": n.stub, "count": contradicted[n.id]}
                 for n in nodes if contradicted.get(n.id)]
    stale = [f for f in sorted(all_facts, key=lambda f: f["updated_at"]) if f["stale"]][:10]
    title = "What memory knows about you" if kind == "me" else subject_id
    return {
        "subject": {"kind": kind, "id": subject_id, "title": title},
        "generated_at": now.isoformat(), "rebuilt": True, "truncated": truncated, "source_count": len(nodes),
        "last_source_change": max((n.updated_at for n in nodes if n.updated_at), default=None),
        "summary": {"facts": len(all_facts), "folded": sum(f["similar"] for f in all_facts),
                    "sections": len(sections), "entities": len(entities), "open_questions": len(questions),
                    "stale": len(stale)},
        "highlights": sorted(all_facts, key=lambda f: -f["weight"])[:5],
        "sections": sections, "entities": entities,
        "recent": [{"memory_id": str(n.id), "text": n.stub, "updated_at": n.updated_at.isoformat(),
                    "change": "new" if n.version == 1 else "updated", "version": n.version} for n in recent],
        "questions": questions, "stale": stale, "changes": None,
        "scopes": dict(Counter(n.scope for n in nodes)),
    }


@router.get("/api/knowledge")
async def knowledge_index(db: DbDep, access: AccessDep, settings: SettingsDep):
    _labs(access, settings)
    items = []
    for kind, subject_id in [("me", "me")] + [("project", p) for p in sorted(access.human_projects)]:
        page = await _build_page(db, access, kind, subject_id)
        items.append({"kind": kind, "id": subject_id, "title": page["subject"]["title"],
                      "source_count": page["source_count"], "generated_at": page["generated_at"], "fresh": True,
                      "summary": page["summary"], "sections": [s["title"] for s in page["sections"][:4]]})
    return {"items": items}


@router.get("/api/knowledge/shared")
async def shared_knowledge(db: DbDep, access: AccessDep, settings: SettingsDep, q: str | None = None,
                           scope: Literal["project", "organizational", "campaign"] | None = None,
                           audience: Literal["mine", "all"] = "all", sort: Literal["updated", "title"] = "updated",
                           limit: Annotated[int, Query(ge=1, le=500)] = 200):
    _labs(access, settings)
    stmt = select(MemoryNode).where(
        MemoryNode.is_current.is_(True), MemoryNode.deleted_at.is_(None), access.clause(),
        MemoryNode.scope.in_(("project", "organizational", "campaign")),
        MemoryNode.metadata_.op("->>")("shared_knowledge").is_not(None),
    )
    if scope:
        stmt = stmt.where(MemoryNode.scope == scope)
    if q:
        stmt = stmt.where(MemoryNode.content.ilike(f"%{q.replace('%', '').replace('_', '')}%"))
    stmt = stmt.order_by(MemoryNode.updated_at.desc() if sort == "updated" else MemoryNode.stub)
    rows = [n for n in (await db.execute(stmt.limit(limit))).scalars().all() if access.can_read(n)]
    items = []
    for node in rows:
        meta = node.metadata_ or {}
        shared = meta.get("shared_knowledge") or {}
        submitted_by = shared.get("submitted_by") or node.owner_id
        if audience == "mine" and submitted_by != access.principal.username and node.owner_id not in access.identities:
            continue
        items.append({
            "id": str(node.id), "memory_id": str(node.id), "title": node.stub,
            "subtitle": ", ".join(node.domains or []) or None, "scope": node.scope, "scope_id": node.scope_id,
            "scope_label": node.scope_id if node.scope == "project" else None,
            "source": f"Request {shared['request_id']}" if shared.get("request_id") else "Published",
            "version": node.version,
            "audience": "Organization" if node.scope == "organizational" else f"Project {node.scope_id}",
            "relevant_until": node.relevant_until.isoformat() if node.relevant_until else None,
            "updated_at": node.updated_at.isoformat(),
        })
    return {"items": items, "total": len(items)}


@router.get("/api/knowledge/{kind}/{subject_id}")
async def knowledge_page(kind: Literal["me", "project"], subject_id: str, db: DbDep, access: AccessDep,
                         settings: SettingsDep, refresh: bool = False):
    """Built from the current memories whenever it is opened; ``refresh`` is accepted for the page's button."""
    _labs(access, settings)
    return await _build_page(db, access, kind, "me" if kind == "me" else subject_id)


@router.get("/api/nav-features")
async def nav_features(db: DbDep, access: AccessDep, settings: SettingsDep):
    """Counts for the Knowledge tabs; empty when Knowledge is not available to the caller."""
    if not settings.ui_labs or access.principal.role != "user":
        return {}
    documents = (await db.execute(
        select(func.count()).select_from(Document).where(*_visible_docs(access)))).scalar_one()
    packs = (await db.execute(
        select(func.count()).select_from(ContextPack).where(*_visible_packs(access)))).scalar_one()
    return {"knowledge": 1 + len(access.human_projects), "documents": int(documents), "packs": int(packs)}
