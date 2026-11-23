"""My requests (#719) against a real PostgreSQL + pgvector database.

Covers the request lifecycle end to end: who may submit, the one-open-request
rule, withdrawal, the reviewer's queue and snapshot read (audited for the
requester), and approval publishing exactly the submitted version.

Uses the repository's integration database (tests/integration/compose.yaml,
localhost:15433) and skips when it is not reachable:

    podman compose -f tests/integration/compose.yaml up -d
    cd memoryhub-ui/backend && pytest tests/test_my_requests_pg.py
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
from memoryhub_core.models.schemas import MemoryNodeCreate, MemoryNodeUpdate
from memoryhub_core.services.embeddings import MockEmbeddingService
from memoryhub_core.services.memory import create_memory, update_memory
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.authorization import Access, Principal, get_access
from src.config import Settings, get_settings
from src.database import get_db
from src.human_adapters import get_embedding_service
from src.main import app

TENANT = "tenant-requests"
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
        await conn.execute(text("delete from memoryhub_ui_requests where tenant_id = :t"), {"t": TENANT})
        for table in ("audit_log", "contradiction_reports", "memory_relationships", "memory_nodes"):
            await conn.execute(text(f"delete from {table} where tenant_id = :t"), {"t": TENANT})
        await conn.execute(text("delete from project_memberships where project_id = 'atlas-r'"))
        await conn.execute(text("delete from projects where name = 'atlas-r'"))
    sessions = async_sessionmaker(database, expire_on_commit=False)
    async with sessions() as db:
        db.add(Project(name="atlas-r", tenant_id=TENANT, created_by="bob"))
        await db.flush()
        for user, role in (("alice", "member"), ("bob", "admin"), ("carol", "member")):
            db.add(ProjectMembership(project_id="atlas-r", user_id=user, role=role, joined_by="bob"))
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
            "alice": {"human_projects": {"atlas-r"}, "agents": {"alice-agent"}},
            "bob": {"human_projects": {"atlas-r"}, "human_project_admins": {"atlas-r"}},
            "carol": {"human_projects": {"atlas-r"}},
            "admin": {"human_projects": {"atlas-r"}, "human_project_admins": {"atlas-r"},
                      "moderation_scopes": {"project", "organizational"}},
        }[name]
        who["access"] = Access(principal=Principal(name, role, TENANT), **grants)

    async def seed(content: str, *, scope: str = "project", owner: str = "alice", actor: str = "alice-agent",
                   weight: float = 0.5, parent: uuid.UUID | None = None, branch: str | None = None) -> MemoryNode:
        async with sessions() as db:
            created, _ = await create_memory(MemoryNodeCreate(
                content=content, scope=scope, owner_id=owner, actor_id=actor, weight=weight,
                scope_id="atlas-r" if scope == "project" else None, parent_id=parent, branch_type=branch,
            ), db, embedding, tenant_id=TENANT, skip_curation=True)
            return created

    async def edit(memory_id: uuid.UUID, content: str):
        async with sessions() as db:
            updated = await update_memory(memory_id, MemoryNodeUpdate(content=content), db, embedding,
                                          actor_id="alice")
            await db.commit()
            return updated

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield {"http": http, "seed": seed, "act_as": act_as, "edit": edit, "sessions": sessions}
    app.dependency_overrides.clear()


async def test_request_lifecycle_promotion_to_organizational(env):
    http, seed, act_as, sessions = env["http"], env["seed"], env["act_as"], env["sessions"]
    mine = await seed("Use metric units in every Atlas report", scope="user", owner="alice", actor="alice")

    act_as("alice")
    created = await http.post("/api/me/requests", json={
        "kind": "promotion", "memory_id": str(mine.id), "target_scope": "organizational", "reason": "Team-wide rule"})
    assert created.status_code == 201, created.text
    req = created.json()
    assert req["state"] == "open" and req["waiting_for"] == "review"
    assert req["title"] == "Use metric units in every Atlas report"
    assert (req["from_scope"], req["to_scope"]) == ("user", "organizational")

    again = await http.post("/api/me/requests", json={
        "kind": "promotion", "memory_id": str(mine.id), "target_scope": "organizational"})
    assert again.status_code == 409

    # Another user: no queue, no detail, cannot withdraw it.
    act_as("carol")
    assert (await http.get("/api/review/requests")).json()["items"] == []
    assert (await http.get(f"/api/review/requests/{req['id']}")).status_code == 404
    assert (await http.post(f"/api/me/requests/{req['id']}/withdraw")).status_code == 404

    # The admin sees the queue row without content, then reads the snapshot (audited for alice).
    act_as("admin", "admin")
    queue = (await http.get("/api/review/requests")).json()["items"]
    assert [r["id"] for r in queue] == [req["id"]]
    assert "Use metric units" not in str(queue)
    detail = (await http.get(f"/api/review/requests/{req['id']}")).json()
    assert detail["content"] == "Use metric units in every Atlas report" and detail["requester"] == "alice"
    async with sessions() as db:
        reads = (await db.execute(select(AuditLog).where(
            AuditLog.tenant_id == TENANT, AuditLog.event_type == "request.read"))).scalars().all()
    assert len(reads) == 1 and reads[0].owner_id == "alice" and reads[0].actor_id == "admin"

    approved = await http.post(f"/api/review/requests/{req['id']}/decision", json={"outcome": "approved"})
    assert approved.status_code == 200, approved.text
    result_id = approved.json()["result_memory_id"]
    async with sessions() as db:
        published = await db.get(MemoryNode, uuid.UUID(result_id))
        assert published.scope == "organizational" and published.owner_id == "alice"
        assert published.content == "Use metric units in every Atlas report"
        assert published.metadata_["request"]["id"] == req["id"]
        source = await db.get(MemoryNode, mine.id)
        assert source.scope == "user"  # the personal memory stays where it was

    act_as("alice")
    listing = (await http.get("/api/me/requests")).json()
    assert listing["counts"] == {"open": 0, "decided": 1, "withdrawn": 0}
    row = listing["items"][0]
    assert row["outcome"] == "approved" and row["decided_by"] == "admin" and row["result_memory_id"] == result_id

    act_as("admin", "admin")
    assert (await http.get("/api/review/requests")).json()["items"] == []
    assert (await http.post(f"/api/review/requests/{req['id']}/decision",
                            json={"outcome": "approved"})).status_code == 404


async def test_only_writable_memories_and_broader_targets(env):
    http, seed, act_as = env["http"], env["seed"], env["act_as"]
    alices = await seed("Alice personal note", scope="user", owner="alice", actor="alice")
    project = await seed("Atlas gripper limit is 11.5 Nm", owner="carol", actor="carol")
    org = await seed("Company holiday calendar", scope="organizational", owner="admin", actor="admin")

    act_as("carol")
    # Someone else's personal memory: as if it did not exist.
    assert (await http.post("/api/me/requests", json={
        "kind": "promotion", "memory_id": str(alices.id), "target_scope": "organizational"})).status_code == 404
    # Organizational memories are not writable by a user.
    assert (await http.post("/api/me/requests", json={
        "kind": "promotion", "memory_id": str(org.id), "target_scope": "organizational"})).status_code == 404
    # A project memory cannot go "down" or sideways to a project.
    assert (await http.post("/api/me/requests", json={
        "kind": "promotion", "memory_id": str(project.id), "target_scope": "project",
        "project": "atlas-r"})).status_code == 422
    ok = await http.post("/api/me/requests", json={
        "kind": "shared_knowledge", "memory_id": str(project.id), "target_scope": "organizational"})
    assert ok.status_code == 201

    act_as("alice")
    # Promote into a project only if she belongs to it.
    assert (await http.post("/api/me/requests", json={
        "kind": "promotion", "memory_id": str(alices.id), "target_scope": "project",
        "project": "elsewhere"})).status_code == 403
    assert (await http.post("/api/me/requests", json={
        "kind": "promotion", "memory_id": str(alices.id), "target_scope": "project"})).status_code == 422

    act_as("admin", "admin")
    assert (await http.post("/api/me/requests", json={
        "kind": "promotion", "memory_id": str(project.id), "target_scope": "organizational"})).status_code == 403


async def test_withdrawn_requests_leave_the_queue_and_project_admins_decide_project_targets(env):
    http, seed, act_as, sessions = env["http"], env["seed"], env["act_as"], env["sessions"]
    first = await seed("Alice sketch for the Atlas mount", scope="user", owner="alice", actor="alice")
    second = await seed("Alice checklist for Atlas releases", scope="user", owner="alice", actor="alice")

    act_as("alice")
    a = (await http.post("/api/me/requests", json={
        "kind": "promotion", "memory_id": str(first.id), "target_scope": "project", "project": "atlas-r"})).json()
    b = (await http.post("/api/me/requests", json={
        "kind": "promotion", "memory_id": str(second.id), "target_scope": "project", "project": "atlas-r"})).json()
    assert a["waiting_for"] == "project_owner"

    act_as("bob")  # project admin of atlas-r, role user
    assert {r["id"] for r in (await http.get("/api/review/requests")).json()["items"]} == {a["id"], b["id"]}
    act_as("alice")
    withdrawn = await http.post(f"/api/me/requests/{a['id']}/withdraw")
    assert withdrawn.status_code == 200 and withdrawn.json()["state"] == "withdrawn"
    assert (await http.post(f"/api/me/requests/{a['id']}/withdraw")).status_code == 409

    act_as("bob")
    assert [r["id"] for r in (await http.get("/api/review/requests")).json()["items"]] == [b["id"]]
    assert (await http.get(f"/api/review/requests/{a['id']}")).status_code == 404
    rejected = await http.post(f"/api/review/requests/{b['id']}/decision", json={"outcome": "rejected"})
    assert rejected.status_code == 422  # a rejection needs a reason
    rejected = await http.post(f"/api/review/requests/{b['id']}/decision",
                               json={"outcome": "rejected", "reason": "Duplicates the release runbook"})
    assert rejected.status_code == 200 and rejected.json()["result_memory_id"] is None

    act_as("carol")  # member, not admin: no project queue
    assert (await http.get("/api/review/requests")).json()["items"] == []

    act_as("alice")
    listing = (await http.get("/api/me/requests")).json()
    by_id = {r["id"]: r for r in listing["items"]}
    assert by_id[b["id"]]["reason"] == "Duplicates the release runbook"
    assert by_id[b["id"]]["decided_by_role"] == "project_admin"
    assert listing["counts"] == {"open": 0, "decided": 1, "withdrawn": 1}
    async with sessions() as db:
        events = (await db.execute(select(AuditLog.event_type).where(
            AuditLog.tenant_id == TENANT, AuditLog.owner_id == "alice").order_by(AuditLog.id))).scalars().all()
    assert events.count("request.submitted") == 2
    assert "request.withdrawn" in events and "request.decided" in events


async def test_approval_refuses_a_memory_changed_after_submission(env):
    http, seed, act_as, edit = env["http"], env["seed"], env["act_as"], env["edit"]
    mine = await seed("Atlas torque table v1", scope="user", owner="alice", actor="alice")
    act_as("alice")
    req = (await http.post("/api/me/requests", json={
        "kind": "promotion", "memory_id": str(mine.id), "target_scope": "organizational"})).json()
    await edit(mine.id, "Atlas torque table v2")

    act_as("admin", "admin")
    refused = await http.post(f"/api/review/requests/{req['id']}/decision", json={"outcome": "approved"})
    assert refused.status_code == 409
    detail = (await http.get(f"/api/review/requests/{req['id']}")).json()
    assert detail["content"] == "Atlas torque table v1"  # still the submitted snapshot


async def test_nobody_decides_their_own_request(env):
    http, seed, act_as = env["http"], env["seed"], env["act_as"]
    bobs = await seed("Bob personal idea for Atlas", scope="user", owner="bob", actor="bob")
    act_as("bob")
    req = (await http.post("/api/me/requests", json={
        "kind": "promotion", "memory_id": str(bobs.id), "target_scope": "project", "project": "atlas-r"})).json()
    assert (await http.get("/api/review/requests")).json()["items"] == []
    assert (await http.post(f"/api/review/requests/{req['id']}/decision",
                            json={"outcome": "approved"})).status_code == 404
    assert (await http.get("/api/me/requests")).json()["to_decide"] == 0
