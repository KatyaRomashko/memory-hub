"""My activity (#718) against a real PostgreSQL + pgvector database.

Covers the person's activity log, who read their memories and the background
jobs on them, with the SQL the mocked sessions cannot check.

Uses the repository's integration database (tests/integration/compose.yaml,
localhost:15433) and skips when it is not reachable:

    podman compose -f tests/integration/compose.yaml up -d
    cd memoryhub-ui/backend && pytest tests/test_my_activity_pg.py
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
from memoryhub_core.models.conversation import ConversationExtractionFailure, ConversationThread
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

TENANT = "tenant-activity"
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
        for table in ("conversation_extraction_failures", "conversation_extractions", "conversation_threads",
                      "audit_log", "contradiction_reports", "memory_relationships", "memory_nodes"):
            await conn.execute(text(f"delete from {table} where tenant_id = :t"), {"t": TENANT})
        await conn.execute(text("delete from project_memberships where project_id = 'atlas-a'"))
        await conn.execute(text("delete from projects where name = 'atlas-a'"))
    sessions = async_sessionmaker(database, expire_on_commit=False)
    async with sessions() as db:
        db.add(Project(name="atlas-a", tenant_id=TENANT, created_by="bob"))
        await db.flush()
        for user, role in (("alice", "member"), ("bob", "admin"), ("carol", "member")):
            db.add(ProjectMembership(project_id="atlas-a", user_id=user, role=role, joined_by="bob"))
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
            "alice": {"human_projects": {"atlas-a"}, "agents": {"alice-agent"}},
            "bob": {"human_projects": {"atlas-a"}, "human_project_admins": {"atlas-a"}},
            "carol": {"human_projects": {"atlas-a"}},
            "admin": {"human_projects": {"atlas-a"}, "human_project_admins": {"atlas-a"},
                      "moderation_scopes": {"project", "organizational"}},
        }[name]
        who["access"] = Access(principal=Principal(name, role, TENANT), **grants)

    async def seed(content: str, *, scope: str = "project", owner: str = "alice", actor: str = "alice-agent",
                   weight: float = 0.5, parent: uuid.UUID | None = None, branch: str | None = None) -> MemoryNode:
        async with sessions() as db:
            created, _ = await create_memory(MemoryNodeCreate(
                content=content, scope=scope, owner_id=owner, actor_id=actor, weight=weight,
                scope_id="atlas-a" if scope == "project" else None, parent_id=parent, branch_type=branch,
            ), db, embedding, tenant_id=TENANT, skip_curation=True)
            return created

    async def new_version(memory_id: uuid.UUID, content: str):
        async with sessions() as db:
            return await update_memory(memory_id, MemoryNodeUpdate(content=content), db, embedding,
                                       actor_id="alice", driver_id="alice")

    async def audit(event_type: str, actor: str, memory_id=None, minutes_ago: float = 0, *,
                    decision: str = "allowed", scope: str = "project", driver: str | None = None, **metadata):
        async with sessions() as db:
            db.add(AuditLog(event_type=event_type, actor_id=actor, driver_id=driver or actor, scope=scope,
                            owner_id="alice", memory_id=memory_id, decision=decision, tenant_id=TENANT,
                            event_metadata=metadata or None,
                            timestamp=datetime.now(UTC) - timedelta(minutes=minutes_ago)))
            await db.commit()

    async def fetch(memory_id) -> MemoryNode:
        async with sessions() as db:
            return (await db.execute(select(MemoryNode).where(MemoryNode.id == memory_id))).scalar_one()

    async def set_meta(memory_id, **meta):
        async with sessions() as db:
            node = (await db.execute(select(MemoryNode).where(MemoryNode.id == memory_id))).scalar_one()
            node.metadata_ = {**(node.metadata_ or {}), **meta}
            await db.commit()

    async def conversation_failure(owner: str, error: str):
        async with sessions() as db:
            thread = ConversationThread(scope="user", owner_id=owner, actor_id=owner, tenant_id=TENANT)
            db.add(thread)
            await db.flush()
            db.add(ConversationExtractionFailure(thread_id=thread.id, window_start=0, window_end=4,
                                                 attempt_count=2, last_error=error, tenant_id=TENANT))
            await db.commit()

    act_as("alice")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield {"http": http, "act_as": act_as, "seed": seed, "new_version": new_version, "audit": audit,
               "fetch": fetch, "set_meta": set_meta, "conversation_failure": conversation_failure}
async def test_activity_log_is_the_person_and_their_agents_only(env):
    http, seed, audit, new_version = env["http"], env["seed"], env["audit"], env["new_version"]
    mine = await seed("Deploy window is Tuesday")
    await new_version(mine.id, "Deploy window is Wednesday")
    await audit("memory.write", "alice-agent", mine.id, minutes_ago=30)
    await audit("memory.update", "alice", mine.id, minutes_ago=20, scope="project")
    await audit("memory.write", "alice-agent", None, minutes_ago=10, decision="denied", scope="user",
                rule="secrets_scan", blocked_by="curation_rule")
    await audit("memory.search", "alice-agent", None, minutes_ago=5, scope="all", query="deploy window")
    await audit("memory.read", "carol", mine.id, minutes_ago=4)              # someone else: not alice's activity
    await audit("memory.write", "alice-agent", None, minutes_ago=60 * 24 * 10)  # outside the default 7 days

    body = (await http.get("/api/audit")).json()
    assert body["total"] == 4
    assert [e["operation"] for e in body["items"]] == ["memory.search", "memory.write", "memory.update", "memory.write"]
    search, blocked, update, write = body["items"]
    assert search["metadata"] == {"query": "deploy window"} and search["actor_type"] == "agent"
    assert blocked["decision"] == "denied" and blocked["metadata"]["rule"] == "secrets_scan"
    assert update["actor_type"] == "user" and update["memory_stub"]
    assert update["change"] == {"before": {"version": 1, "weight": 0.5}, "after": {"version": 2, "weight": 0.5}}
    summary = body["summary"]
    assert summary["changes_by_me"] == 1 and summary["agent_writes"] == 1 and summary["blocked"] == 1
    assert summary["agent_writes_project"] == 1
    assert summary["blocked_recent"][0]["rule"] == "secrets_scan"
    assert {a["actor_id"] for a in summary["by_actor"]} == {"alice", "alice-agent"}

    only_agents = (await http.get("/api/audit", params={"actor": "agents"})).json()
    assert {e["actor_id"] for e in only_agents["items"]} == {"alice-agent"}
    denied = (await http.get("/api/audit", params={"decision": "denied"})).json()
    assert denied["total"] == 1
    assert (await http.get("/api/audit", params={"q": "Tuesday"})).json()["total"] == 2
    assert (await http.get("/api/audit", params={"operation": ["memory.update"]})).json()["total"] == 1
    assert (await http.get("/api/audit", params={"hours": 24 * 30})).json()["total"] == 5

    env["act_as"]("carol")
    carol = (await http.get("/api/audit")).json()
    assert [e["actor_id"] for e in carol["items"]] == ["carol"]


async def test_who_read_my_memories_lists_readers_and_why_without_content(env):
    http, seed, audit = env["http"], env["seed"], env["audit"]
    mine = await seed("Gripper torque limit is 11.5 Nm")
    personal = await seed("My standing notes", scope="user", owner="alice", actor="alice")
    carols = await seed("Carol's note", owner="carol", actor="carol")
    await audit("memory.read", "carol", mine.id, minutes_ago=50)
    await audit("memory.read", "carol", mine.id, minutes_ago=40)
    await audit("memory.read", "alice-agent", mine.id, minutes_ago=30)
    await audit("memory.read", "mallory-agent", personal.id, minutes_ago=20, decision="denied", scope="user")
    await audit("memory.read", "bob", carols.id, minutes_ago=10)             # not alice's memory

    body = (await http.get("/api/me/reads")).json()
    assert {r["actor_id"] for r in body["readers"]} == {"carol", "mallory-agent"}
    carol = next(r for r in body["readers"] if r["actor_id"] == "carol")
    assert carol["count"] == 2 and carol["memories"] == 1
    assert (carol["reason"], carol["reason_kind"]) == ("Member of atlas-a", "project")
    mallory = next(r for r in body["readers"] if r["actor_id"] == "mallory-agent")
    assert mallory["denied"] == 1 and mallory["reason"] is None
    assert body["summary"]["reads_by_others"] == 2 and body["summary"]["personal_by_others"] == 0
    assert body["top_memories"][0]["id"] == str(mine.id) and body["top_memories"][0]["count"] == 2
    assert "Carol's note" not in str(body)

    everyone = (await http.get("/api/me/reads", params={"who": "all"})).json()
    agent = next(r for r in everyone["readers"] if r["actor_id"] == "alice-agent")
    assert agent["yours"] and agent["reason_kind"] == "own"


async def test_background_jobs_are_own_and_failed_extraction_can_be_retried(env, monkeypatch):
    http, seed = env["http"], env["seed"]
    failed = await seed("Atlas arm calibration")
    done = await seed("Atlas wrist limits")
    carols = await seed("Carol's calibration", owner="carol", actor="carol")
    await env["set_meta"](failed.id, extraction_status="failed")
    await env["set_meta"](done.id, extraction_status="complete", extracted_entities=[{"name": "Atlas"}])
    await env["set_meta"](carols.id, extraction_status="failed")
    await env["conversation_failure"]("alice", "timeout talking to the model")
    started = []

    async def fake_trigger(memory_id, content, tenant_id, owner_id, embedding_service):
        started.append(memory_id)

    monkeypatch.setattr("src.activity_adapter.trigger_extraction", fake_trigger)

    body = (await http.get("/api/operations")).json()
    by_id = {j["id"]: j for j in body["items"]}
    assert set(by_id) >= {f"extract:{failed.id}", f"extract:{done.id}"}
    assert f"extract:{carols.id}" not in by_id
    assert by_id[f"extract:{failed.id}"]["status"] == "failed" and by_id[f"extract:{failed.id}"]["retryable"]
    assert by_id[f"extract:{done.id}"]["detail"]["entities"] == 1
    conv = [j for j in body["items"] if j["kind"] == "conversation_extraction"]
    assert conv[0]["status"] == "failed" and conv[0]["attempts"] == 2 and "timeout" in conv[0]["error"]
    assert {k["kind"]: k["failed"] for k in body["kinds"]} == {"entity_extraction": 1, "conversation_extraction": 1}
    assert (await http.get("/api/operations", params={"status": "ok"})).json()["items"][0]["id"] == f"extract:{done.id}"

    assert (await http.post(f"/api/operations/extract:{done.id}/retry")).status_code == 409
    assert (await http.post(f"/api/operations/extract:{carols.id}/retry")).status_code == 404
    assert (await http.post(f"/api/operations/{conv[0]['id']}/retry")).status_code == 409
    retried = await http.post(f"/api/operations/extract:{failed.id}/retry")
    assert retried.status_code == 200 and started == [failed.id]
    assert (await env["fetch"](failed.id)).metadata_["extraction_status"] == "pending"
    again = {j["id"]: j for j in (await http.get("/api/operations")).json()["items"]}[f"extract:{failed.id}"]
    assert again["status"] == "running" and not again["stale"] and again["attempts"] == 2
    assert again["trigger"] == "retried from the dashboard"
    after = (await http.get("/api/audit", params={"operation": ["job.retry"]})).json()
    assert after["total"] == 1
