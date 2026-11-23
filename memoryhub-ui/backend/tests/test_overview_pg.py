"""User Overview (#711) against a real PostgreSQL + pgvector database.

Covers activity per period limited to the person and their agents, agent usage
from memory.served events, categories of readable memories, and the
getting-started checklist with its per-person dismissal.

Uses the repository's integration database (tests/integration/compose.yaml,
localhost:15433) and skips when it is not reachable:

    podman compose -f tests/integration/compose.yaml up -d
    cd memoryhub-ui/backend && pytest tests/test_overview_pg.py
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
from memoryhub_core.models.memory import MemoryNode
from memoryhub_core.models.project import Project, ProjectMembership
from memoryhub_core.models.schemas import MemoryNodeCreate
from memoryhub_core.services.embeddings import MockEmbeddingService
from memoryhub_core.services.memory import create_memory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.authorization import Access, Principal, get_access
from src.config import Settings, get_settings
from src.database import get_db
from src.human_adapters import get_embedding_service
from src.main import app

TENANT = "tenant-overview"
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
        await conn.execute(text("delete from memoryhub_ui_onboarding where tenant_id = :t"), {"t": TENANT})
        await conn.execute(text("delete from curator_rules where tenant_id = :t"), {"t": TENANT})
        for table in ("audit_log", "contradiction_reports", "memory_relationships", "memory_nodes"):
            await conn.execute(text(f"delete from {table} where tenant_id = :t"), {"t": TENANT})
        await conn.execute(text("delete from project_memberships where project_id = 'atlas-o'"))
        await conn.execute(text("delete from projects where name = 'atlas-o'"))
    sessions = async_sessionmaker(database, expire_on_commit=False)
    async with sessions() as db:
        db.add(Project(name="atlas-o", tenant_id=TENANT, created_by="bob"))
        await db.flush()
        for user, role in (("alice", "member"), ("bob", "admin"), ("carol", "member")):
            db.add(ProjectMembership(project_id="atlas-o", user_id=user, role=role, joined_by="bob"))
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
            "alice": {"human_projects": {"atlas-o"}, "agents": {"alice-agent"}},
            "bob": {"human_projects": {"atlas-o"}, "human_project_admins": {"atlas-o"}},
            "carol": {"human_projects": {"atlas-o"}, "agents": {"carol-agent"}},
            "dave": {},
            "admin": {"human_projects": {"atlas-o"}, "human_project_admins": {"atlas-o"},
                      "moderation_scopes": {"project", "organizational"}},
        }[name]
        who["access"] = Access(principal=Principal(name, role, TENANT), **grants)

    async def seed(content: str, *, scope: str = "project", owner: str = "alice", actor: str = "alice-agent",
                   weight: float = 0.5, parent: uuid.UUID | None = None, branch: str | None = None) -> MemoryNode:
        async with sessions() as db:
            created, _ = await create_memory(MemoryNodeCreate(
                content=content, scope=scope, owner_id=owner, actor_id=actor, weight=weight,
                scope_id="atlas-o" if scope == "project" else None, parent_id=parent, branch_type=branch,
            ), db, embedding, tenant_id=TENANT, skip_curation=True)
            return created

    def event(event_type: str, actor: str, *, minutes_ago: float = 5, memory_id=None, driver=None,
              decision: str = "allowed", metadata: dict | None = None) -> AuditLog:
        return AuditLog(event_type=event_type, actor_id=actor, driver_id=driver or actor, scope="user",
                        owner_id=actor, memory_id=memory_id, decision=decision, tenant_id=TENANT,
                        event_metadata=metadata, timestamp=datetime.now(UTC) - timedelta(minutes=minutes_ago))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield {"http": http, "seed": seed, "act_as": act_as, "sessions": sessions, "event": event}
    app.dependency_overrides.clear()


async def test_activity_counts_only_my_events_per_period(env):
    http, act_as, sessions, event = env["http"], env["act_as"], env["sessions"], env["event"]
    async with sessions() as db:
        db.add_all([
            event("memory.write", "alice", minutes_ago=30),
            event("memory.write", "alice-agent", minutes_ago=90),
            event("memory.update", "alice-agent", minutes_ago=60 * 30),  # 30 h ago: in 7d, not in 1d
            event("memory.search", "alice-agent", minutes_ago=10),
            event("memory.search", "svc-bot", driver="alice", minutes_ago=15),  # driven by alice
            event("memory.write", "alice-agent", decision="denied", minutes_ago=20),  # refused: not a write
            event("memory.write", "carol-agent", minutes_ago=5),  # someone else
            event("memory.search", "carol", minutes_ago=5),
            event("memory.write", "alice", minutes_ago=60 * 24 * 40),  # older than 30 days
        ])
        await db.commit()

    act_as("alice")
    day = (await http.get("/api/overview", params={"range": "1d"})).json()
    assert day["totals"] == {"writes": 2, "searches": 2}
    assert day["activity_bucket_seconds"] == 3600 and 24 <= len(day["activity"]) <= 25
    week = (await http.get("/api/overview", params={"range": "7d"})).json()
    assert week["totals"] == {"writes": 3, "searches": 2}
    month = (await http.get("/api/overview", params={"range": "30d"})).json()
    assert month["totals"] == {"writes": 3, "searches": 2}

    act_as("carol")
    assert (await http.get("/api/overview", params={"range": "1d"})).json()["totals"] == {"writes": 1, "searches": 1}
    act_as("admin", "admin")
    assert (await http.get("/api/overview")).status_code == 403
    assert (await http.get("/api/usage/summary")).status_code == 403


async def test_categories_cover_readable_memories_and_requests_hide_when_not_installed(env):
    http, seed, act_as = env["http"], env["seed"], env["act_as"]
    async with env["sessions"]() as db:
        for content, scope, owner, domains in (
            ("Alice robotics note", "user", "alice", ["robotics", "safety"]),
            ("Atlas safety limits", "project", "carol", ["safety"]),
            ("Carol private note", "user", "carol", ["salary"]),
        ):
            node = await seed(content, scope=scope, owner=owner, actor=owner)
            row = await db.get(MemoryNode, node.id)
            row.domains = domains
        await db.commit()

    act_as("alice")
    body = (await http.get("/api/overview")).json()
    assert body["categories"] == [{"name": "safety", "memory_count": 2}, {"name": "robotics", "memory_count": 1}]
    installed = await _requests_installed(env)
    assert (body["requests"] is None) == (not installed)


async def _requests_installed(env) -> bool:
    async with env["sessions"]() as db:
        return (await db.execute(text("select to_regclass('memoryhub_ui_requests') is not null"))).scalar_one()


async def test_agent_usage_measures_only_my_agents_and_hides_unreadable_memories(env):
    http, seed, act_as, sessions, event = env["http"], env["seed"], env["act_as"], env["sessions"], env["event"]
    readable = await seed("Atlas gripper limit", owner="carol", actor="carol")
    acted = await seed("Atlas path planner budget", owner="carol", actor="carol")
    secret = await seed("Carol private plan", scope="user", owner="carol", actor="carol")
    full = await seed("Atlas safety stop", owner="carol", actor="carol", weight=0.9)
    async with sessions() as db:
        db.add_all([
            event("memory.served", "alice-agent", minutes_ago=60, metadata={
                "result_ids": [str(readable.id), str(acted.id), str(secret.id), str(full.id)],
                "full_ids": [str(full.id)]}),
            event("memory.read", "alice-agent", memory_id=acted.id, minutes_ago=50),  # 10 min later: used
            event("memory.read", "alice-agent", memory_id=readable.id, minutes_ago=5),  # 55 min later: too late
            event("memory.served", "carol-agent", minutes_ago=30, metadata={"result_ids": [str(readable.id)]}),
        ])
        await db.commit()

    act_as("alice")
    usage = (await http.get("/api/usage/summary", params={"days": 30})).json()
    assert usage["agents"] == ["alice-agent"]
    assert (usage["served"], usage["full"], usage["used"], usage["skipped"]) == (4, 1, 1, 2)
    assert usage["use_rate"] == 1 / 3
    # carol's personal memory was skipped too, but alice may not read it: not listed.
    assert [m["id"] for m in usage["skipped_most"]] == [str(readable.id)]
    assert "Carol private plan" not in str(usage)

    act_as("dave")  # no agents
    assert (await http.get("/api/usage/summary")).json()["served"] == 0


async def test_onboarding_steps_follow_my_records_and_dismissal_is_mine(env):
    http, seed, act_as, sessions, event = env["http"], env["seed"], env["act_as"], env["sessions"], env["event"]
    act_as("dave")
    fresh = (await http.get("/api/me/onboarding")).json()
    assert fresh["done"] == 0 and fresh["total"] == 5 and fresh["dismissed"] is False

    await seed("Alice first memory", scope="user", owner="alice-agent", actor="alice-agent")
    async with sessions() as db:
        db.add(event("memory.search", "alice-agent"))
        await db.commit()
    act_as("alice")
    steps = {s["id"]: s["done"] for s in (await http.get("/api/me/onboarding")).json()["steps"]}
    assert steps == {"connect_agent": True, "first_memory": True, "agent_search": True,
                     "join_project": True, "curation_rule": False}

    hidden = (await http.post("/api/me/onboarding/dismiss")).json()
    assert hidden["dismissed"] is True
    assert (await http.get("/api/me/onboarding")).json()["dismissed"] is True
    act_as("dave")
    assert (await http.get("/api/me/onboarding")).json()["dismissed"] is False
    act_as("alice")
    assert (await http.post("/api/me/onboarding/dismiss", params={"dismissed": "false"})).json()["dismissed"] is False
