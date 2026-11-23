"""Projects & teams (#720) against a real PostgreSQL + pgvector database.

Covers teams (new tables, migration 034), campaign enrollment by project
admins, project roles, self-join of open projects and per-person project figures.

Uses the repository's integration database (tests/integration/compose.yaml,
localhost:15433) and skips when it is not reachable:

    podman compose -f tests/integration/compose.yaml up -d
    cd memoryhub-ui/backend && pytest tests/test_projects_teams_pg.py
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
from memoryhub_core.models.campaign import Campaign, CampaignMembership
from memoryhub_core.models.contradiction import ContradictionReport
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

TENANT = "tenant-teams"
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
        await conn.execute(text("delete from memoryhub_ui_teams where tenant_id = :t"), {"t": TENANT})
        await conn.execute(text(
            "delete from campaign_memberships where project_id in ('atlas-t', 'open-t', 'closed-t')"))
        await conn.execute(text("delete from campaigns where tenant_id = :t"), {"t": TENANT})
        await conn.execute(text("delete from project_memberships where project_id in ('open-t', 'closed-t')"))
        await conn.execute(text("delete from projects where name in ('open-t', 'closed-t')"))
        for table in ("audit_log", "contradiction_reports", "memory_relationships", "memory_nodes"):
            await conn.execute(text(f"delete from {table} where tenant_id = :t"), {"t": TENANT})
        await conn.execute(text("delete from project_memberships where project_id = 'atlas-t'"))
        await conn.execute(text("delete from projects where name = 'atlas-t'"))
    sessions = async_sessionmaker(database, expire_on_commit=False)
    async with sessions() as db:
        db.add(Project(name="atlas-t", tenant_id=TENANT, created_by="bob"))
        db.add(Project(name="open-t", tenant_id=TENANT, created_by="bob", invite_only=False))
        db.add(Project(name="closed-t", tenant_id=TENANT, created_by="bob", invite_only=True))
        await db.flush()
        for user, role in (("alice", "member"), ("bob", "admin"), ("carol", "member")):
            db.add(ProjectMembership(project_id="atlas-t", user_id=user, role=role, joined_by="bob"))
        await db.commit()
    embedding = MockEmbeddingService()
    who = {"access": None}

    async def db_dep():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = db_dep
    app.dependency_overrides[get_access] = lambda: who["access"]
    app.dependency_overrides[get_settings] = lambda: Settings(ui_tenant_id=TENANT, embedding_url="mock",
                                                              ui_allowed_users="alice,bob,carol,dave",
                                                              ui_admin_users="admin")
    app.dependency_overrides[get_embedding_service] = lambda: embedding

    def act_as(name: str, role: str = "user") -> None:
        grants = {
            "alice": {"human_projects": {"atlas-t"}, "agents": {"alice-agent"}},
            "bob": {"human_projects": {"atlas-t"}, "human_project_admins": {"atlas-t"}},
            "carol": {"human_projects": {"atlas-t"}},
            "dave": {},
            "admin": {"human_projects": {"atlas-t"}, "human_project_admins": {"atlas-t"},
                      "moderation_scopes": {"project", "organizational"}},
        }[name]
        who["access"] = Access(principal=Principal(name, role, TENANT), **grants)

    async def seed(content: str, *, scope: str = "project", owner: str = "alice", actor: str = "alice-agent",
                   weight: float = 0.5, parent: uuid.UUID | None = None, branch: str | None = None) -> MemoryNode:
        async with sessions() as db:
            created, _ = await create_memory(MemoryNodeCreate(
                content=content, scope=scope, owner_id=owner, actor_id=actor, weight=weight,
                scope_id="atlas-t" if scope == "project" else None, parent_id=parent, branch_type=branch,
            ), db, embedding, tenant_id=TENANT, skip_curation=True)
            return created

    async def new_version(memory_id: uuid.UUID, content: str):
        async with sessions() as db:
            return await update_memory(memory_id, MemoryNodeUpdate(content=content), db, embedding,
                                       actor_id="alice", driver_id="alice")

    async def audit(event_type: str, actor: str, memory_id=None, minutes_ago: float = 0, **metadata):
        async with sessions() as db:
            db.add(AuditLog(event_type=event_type, actor_id=actor, driver_id=actor, scope="project",
                            owner_id="alice", memory_id=memory_id, decision="allowed", tenant_id=TENANT,
                            event_metadata=metadata or None,
                            timestamp=datetime.now(UTC) - timedelta(minutes=minutes_ago)))
            await db.commit()

    async def fetch(memory_id) -> MemoryNode:
        async with sessions() as db:
            return (await db.execute(select(MemoryNode).where(MemoryNode.id == memory_id))).scalar_one()

    async def campaign(name: str, status: str = "active", projects=()):
        async with sessions() as db:
            c = Campaign(name=name, status=status, tenant_id=TENANT, default_ttl=timedelta(days=30))
            db.add(c)
            await db.flush()
            for p in projects:
                db.add(CampaignMembership(campaign_id=c.id, project_id=p, enrolled_by="bob"))
            await db.commit()
            return c.id

    act_as("alice")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield {"http": http, "act_as": act_as, "seed": seed, "new_version": new_version, "audit": audit,
               "fetch": fetch, "campaign": campaign}
async def test_teams_are_visible_to_members_and_managed_by_admin_or_lead(env):
    http, act_as = env["http"], env["act_as"]
    act_as("alice")
    assert (await http.post("/api/teams", json={"name": "Robotics"})).status_code == 403

    act_as("admin", "admin")
    created = await http.post("/api/teams", json={"name": "Robotics", "description": "Arm and gripper",
                                                  "members": [{"user_id": "alice", "role": "lead"},
                                                              {"user_id": "carol"}]})
    assert created.status_code == 201
    team = created.json()["id"]
    assert (await http.post("/api/teams", json={"name": "Robotics"})).status_code == 409
    bots = {"name": "Bots", "members": [{"user_id": "alice-agent"}]}
    assert (await http.post("/api/teams", json=bots)).status_code == 422

    act_as("alice")
    mine = (await http.get("/api/teams")).json()
    assert [t["name"] for t in mine["items"]] == ["Robotics"] and mine["counts"]["people"] == 2
    assert (await http.post(f"/api/teams/{team}/members", json={"user_id": "bob"})).status_code == 200
    assert (await http.post(f"/api/teams/{team}/members", json={"user_id": "dave", "role": "lead"})).status_code == 403
    assert (await http.put(f"/api/teams/{team}/projects/atlas-t")).status_code == 200
    assert (await http.put(f"/api/teams/{team}/projects/closed-t")).status_code == 403
    detail = (await http.get(f"/api/teams/{team}")).json()
    assert detail["projects"] == ["atlas-t"] and detail["can_manage"] and detail["my_role"] == "lead"
    assert (await http.delete(f"/api/teams/{team}/members/alice")).status_code == 409  # last lead

    act_as("carol")
    assert (await http.post(f"/api/teams/{team}/members", json={"user_id": "dave"})).status_code == 403
    assert (await http.delete(f"/api/teams/{team}/members/carol")).status_code == 204
    assert (await http.get(f"/api/teams/{team}")).status_code == 404
    assert (await http.get("/api/teams")).json()["items"] == []

    act_as("dave")
    assert (await http.get(f"/api/teams/{team}")).status_code == 404


async def test_campaigns_show_mine_and_project_admins_enroll(env):
    http, act_as, campaign = env["http"], env["act_as"], env["campaign"]
    winter = await campaign("Winter release", projects=("atlas-t",))
    spring = await campaign("Spring cleanup")
    await campaign("Old pilot", status="archived")

    act_as("carol")  # member of atlas-t, admin of nothing
    rows = (await http.get("/api/campaigns")).json()["items"]
    assert [c["name"] for c in rows] == ["Winter release"]
    assert rows[0]["my_projects"] == ["atlas-t"] and rows[0]["can_leave"] == []
    assert (await http.post(f"/api/campaigns/{spring}/projects/atlas-t")).status_code == 403

    act_as("bob")  # admin of atlas-t
    rows = {c["name"]: c for c in (await http.get("/api/campaigns")).json()["items"]}
    assert set(rows) == {"Winter release", "Spring cleanup"}
    assert rows["Spring cleanup"]["can_enroll"] == ["atlas-t"] and rows["Winter release"]["can_leave"] == ["atlas-t"]
    assert rows["Winter release"]["default_ttl_days"] == 30
    assert (await http.post(f"/api/campaigns/{spring}/projects/atlas-t")).status_code == 200
    assert (await http.post(f"/api/campaigns/{spring}/projects/atlas-t")).status_code == 409
    assert (await http.delete(f"/api/campaigns/{winter}/projects/atlas-t")).status_code == 204
    act_as("carol")
    assert [c["name"] for c in (await http.get("/api/campaigns")).json()["items"]] == ["Spring cleanup"]


async def test_project_roles_join_and_own_figures(env):
    http, act_as, seed = env["http"], env["act_as"], env["seed"]
    roles = (await http.get("/api/project-roles")).json()["items"]
    assert [r["role"] for r in roles] == ["owner", "manager", "contributor"]

    mine = await seed("Gripper limit 11.5 Nm")                       # alice-agent wrote it
    await seed("Carol's note", owner="carol", actor="carol")
    contradicted = await seed("Wrist limit 3 Nm", owner="carol", actor="carol")
    sessions = app.dependency_overrides[get_db]
    async for db in sessions():
        db.add(ContradictionReport(memory_id=contradicted.id, observed_behavior="now 4 Nm", confidence=0.8,
                                   reporter="curator", tenant_id=TENANT))
        await db.commit()

    act_as("alice")
    detail = (await http.get("/api/projects/atlas-t")).json()
    assert detail["my_project_role"] == "contributor"
    assert detail["written_by_me"] == 1 and detail["written_by_my_agents"] == 1
    assert detail["open_contradictions"] == 1 and detail["quota"] is None
    assert detail["managers"] == ["bob"]
    roles_by_user = {m["user_id"]: m["project_role"] for m in detail["members_list"]}
    assert roles_by_user == {"alice": "contributor", "bob": "owner", "carol": "contributor"}
    assert mine.id

    act_as("bob")
    assert (await http.get("/api/projects/atlas-t")).json()["my_project_role"] == "owner"

    act_as("dave")
    assert (await http.get("/api/projects/open-t")).json()["can_join"] is True
    assert (await http.post("/api/projects/closed-t/join")).status_code in (403, 404)
    joined = await http.post("/api/projects/open-t/join")
    assert joined.status_code == 200 and joined.json()["role"] == "contributor"
    assert (await http.post("/api/projects/open-t/join")).status_code == 409
    act_as("dave")
    mine_now = (await http.get("/api/projects", params={"mine": True})).json()["items"]
    assert any(p["name"] == "open-t" for p in mine_now)
