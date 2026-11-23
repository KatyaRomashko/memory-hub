"""Preferences & privacy and notifications (#723) against PostgreSQL + pgvector.

Covers per-person storage and revisions, how MCP finds an agent's owner and
applies their preferences, deleting and exporting one's own data, and
notifications with their unread state.

Uses the repository's integration database (tests/integration/compose.yaml,
localhost:15433) and skips when it is not reachable:

    podman compose -f tests/integration/compose.yaml up -d
    cd memoryhub-ui/backend && pytest tests/test_preferences_pg.py
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
from memoryhub_core.models.contradiction import ContradictionReport
from memoryhub_core.models.conversation import ConversationThread
from memoryhub_core.models.memory import MemoryNode
from memoryhub_core.models.project import Project, ProjectMembership
from memoryhub_core.models.schemas import MemoryNodeCreate
from memoryhub_core.services import user_prefs
from memoryhub_core.services.dreaming import extract_from_thread
from memoryhub_core.services.embeddings import MockEmbeddingService
from memoryhub_core.services.memory import create_memory
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.authorization import Access, AgentOwner, Principal, get_access
from src.config import Settings, get_settings
from src.database import get_db
from src.human_adapters import get_embedding_service
from src.main import app

TENANT = "tenant-prefs"
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
        for table in ("memoryhub_user_prefs", "memoryhub_ui_agent_owners"):
            await conn.execute(text(f"delete from {table} where tenant_id = :t"), {"t": TENANT})
        for table in ("audit_log", "contradiction_reports", "memory_relationships", "memory_nodes"):
            await conn.execute(text(f"delete from {table} where tenant_id = :t"), {"t": TENANT})
        await conn.execute(text("delete from project_memberships where project_id = 'atlas-p'"))
        await conn.execute(text("delete from projects where name = 'atlas-p'"))
    sessions = async_sessionmaker(database, expire_on_commit=False)
    async with sessions() as db:
        db.add(Project(name="atlas-p", tenant_id=TENANT, created_by="bob"))
        await db.flush()
        for user, role in (("alice", "member"), ("bob", "admin"), ("carol", "member")):
            db.add(ProjectMembership(project_id="atlas-p", user_id=user, role=role, joined_by="bob"))
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
            "alice": {"human_projects": {"atlas-p"}, "agents": {"alice-agent"}},
            "bob": {"human_projects": {"atlas-p"}, "human_project_admins": {"atlas-p"}},
            "carol": {"human_projects": {"atlas-p"}},
            "admin": {"human_projects": {"atlas-p"}, "human_project_admins": {"atlas-p"},
                      "moderation_scopes": {"project", "organizational"}},
        }[name]
        who["access"] = Access(principal=Principal(name, role, TENANT), **grants)

    async def seed(content: str, *, scope: str = "project", owner: str = "alice", actor: str = "alice-agent",
                   weight: float = 0.5, parent: uuid.UUID | None = None, branch: str | None = None) -> MemoryNode:
        async with sessions() as db:
            created, _ = await create_memory(MemoryNodeCreate(
                content=content, scope=scope, owner_id=owner, actor_id=actor, weight=weight,
                scope_id="atlas-p" if scope == "project" else None, parent_id=parent, branch_type=branch,
            ), db, embedding, tenant_id=TENANT, skip_curation=True)
            return created

    async with sessions() as db:
        db.add(AgentOwner(tenant_id=TENANT, client_id="alice-agent", owner="alice"))
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield {"http": http, "seed": seed, "act_as": act_as, "sessions": sessions}
    app.dependency_overrides.clear()


async def test_prefs_are_per_person_versioned_and_reach_the_owners_agents(env):
    http, act_as, sessions = env["http"], env["act_as"], env["sessions"]
    act_as("alice")
    first = (await http.get("/api/me/prefs")).json()
    assert first["prefs"]["write"] == {"default_scope": "user", "default_weight": 0.7}
    assert first["prefs"]["behavior"]["revision"] == 0 and first["prefs"]["behavior"]["template"] == "balanced"
    assert first["account"]["agents"] == ["alice-agent"] and first["account"]["projects"] == ["atlas-p"]

    behavior = {**{k: v for k, v in first["prefs"]["behavior"].items()
                   if k not in ("schema", "revision", "updated_at")}, "template": "custom",
                "custom_exclude": ["  salary  ", "Salary", "home address"]}
    saved = await http.patch("/api/me/prefs", json={
        "write": {"default_scope": "project", "default_weight": 0.55}, "behavior": behavior})
    assert saved.status_code == 200, saved.text
    prefs = saved.json()["prefs"]
    assert prefs["behavior"]["revision"] == 1 and prefs["behavior"]["custom_exclude"] == ["salary", "home address"]
    assert prefs["write"]["default_weight"] == 0.55

    again = await http.patch("/api/me/prefs", json={"behavior": {**behavior, "cadence": "every_turn"}})
    assert again.json()["prefs"]["behavior"]["revision"] == 2
    assert [h["revision"] for h in again.json()["prefs"]["behavior_history"]] == [1]
    same = await http.patch("/api/me/prefs", json={"dreaming": {"enabled": False, "fade_days": 90}})
    assert same.json()["prefs"]["behavior"]["revision"] == 2  # other sections do not bump the profile
    assert same.json()["prefs"]["dreaming"] == {"enabled": False, "fade_days": 90}
    assert (await http.patch("/api/me/prefs", json={"write": {"default_weight": 2}})).status_code == 422
    assert (await http.patch("/api/me/prefs", json={"unknown": 1})).status_code == 422

    # MCP's view: alice-agent acts for alice, so alice's preferences apply to it.
    async with sessions() as db:
        owner, applied = await user_prefs.prefs_for_identity(db, TENANT, "alice-agent")
        assert owner == "alice" and applied["write"]["default_weight"] == 0.55
        assert user_prefs.excluded_topic("Her salary review is in March", applied) == "salary"
        assert user_prefs.excluded_topic("Moved to a new HOME   ADDRESS", applied) == "home address"
        assert user_prefs.excluded_topic("The salaryman novel", applied) is None
        stranger, defaults = await user_prefs.prefs_for_identity(db, TENANT, "carol-agent")
        assert stranger == "carol-agent" and defaults["behavior"]["custom_exclude"] == []

    act_as("carol")  # carol's preferences are her own
    assert (await http.get("/api/me/prefs")).json()["prefs"]["write"]["default_weight"] == 0.7

    async with sessions() as db:
        events = (await db.execute(select(AuditLog.event_type).where(
            AuditLog.tenant_id == TENANT, AuditLog.event_type == "prefs.updated"))).scalars().all()
    assert len(events) == 3  # the two refused patches wrote nothing


async def test_delete_and_export_cover_only_my_own_personal_memories(env):
    http, seed, act_as, sessions = env["http"], env["seed"], env["act_as"], env["sessions"]
    mine = await seed("Alice prefers metric units", scope="user", owner="alice", actor="alice")
    agents = await seed("Alice's agent noted her editor", scope="user", owner="alice-agent", actor="alice-agent")
    project = await seed("Atlas gripper limit is 11.5 Nm", owner="alice", actor="alice")
    carols = await seed("Carol prefers imperial units", scope="user", owner="carol", actor="carol")

    act_as("alice")
    exported = await http.get("/api/memories/export", params={"mine": "true"})
    assert exported.status_code == 200
    assert "Alice prefers metric units" in exported.text and "Carol prefers" not in exported.text

    assert (await http.post("/api/me/data/delete", json={"confirm": "Alice"})).status_code == 422
    deleted = await http.post("/api/me/data/delete", json={"confirm": "alice"})
    assert deleted.status_code == 200 and deleted.json()["deleted"] == 2
    async with sessions() as db:
        state = {m.id: (await db.get(MemoryNode, m.id)).deleted_at is not None
                 for m in (mine, agents, project, carols)}
    assert state == {mine.id: True, agents.id: True, project.id: False, carols.id: False}
    assert (await http.get("/api/me/prefs")).json()["account"]["own_memories"] == 0

    act_as("admin", "admin")
    assert (await http.post("/api/me/data/delete", json={"confirm": "admin"})).status_code == 403


async def test_notifications_for_decisions_reports_and_incidents_with_unread(env):
    http, seed, act_as, sessions = env["http"], env["seed"], env["act_as"], env["sessions"]
    mine = await seed("Alice's torque note", scope="user", owner="alice", actor="alice")
    async with sessions() as db:
        db.add(AuditLog(event_type="request.decided", actor_id="admin", driver_id="admin", scope="user",
                        owner_id="alice", memory_id=mine.id, decision="allowed", tenant_id=TENANT,
                        event_metadata={"request_id": "REQ-ABC123", "outcome": "approved",
                                        "target_scope": "organizational"}))
        db.add(AuditLog(event_type="incident.access", actor_id="sre-oncall", driver_id="sre-oncall", scope="user",
                        owner_id="alice", memory_id=mine.id, decision="allowed", tenant_id=TENANT,
                        event_metadata={"incident": "INC-42"}))
        db.add(ContradictionReport(memory_id=mine.id, observed_behavior="Torque is 12 Nm now", confidence=0.8,
                                   reporter="bob-agent"))
        db.add(ContradictionReport(memory_id=mine.id, observed_behavior="My own note", confidence=0.5,
                                   reporter="alice-agent"))  # her own agent: not news
        await db.commit()

    act_as("alice")
    first = (await http.get("/api/notifications")).json()
    kinds = [n["kind"] for n in first["items"]]
    assert sorted(kinds) == ["break_glass", "contradiction", "review"] and first["unread"] == 3
    review = next(n for n in first["items"] if n["kind"] == "review")
    assert review["title"] == "Request REQ-ABC123 approved" and review["href"] == "/review?tab=decided"
    assert next(n for n in first["items"] if n["kind"] == "break_glass")["severity"] == "danger"
    assert "Torque is 12" not in str(first)  # a report's text stays on the memory page

    assert (await http.post("/api/notifications/seen")).json()["ok"] is True
    after = (await http.get("/api/notifications")).json()
    assert after["unread"] == 0 and len(after["items"]) == 3

    act_as("carol")
    assert (await http.get("/api/notifications")).json() == {"items": [], "unread": 0, "seen_at": None}


async def test_dreaming_off_skips_conversation_extraction_for_the_owners_threads(env):
    http, act_as, sessions = env["http"], env["act_as"], env["sessions"]
    async with sessions() as db:
        await db.execute(text("delete from conversation_threads where tenant_id = :t"), {"t": TENANT})
        thread = ConversationThread(scope="user", owner_id="alice-agent", tenant_id=TENANT,
                                    participant_ids=["alice-agent"], extraction_cursor=3)
        db.add(thread)
        await db.commit()

    act_as("alice")
    await http.patch("/api/me/prefs", json={"dreaming": {"enabled": False, "fade_days": 60}})
    async with sessions() as db:
        result = await extract_from_thread(db, thread_id=thread.id, tenant_id=TENANT, owner_id="alice-agent",
                                           embedding_service=MockEmbeddingService(),
                                           model_override="never-called", url_override="http://127.0.0.1:9")
    assert result["skipped"] == "dreaming_disabled" and result["extracted_count"] == 0 and result["cursor"] == 3
    async with sessions() as db:
        await db.execute(text("delete from conversation_threads where tenant_id = :t"), {"t": TENANT})
        await db.commit()
