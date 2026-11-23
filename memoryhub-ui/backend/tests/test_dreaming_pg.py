"""Dreaming (#716) against a real PostgreSQL + pgvector database.

Embeddings are set by hand so that duplicates, connections and unrelated
memories are deterministic. Covers what a run proposes and never proposes,
accepting and dismissing insights, consolidation through curation, and the
schedule runner.

Uses the repository's integration database (tests/integration/compose.yaml,
localhost:15433) and skips when it is not reachable:

    podman compose -f tests/integration/compose.yaml up -d
    cd memoryhub-ui/backend && pytest tests/test_dreaming_pg.py
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
from memoryhub_core.models.contradiction import ContradictionReport
from memoryhub_core.models.memory import MemoryNode, MemoryRelationship
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
from src.dreams_adapter import run_due
from src.human_adapters import get_embedding_service
from src.main import app

TENANT = "tenant-dreams"
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
        for table in ("memoryhub_ui_dream_runs", "memoryhub_ui_dream_settings", "curator_rules"):
            await conn.execute(text(f"delete from {table} where tenant_id = :t"), {"t": TENANT})
        for table in ("audit_log", "contradiction_reports", "memory_relationships", "memory_nodes"):
            await conn.execute(text(f"delete from {table} where tenant_id = :t"), {"t": TENANT})
        await conn.execute(text("delete from project_memberships where project_id = 'atlas-dr'"))
        await conn.execute(text("delete from projects where name = 'atlas-dr'"))
    sessions = async_sessionmaker(database, expire_on_commit=False)
    async with sessions() as db:
        db.add(Project(name="atlas-dr", tenant_id=TENANT, created_by="bob"))
        await db.flush()
        for user, role in (("alice", "member"), ("bob", "admin"), ("carol", "member")):
            db.add(ProjectMembership(project_id="atlas-dr", user_id=user, role=role, joined_by="bob"))
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
            "alice": {"human_projects": {"atlas-dr"}, "agents": {"alice-agent"}},
            "bob": {"human_projects": {"atlas-dr"}, "human_project_admins": {"atlas-dr"}},
            "carol": {"human_projects": {"atlas-dr"}},
            "admin": {"human_projects": {"atlas-dr"}, "human_project_admins": {"atlas-dr"},
                      "moderation_scopes": {"project", "organizational"}},
        }[name]
        who["access"] = Access(principal=Principal(name, role, TENANT), **grants)

    async def seed(content: str, *, scope: str = "project", owner: str = "alice", actor: str = "alice-agent",
                   weight: float = 0.5, parent: uuid.UUID | None = None, branch: str | None = None) -> MemoryNode:
        async with sessions() as db:
            created, _ = await create_memory(MemoryNodeCreate(
                content=content, scope=scope, owner_id=owner, actor_id=actor, weight=weight,
                scope_id="atlas-dr" if scope == "project" else None, parent_id=parent, branch_type=branch,
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


async def _world(env):
    seed, shape = env["seed"], env["shape"]
    m = {}
    m["newer"] = await shape(await seed("Atlas gripper limit is 11.5 Nm", scope="user", owner="alice", actor="alice"),
                             cos=1.0, days_old=1)
    m["older"] = await shape(await seed("Gripper torque limit 11.5 Nm on Atlas", scope="user", owner="alice",
                                        actor="alice"), cos=0.97, axis=2, days_old=5)
    m["related"] = await shape(await seed("Atlas gripper calibration steps", scope="user", owner="alice",
                                          actor="alice"), cos=0.85, axis=3, days_old=2)
    m["carols"] = await shape(await seed("Carol's Atlas gripper notes", scope="user", owner="carol", actor="carol"),
                              cos=0.99, axis=4)
    m["project"] = await shape(await seed("Atlas gripper limit in the project", owner="carol", actor="carol"),
                               cos=0.98, axis=5)
    m["stale"] = await shape(await seed("Old deployment checklist", scope="user", owner="alice", actor="alice"),
                             cos=0.0, axis=6, days_old=90, weight=0.8)
    for i in range(4):
        m[f"theme{i}"] = await shape(await seed(f"Robotics fact {i}", scope="user", owner="alice", actor="alice"),
                                     cos=0.0, axis=10 + i, domains=["robotics"])
    async with env["sessions"]() as db:
        db.add(ContradictionReport(memory_id=m["project"].id, observed_behavior="It is 12 Nm now", confidence=0.8,
                                   reporter="bob-agent"))
        await db.commit()
    return m


async def test_a_run_proposes_from_readable_memories_only(env):
    http, act_as = env["http"], env["act_as"]
    m = await _world(env)
    act_as("alice")
    report = (await http.post("/api/dreams/run")).json()
    by_kind = {}
    for insight in report["insights"]:
        by_kind.setdefault(insight["kind"], []).append({x["id"] for x in insight["memories"]})
    assert by_kind["duplicates"] == [{str(m["newer"].id), str(m["older"].id)}]
    assert {str(m["newer"].id), str(m["related"].id)} in by_kind["connections"]
    assert by_kind["fading"] == [{str(m["stale"].id)}]
    assert by_kind["contradictions"] == [{str(m["project"].id)}]
    assert by_kind["themes"] == [{str(m[f"theme{i}"].id) for i in range(4)}]
    everything = str(report)
    assert str(m["carols"].id) not in everything and "Carol's Atlas" not in everything
    # A personal and a project memory are never paired: they cannot be linked.
    assert all(str(m["project"].id) not in ids for kind in ("duplicates", "connections") for ids in by_kind[kind])
    # Alice reads 9 of the 10: her 7, the project memory, and not Carol's personal one.
    assert report["trigger"] == "manual" and report["reviewed"] == 9

    dreams = (await http.get("/api/dreams")).json()
    assert dreams["last"]["id"] == report["id"] and dreams["history"][0]["total"] == len(report["insights"])

    act_as("admin", "admin")
    assert (await http.get("/api/dreams")).status_code == 403
    env["labs"]["on"] = False
    act_as("alice")
    assert (await http.get("/api/dreams")).status_code == 404


async def test_accepting_merges_links_fades_and_dismissed_ones_stay_away(env):
    http, act_as, sessions = env["http"], env["act_as"], env["sessions"]
    m = await _world(env)
    act_as("alice")
    report = (await http.post("/api/dreams/run")).json()
    find = lambda kind: next(i for i in report["insights"] if i["kind"] == kind)  # noqa: E731
    run = report["id"]

    merged = await http.post(f"/api/dreams/{run}/insights/{find('duplicates')['id']}/decision",
                             json={"decision": "accept"})
    assert merged.status_code == 200, merged.text
    linked = next(i for i in report["insights"] if i["kind"] == "connections"
                  and {x["id"] for x in i["memories"]} == {str(m["newer"].id), str(m["related"].id)})
    assert (await http.post(f"/api/dreams/{run}/insights/{linked['id']}/decision",
                            json={"decision": "accept"})).json()["status"] == "accepted"
    fade = find("fading")
    assert (await http.post(f"/api/dreams/{run}/insights/{fade['id']}/decision",
                            json={"decision": "dismiss"})).json()["status"] == "dismissed"
    assert (await http.post(f"/api/dreams/{run}/insights/{fade['id']}/decision",
                            json={"decision": "accept"})).status_code == 409

    async with sessions() as db:
        older = (await db.execute(select(MemoryNode).where(
            MemoryNode.previous_version_id == m["older"].id))).scalar_one()
        assert older.is_current and older.weight == 0.2
        edges = {(r.source_id, r.target_id, r.relationship_type) for r in (await db.execute(
            select(MemoryRelationship).where(MemoryRelationship.tenant_id == TENANT))).scalars()}
        assert (m["newer"].id, older.id, "supersedes") in edges
        assert {(m["newer"].id, m["related"].id, "related_to"), (m["related"].id, m["newer"].id, "related_to")} & edges
        events = (await db.execute(select(AuditLog.event_type).where(
            AuditLog.tenant_id == TENANT, AuditLog.actor_id == "alice"))).scalars().all()
        assert events.count("dream.insight") == 3 and "memory.relationship_created" in events

    again = (await http.post("/api/dreams/run")).json()
    assert fade["id"] not in {i["id"] for i in again["insights"]}  # dismissed: not proposed again
    assert not any(i["kind"] == "duplicates" for i in again["insights"])  # the older one now supersedes-linked
    act_as("carol")
    assert (await http.post(f"/api/dreams/{run}/insights/{fade['id']}/decision",
                            json={"decision": "dismiss"})).status_code == 404


async def test_consolidating_a_theme_goes_through_curation(env):
    http, act_as, sessions = env["http"], env["act_as"], env["sessions"]
    m = await _world(env)
    act_as("alice")
    report = (await http.post("/api/dreams/run")).json()
    theme = next(i for i in report["insights"] if i["kind"] == "themes")
    assert theme["action"]["draft"].startswith("robotics: what we know")

    blocked = await http.post(f"/api/dreams/{report['id']}/insights/{theme['id']}/consolidate",
                              json={"content": "Robotics owner SSN 123-45-6789"})
    assert blocked.status_code == 409 and "Curation" in blocked.json()["detail"]
    written = await http.post(f"/api/dreams/{report['id']}/insights/{theme['id']}/consolidate",
                              json={"content": "Robotics: four facts we rely on."})
    assert written.status_code == 201, written.text
    new_id = uuid.UUID(written.json()["memory_id"])
    async with sessions() as db:
        node = await db.get(MemoryNode, new_id)
        assert node.owner_id == "alice" and node.domains == ["robotics"]
        assert set(node.metadata_["consolidated_from"]) == {str(m[f"theme{i}"].id) for i in range(4)}
        derived = (await db.execute(select(MemoryRelationship.target_id).where(
            MemoryRelationship.source_id == new_id,
            MemoryRelationship.relationship_type == "derived_from"))).scalars().all()
        assert set(derived) == {m[f"theme{i}"].id for i in range(4)}
    assert (await http.post(f"/api/dreams/{report['id']}/insights/{theme['id']}/consolidate",
                            json={"content": "again"})).status_code == 409


async def test_schedule_runs_due_dreams_once_and_can_act_on_its_own(env):
    http, act_as, sessions = env["http"], env["act_as"], env["sessions"]
    m = await _world(env)
    act_as("alice")
    saved = await http.patch("/api/dreams/settings", json={"schedule": "nightly", "window": "03:30",
                                                          "auto": {"fading": True}})
    assert saved.status_code == 200
    next_at = datetime.fromisoformat(saved.json()["next_run_at"])
    assert (next_at.hour, next_at.minute) == (3, 30) and next_at > datetime.now(UTC)
    assert (await http.patch("/api/dreams/settings", json={"auto": {"themes": True}})).status_code == 422

    settings = Settings(ui_tenant_id=TENANT, embedding_url="mock", ui_labs=True)
    assert await run_due(sessions, settings, env["embedding"]) == 0  # not due yet
    async with sessions() as db:
        await db.execute(text("update memoryhub_ui_dream_settings set next_run_at = now() - interval '1 minute' "
                              "where tenant_id = :t"), {"t": TENANT})
        await db.commit()
    assert await run_due(sessions, settings, env["embedding"]) == 1
    assert await run_due(sessions, settings, env["embedding"]) == 0  # moved to the next window

    last = (await http.get("/api/dreams")).json()["last"]
    assert last["trigger"] == "scheduled"
    fading = next(i for i in last["insights"] if i["kind"] == "fading")
    assert fading["status"] == "accepted" and fading["auto"] is True
    async with sessions() as db:
        faded = (await db.execute(select(MemoryNode).where(
            MemoryNode.previous_version_id == m["stale"].id))).scalar_one()
        assert faded.weight == 0.4
    assert (await http.patch("/api/dreams/settings", json={"schedule": "off"})).json()["next_run_at"] is None


async def test_two_scheduler_passes_at_once_run_a_due_dream_once(env):
    import asyncio

    http, act_as, sessions = env["http"], env["act_as"], env["sessions"]
    await _world(env)
    act_as("alice")
    await http.patch("/api/dreams/settings", json={"schedule": "weekly", "window": "04:00"})
    async with sessions() as db:
        await db.execute(text("update memoryhub_ui_dream_settings set next_run_at = now() - interval '1 minute' "
                              "where tenant_id = :t"), {"t": TENANT})
        await db.commit()
    settings = Settings(ui_tenant_id=TENANT, embedding_url="mock", ui_labs=True)
    ran = await asyncio.gather(run_due(sessions, settings), run_due(sessions, settings))
    assert sum(ran) == 1
    async with sessions() as db:
        runs = (await db.execute(text("select count(*) from memoryhub_ui_dream_runs where tenant_id = :t"),
                                 {"t": TENANT})).scalar_one()
        next_at = (await db.execute(text("select next_run_at from memoryhub_ui_dream_settings where tenant_id = :t"),
                                    {"t": TENANT})).scalar_one()
    assert runs == 1 and next_at > datetime.now(UTC) + timedelta(days=5)  # weekly: six days after the run
