"""Entities (#717) against a real PostgreSQL + pgvector database.

Covers editing an entity's type and aliases (with the dedup-hash collision a
new type can cause, and the audit record), and the graph filtered to the
memories that mention an entity.

Uses the repository's integration database (tests/integration/compose.yaml,
localhost:15433) and skips when it is not reachable:

    podman compose -f tests/integration/compose.yaml up -d
    cd memoryhub-ui/backend && pytest tests/test_entities_pg.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from memoryhub_core.models.audit import AuditLog
from memoryhub_core.models.memory import MemoryNode
from memoryhub_core.models.project import Project, ProjectMembership
from memoryhub_core.models.schemas import MemoryNodeCreate
from memoryhub_core.services.embeddings import MockEmbeddingService
from memoryhub_core.services.entity import (
    compute_entity_hash,
    create_mentions_relationship,
    find_or_create_entity,
)
from memoryhub_core.services.memory import create_memory
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.authorization import Access, Principal, get_access
from src.config import Settings, get_settings
from src.database import get_db
from src.human_adapters import get_embedding_service
from src.main import app

TENANT = "tenant-entities"
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
        for table in ("audit_log", "contradiction_reports", "memory_relationships", "memory_nodes"):
            await conn.execute(text(f"delete from {table} where tenant_id = :t"), {"t": TENANT})
        await conn.execute(text("delete from project_memberships where project_id = 'atlas-e'"))
        await conn.execute(text("delete from projects where name = 'atlas-e'"))
    sessions = async_sessionmaker(database, expire_on_commit=False)
    async with sessions() as db:
        db.add(Project(name="atlas-e", tenant_id=TENANT, created_by="bob"))
        await db.flush()
        for user, role in (("alice", "member"), ("bob", "admin"), ("carol", "member")):
            db.add(ProjectMembership(project_id="atlas-e", user_id=user, role=role, joined_by="bob"))
        await db.commit()
    embedding = MockEmbeddingService()
    who = {"access": None}

    async def db_dep():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = db_dep
    app.dependency_overrides[get_access] = lambda: who["access"]
    app.dependency_overrides[get_settings] = lambda: Settings(ui_tenant_id=TENANT, embedding_url="mock")
    app.dependency_overrides[get_embedding_service] = lambda: embedding

    def act_as(name: str, role: str = "user") -> None:
        grants = {
            "alice": {"human_projects": {"atlas-e"}, "agents": {"alice-agent"}},
            "bob": {"human_projects": {"atlas-e"}, "human_project_admins": {"atlas-e"}},
            "carol": {"human_projects": {"atlas-e"}},
            "admin": {"human_projects": {"atlas-e"}, "human_project_admins": {"atlas-e"},
                      "moderation_scopes": {"project", "organizational"}},
        }[name]
        who["access"] = Access(principal=Principal(name, role, TENANT), **grants)

    async def seed(content: str, *, scope: str = "project", owner: str = "alice", actor: str = "alice-agent",
                   weight: float = 0.5, parent: uuid.UUID | None = None, branch: str | None = None) -> MemoryNode:
        async with sessions() as db:
            created, _ = await create_memory(MemoryNodeCreate(
                content=content, scope=scope, owner_id=owner, actor_id=actor, weight=weight,
                scope_id="atlas-e" if scope == "project" else None, parent_id=parent, branch_type=branch,
            ), db, embedding, tenant_id=TENANT, skip_curation=True)
            return created

    async def entity(name: str, kind: str = "person", owner: str = "alice", aliases=None):
        async with sessions() as db:
            node, _ = await find_or_create_entity(name, kind, db, embedding, tenant_id=TENANT, owner_id=owner,
                                                  aliases=aliases)
            await db.commit()
            return node

    async def mention(memory_id, entity_id):
        async with sessions() as db:
            await create_mentions_relationship(memory_id, entity_id, db, tenant_id=TENANT, created_by="alice")
            await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield {"http": http, "seed": seed, "act_as": act_as, "sessions": sessions, "entity": entity,
               "mention": mention}
    app.dependency_overrides.clear()


async def test_type_and_aliases_are_editable_and_audited(env):
    http, seed, act_as, sessions = env["http"], env["seed"], env["act_as"], env["sessions"]
    gripper = await env["entity"]("Atlas gripper", aliases=["AG"])
    note = await seed("The Atlas gripper torque is 11.5 Nm", scope="user", owner="alice", actor="alice")
    await env["mention"](note.id, gripper.id)

    act_as("alice")
    response = await http.patch(f"/api/entities/{gripper.id}", json={
        "entity_type": "object", "aliases": [" Atlas  end effector ", "atlas end effector", "Atlas gripper", "AG"]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["entity_type"] == "object" and body["aliases"] == ["AG", "Atlas end effector"]
    assert body["changes"] == {"entity_type": {"before": "person", "after": "object"},
                               "aliases": {"before": ["AG"], "after": ["AG", "Atlas end effector"]}}
    detail = (await http.get(f"/api/entities/{gripper.id}")).json()
    assert detail["entity_type"] == "object" and detail["can_edit"] is True
    assert detail["aliases"] == ["AG", "Atlas end effector"]
    async with sessions() as db:
        audit = (await db.execute(select(AuditLog).where(
            AuditLog.tenant_id == TENANT, AuditLog.event_type == "entity.updated"))).scalars().all()
    assert len(audit) == 1 and audit[0].memory_id == gripper.id and audit[0].actor_id == "alice"
    assert audit[0].event_metadata["changes"]["entity_type"] == {"before": "person", "after": "object"}

    # Rename and retype together; the old name becomes an alias.
    both = await http.patch(f"/api/entities/{gripper.id}", json={"name": "Atlas end effector", "entity_type": "event"})
    assert both.status_code == 200, both.text
    assert both.json()["name"] == "Atlas end effector" and both.json()["entity_type"] == "event"
    assert "Atlas gripper" in both.json()["aliases"]

    # The new name was an alias; the next edit of the aliases drops it, then the same list changes nothing.
    tidy = await http.patch(f"/api/entities/{gripper.id}", json={"aliases": both.json()["aliases"]})
    assert tidy.status_code == 200 and "Atlas end effector" not in tidy.json()["aliases"]
    same = await http.patch(f"/api/entities/{gripper.id}", json={"aliases": tidy.json()["aliases"]})
    assert same.status_code == 422
    assert (await http.patch(f"/api/entities/{gripper.id}", json={"entity_type": "robot"})).status_code == 422


async def test_a_new_type_that_collides_asks_for_a_merge_and_changes_nothing(env):
    http, act_as, sessions = env["http"], env["act_as"], env["sessions"]
    person = await env["entity"]("Mercury")
    # The service would fold a second "Mercury" into the first; make the other one directly.
    place = await env["entity"]("Mercury the planet", "location")
    async with sessions() as db:
        row = await db.get(MemoryNode, place.id)
        row.content = "Mercury"
        row.content_hash = compute_entity_hash(TENANT, "alice", "Mercury", "location")
        await db.commit()
    act_as("alice")
    clash = await http.patch(f"/api/entities/{person.id}", json={"entity_type": "location", "aliases": ["Hg"]})
    assert clash.status_code == 409 and "merge" in clash.json()["detail"]
    async with sessions() as db:
        row = await db.get(MemoryNode, person.id)
        assert row.branch_type == "entity:person" and (row.metadata_ or {}).get("aliases", []) == []
        assert (await db.execute(select(AuditLog).where(
            AuditLog.tenant_id == TENANT, AuditLog.event_type == "entity.updated"))).scalars().all() == []


async def test_only_the_owner_whose_memories_are_writable_edits(env):
    http, seed, act_as = env["http"], env["seed"], env["act_as"]
    alices = await env["entity"]("Path planner")
    project_note = await seed("Path planner budget is 40 ms", owner="carol", actor="carol")
    await env["mention"](project_note.id, alices.id)
    act_as("carol")  # not the entity's owner
    assert (await http.patch(f"/api/entities/{alices.id}", json={"entity_type": "object"})).status_code == 404
    act_as("admin", "admin")
    assert (await http.patch(f"/api/entities/{alices.id}", json={"entity_type": "object"})).status_code == 403
    act_as("alice")  # owner, and a member of the project the mention lives in
    assert (await http.patch(f"/api/entities/{alices.id}", json={"entity_type": "object"})).status_code == 200


async def test_graph_filtered_by_entity_shows_only_readable_memories_that_mention_it(env):
    http, seed, act_as = env["http"], env["seed"], env["act_as"]
    atlas = await env["entity"]("Atlas", "object")
    mentions = await seed("Atlas ships in March", owner="carol", actor="carol")
    own = await seed("Alice's Atlas checklist", scope="user", owner="alice", actor="alice")
    private = await seed("Carol's Atlas worries", scope="user", owner="carol", actor="carol")
    unrelated = await seed("Path planner budget is 40 ms", owner="carol", actor="carol")
    for memory in (mentions, own, private):
        await env["mention"](memory.id, atlas.id)

    act_as("alice")
    graph = (await http.get("/api/graph", params={"entity": str(atlas.id)})).json()
    assert {n["id"] for n in graph["nodes"]} == {str(mentions.id), str(own.id)}
    everything = (await http.get("/api/graph")).json()
    assert str(unrelated.id) in {n["id"] for n in everything["nodes"]}
    assert (await http.get("/api/graph", params={"entity": str(uuid.uuid4())})).json()["nodes"] == []
    listed = (await http.get("/api/memories/browse", params={"entity": str(atlas.id), "tab": "all"})).json()
    assert {i["id"] for i in listed["items"]} == {str(mentions.id), str(own.id)}
