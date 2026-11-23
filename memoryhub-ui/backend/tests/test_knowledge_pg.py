"""Knowledge (#715) against a real PostgreSQL + pgvector database.

Covers the Labs gate, documents (sharing, memories from spans, links checked
again on a new version), context packs (pinned memories must be readable, only
the owner's own agents, rendering with the viewer's grants), knowledge pages
and shared knowledge.

Uses the repository's integration database (tests/integration/compose.yaml,
localhost:15433) and skips when it is not reachable:

    podman compose -f tests/integration/compose.yaml up -d
    cd memoryhub-ui/backend && pytest tests/test_knowledge_pg.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from memoryhub_core.models.audit import AuditLog
from memoryhub_core.models.knowledge import DocumentVersion
from memoryhub_core.models.memory import MemoryNode
from memoryhub_core.models.project import Project, ProjectMembership
from memoryhub_core.models.schemas import MemoryNodeCreate
from memoryhub_core.services.embeddings import MockEmbeddingService
from memoryhub_core.services.memory import create_memory
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.authorization import Access, Principal, get_access
from src.config import Settings, get_settings
from src.database import get_db
from src.human_adapters import get_embedding_service
from src.main import app

TENANT = "tenant-knowledge"
REPO = Path(__file__).resolve().parents[3]


def _url() -> str:
    env = os.environ.get
    return (f"postgresql+asyncpg://{env('MEMORYHUB_DB_USER', 'memoryhub')}:"
            f"{env('MEMORYHUB_DB_PASSWORD', 'memoryhub-test')}@{env('MEMORYHUB_DB_HOST', 'localhost')}:"
            f"{env('MEMORYHUB_DB_PORT', '15433')}/{env('MEMORYHUB_DB_NAME', 'memoryhub')}")


@pytest.fixture(scope="module")
def database():
    engine = create_async_engine(_url(), poolclass=NullPool)

    async def reachable() -> bool:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("select 1"))
            return True
        except Exception:  # noqa: BLE001
            return False

    if not asyncio.run(reachable()):
        pytest.skip("Integration PostgreSQL (localhost:15433) is not running")
    env = {**os.environ, "MEMORYHUB_DB_PORT": os.environ.get("MEMORYHUB_DB_PORT", "15433"),
           "MEMORYHUB_DB_PASSWORD": os.environ.get("MEMORYHUB_DB_PASSWORD", "memoryhub-test")}
    migrated = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=REPO, env=env,
                              capture_output=True, text=True)
    assert migrated.returncode == 0, migrated.stderr
    yield engine


@pytest.fixture
async def env(database):
    """Project 'atlas': alice member, bob admin, carol member. alice owns agent alice-agent."""
    async with database.begin() as conn:
        for table in ("memoryhub_document_links", "memoryhub_documents", "memoryhub_context_packs", "curator_rules"):
            await conn.execute(text(f"delete from {table} where tenant_id = :t"), {"t": TENANT})
        for table in ("audit_log", "contradiction_reports", "memory_relationships", "memory_nodes"):
            await conn.execute(text(f"delete from {table} where tenant_id = :t"), {"t": TENANT})
        await conn.execute(text("delete from project_memberships where project_id = 'atlas-kn'"))
        await conn.execute(text("delete from projects where name = 'atlas-kn'"))
    sessions = async_sessionmaker(database, expire_on_commit=False)
    async with sessions() as db:
        db.add(Project(name="atlas-kn", tenant_id=TENANT, created_by="bob"))
        await db.flush()
        for user, role in (("alice", "member"), ("bob", "admin"), ("carol", "member")):
            db.add(ProjectMembership(project_id="atlas-kn", user_id=user, role=role, joined_by="bob"))
        await db.commit()
    embedding = MockEmbeddingService()
    who = {"access": None}

    async def db_dep():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = db_dep
    app.dependency_overrides[get_access] = lambda: who["access"]
    labs = {"on": True}
    app.dependency_overrides[get_settings] = lambda: Settings(ui_tenant_id=TENANT, embedding_url="mock",
                                                              ui_labs=labs["on"])
    app.dependency_overrides[get_embedding_service] = lambda: embedding

    def act_as(name: str, role: str = "user") -> None:
        grants = {
            "alice": {"human_projects": {"atlas-kn"}, "agents": {"alice-agent"}},
            "bob": {"human_projects": {"atlas-kn"}, "human_project_admins": {"atlas-kn"}},
            "carol": {"human_projects": {"atlas-kn"}},
            "dave": {},
            "admin": {"human_projects": {"atlas-kn"}, "human_project_admins": {"atlas-kn"},
                      "moderation_scopes": {"project", "organizational"}},
        }[name]
        who["access"] = Access(principal=Principal(name, role, TENANT), **grants)

    async def seed(content: str, *, scope: str = "project", owner: str = "alice", actor: str = "alice-agent",
                   weight: float = 0.5, parent: uuid.UUID | None = None, branch: str | None = None) -> MemoryNode:
        async with sessions() as db:
            created, _ = await create_memory(MemoryNodeCreate(
                content=content, scope=scope, owner_id=owner, actor_id=actor, weight=weight,
                scope_id="atlas-kn" if scope == "project" else None, parent_id=parent, branch_type=branch,
            ), db, embedding, tenant_id=TENANT, skip_curation=True)
            return created

    async def shape(node, *, cos: float = 1.0, axis: int = 1, days_old: int = 0, domains=None, weight=None):
        """Point the memory's embedding at angle acos(cos) from axis 0, towards `axis`."""
        vector = [0.0] * 384
        vector[0], vector[axis] = cos, (1 - cos * cos) ** 0.5
        async with sessions() as db:
            row = await db.get(MemoryNode, node.id)
            row.embedding = vector
            if days_old:
                row.updated_at = row.created_at = datetime.now(UTC) - timedelta(days=days_old)
            if domains is not None:
                row.domains = domains
            if weight is not None:
                row.weight = weight
            await db.commit()
        return node

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield {"http": http, "seed": seed, "act_as": act_as, "sessions": sessions, "shape": shape, "labs": labs,
               "embedding": embedding}
    app.dependency_overrides.clear()



async def test_labs_gate_and_roles(env):
    http, act_as = env["http"], env["act_as"]
    act_as("alice")
    assert (await http.get("/api/nav-features")).json() == {"knowledge": 2, "documents": 0, "packs": 0}
    act_as("admin", "admin")
    assert (await http.get("/api/knowledge")).status_code == 403
    assert (await http.get("/api/nav-features")).json() == {}
    env["labs"]["on"] = False
    act_as("alice")
    for path in ("/api/knowledge", "/api/documents", "/api/context-packs"):
        assert (await http.get(path)).status_code == 404
    assert (await http.get("/api/nav-features")).json() == {}


async def test_documents_memories_and_new_versions(env):
    http, act_as = env["http"], env["act_as"]
    body = "Gripper limit is 11.5 Nm.\n\nCalibrate weekly on Mondays.\n\nNotes end here."
    act_as("dave")
    assert (await http.post("/api/documents", json={"title": "x", "body": "y", "project": "atlas-kn"})).status_code == 403
    act_as("alice")
    doc_id = (await http.post("/api/documents", json={"title": "Atlas runbook", "body": body,
                                                      "project": "atlas-kn"})).json()["id"]
    first, second = body.index("Gripper"), body.index("Calibrate")
    made = (await http.post(f"/api/documents/{doc_id}/memories", json={"spans": [
        {"start": first, "end": first + 25}, {"start": second, "end": second + 28},
        {"start": 0, "end": 10_000}]})).json()
    assert len(made["created"]) == 2 and made["failed"][0]["detail"] == "The span is outside the document"
    async with env["sessions"]() as db:
        node = await db.get(MemoryNode, uuid.UUID(made["created"][0]))
        assert node.content == "Gripper limit is 11.5 Nm." and node.owner_id == "alice" and node.scope == "user"
        assert node.metadata_["source"]["document_id"] == doc_id

    # Carol, a project member, reads the document but not Alice's personal memories, and cannot edit it.
    act_as("carol")
    detail = (await http.get(f"/api/documents/{doc_id}")).json()
    assert detail["links"] == [] and len(detail["passages"]) == 3 and detail["mine"] is False
    assert (await http.put(f"/api/documents/{doc_id}", json={"title": "mine"})).status_code == 403
    act_as("dave")
    assert (await http.get(f"/api/documents/{doc_id}")).status_code == 404

    act_as("alice")
    new_body = "Intro line.\n\nGripper limit is 11.5 Nm.\n\nCalibrate every month."
    result = (await http.put(f"/api/documents/{doc_id}", json={"body": new_body})).json()
    assert result == {"version": 2, "reprocessed": {"ok": 0, "moved": 1, "changed": 1}}
    detail = (await http.get(f"/api/documents/{doc_id}")).json()
    assert {link["status"] for link in detail["links"]} == {"moved", "changed"}
    moved = next(link for link in detail["links"] if link["status"] == "moved")
    assert new_body[moved["start"]:moved["end"]] == "Gripper limit is 11.5 Nm."
    assert detail["passages"][1]["memories"] == [moved["memory_id"]] and detail["passages"][2]["memories"] == []
    async with env["sessions"]() as db:
        old = await db.get(DocumentVersion, (uuid.UUID(doc_id), 1))
        assert old.body == body
    rows = (await http.get("/api/documents")).json()["items"]
    assert rows[0]["memories"] == 2 and rows[0]["changed"] == 1 and rows[0]["version"] == 2
    assert (await http.delete(f"/api/documents/{doc_id}")).status_code == 204
    assert (await http.get("/api/documents")).json()["items"] == []


async def test_context_packs(env):
    http, act_as, seed = env["http"], env["act_as"], env["seed"]
    mine = await seed("Alice prefers metric units", scope="user", owner="alice", actor="alice")
    carols = await seed("Carol's private note", scope="user", owner="carol", actor="carol")
    shared = await seed("Atlas deploys on Fridays", owner="carol", actor="carol")
    act_as("alice")
    assert (await http.post("/api/context-packs", json={"name": "x", "pinned": [str(carols.id)]})).status_code == 422
    assert (await http.post("/api/context-packs", json={
        "name": "x", "queries": [{"query": "q", "project": "other"}]})).status_code == 422
    pack_id = (await http.post("/api/context-packs", json={
        "name": "Atlas basics", "pinned": [str(mine.id), str(shared.id)], "token_budget": 200})).json()["id"]
    assert (await http.patch(f"/api/context-packs/{pack_id}", json={"agents": ["bob-agent"]})).status_code == 422
    assert (await http.patch(f"/api/context-packs/{pack_id}",
                             json={"agents": ["alice-agent"], "visibility": "project", "project": "atlas-kn"})
            ).status_code == 200
    detail = (await http.get(f"/api/context-packs/{pack_id}")).json()
    assert [i["id"] for i in detail["render"]["items"]] == [str(mine.id), str(shared.id)]
    assert detail["agents"] == ["alice-agent"] and detail["my_agents"] == ["alice-agent"]
    markdown = await http.get(f"/api/context-packs/{pack_id}/markdown")
    assert markdown.text.startswith("# Atlas basics") and "metric units" in markdown.text

    # Carol sees the shared pack rendered with her own grants: Alice's personal memory is hidden.
    act_as("carol")
    detail = (await http.get(f"/api/context-packs/{pack_id}")).json()
    assert detail["read_only"] and detail["agents"] == []
    assert [i["id"] for i in detail["render"]["items"]] == [str(shared.id)] and detail["render"]["hidden"] == 1
    assert (await http.patch(f"/api/context-packs/{pack_id}", json={"name": "y"})).status_code == 403
    act_as("dave")
    assert (await http.get(f"/api/context-packs/{pack_id}")).status_code == 404
    act_as("alice")
    assert (await http.delete(f"/api/context-packs/{pack_id}")).status_code == 204


async def test_knowledge_pages_and_shared(env):
    http, act_as, seed = env["http"], env["act_as"], env["seed"]
    for _ in range(2):
        await seed("Alice prefers metric units", scope="user", owner="alice", actor="alice", weight=0.9)
    await seed("Carol's private note", scope="user", owner="carol", actor="carol")
    published = await seed("Atlas safety rule: stop at 12 Nm", owner="carol", actor="carol")
    async with env["sessions"]() as db:
        row = await db.get(MemoryNode, published.id)
        row.metadata_ = {"shared_knowledge": {"request_id": "r-1", "submitted_by": "carol"}}
        await db.commit()
    act_as("alice")
    page = (await http.get("/api/knowledge/me/me")).json()
    assert page["source_count"] == 2 and page["summary"]["facts"] == 1 and page["summary"]["folded"] == 1
    assert "Carol" not in str(page)
    project = (await http.get("/api/knowledge/project/atlas-kn")).json()
    assert project["source_count"] == 1 and project["questions"] == []
    index = (await http.get("/api/knowledge")).json()["items"]
    assert [(i["kind"], i["id"]) for i in index] == [("me", "me"), ("project", "atlas-kn")]
    items = (await http.get("/api/knowledge/shared")).json()["items"]
    assert [i["memory_id"] for i in items] == [str(published.id)] and items[0]["source"] == "Request r-1"
    act_as("dave")
    assert (await http.get("/api/knowledge/project/atlas-kn")).status_code == 404
    assert (await http.get("/api/knowledge/shared")).json()["items"] == []
