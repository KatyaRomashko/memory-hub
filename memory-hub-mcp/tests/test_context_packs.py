"""memory(action="packs") (#715) against the integration PostgreSQL (localhost:15433).

An agent receives the packs its owner attached to it, rendered with the
agent's own read access; a pack stops being served when the owner no longer
owns the agent. Skips when the database is not reachable.
"""

import os
import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from src.tools import context_packs as module

from memoryhub_core.models.knowledge import ContextPack
from memoryhub_core.models.schemas import MemoryNodeCreate
from memoryhub_core.services.embeddings import MockEmbeddingService
from memoryhub_core.services.memory import create_memory

TENANT = "tenant-packs-mcp"


def _url() -> str:
    env = os.environ.get
    return (f"postgresql+asyncpg://{env('MEMORYHUB_DB_USER', 'memoryhub')}:"
            f"{env('MEMORYHUB_DB_PASSWORD', 'memoryhub-test')}@{env('MEMORYHUB_DB_HOST', 'localhost')}:"
            f"{env('MEMORYHUB_DB_PORT', '15433')}/{env('MEMORYHUB_DB_NAME', 'memoryhub')}")


async def _ready(engine) -> bool:
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text("select to_regclass('memoryhub_context_packs')"))).scalar() is not None
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.asyncio
async def test_agent_receives_its_packs_with_its_own_access():
    engine = create_async_engine(_url(), poolclass=NullPool)
    if not await _ready(engine):
        pytest.skip("Integration PostgreSQL with the 034_knowledge migration is not available")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        for table in ("memoryhub_context_packs", "memoryhub_ui_agent_owners", "audit_log", "memory_nodes"):
            await conn.execute(text(f"delete from {table} where tenant_id = :t"), {"t": TENANT})
        await conn.execute(text("insert into memoryhub_ui_agent_owners (tenant_id, client_id, owner) "
                                "values (:t, 'alice-agent', 'alice')"), {"t": TENANT})
    embedding = MockEmbeddingService()
    async with sessions() as db:
        mine, _ = await create_memory(MemoryNodeCreate(content="Alice prefers metric units", scope="user",
                                                       owner_id="alice-agent", actor_id="alice-agent"),
                                      db, embedding, tenant_id=TENANT, skip_curation=True)
    async with sessions() as db:
        other, _ = await create_memory(MemoryNodeCreate(content="Bob's private note", scope="user",
                                                        owner_id="bob", actor_id="bob"),
                                       db, embedding, tenant_id=TENANT, skip_curation=True)
    now = datetime.now(UTC)
    async with sessions() as db:
        db.add(ContextPack(id=uuid.uuid4(), tenant_id=TENANT, owner="alice", name="Basics",
                           pinned=[str(mine.id), str(other.id)], queries=[], token_budget=2000,
                           visibility="private", agents=["alice-agent"], created_at=now, updated_at=now))
        db.add(ContextPack(id=uuid.uuid4(), tenant_id=TENANT, owner="alice", name="Not attached", pinned=[],
                           queries=[], token_budget=2000, visibility="private", agents=[],
                           created_at=now, updated_at=now))
        await db.commit()

    async def session():
        async with sessions() as s:
            yield s

    async def get_db_session():
        gen = session()
        return await gen.__anext__(), gen

    async def release(gen):
        await gen.aclose()

    claims = {"sub": "alice-agent", "tenant_id": TENANT, "scopes": ["memory:read:user"], "identity_type": "user"}
    with patch.object(module, "get_db_session", get_db_session), patch.object(module, "release_db_session", release), \
            patch.object(module, "get_claims_from_context", lambda: claims), \
            patch.object(module, "get_embedding_service", lambda: embedding):
        result = await module.context_packs()
        assert [p["name"] for p in result["packs"]] == ["Basics"]
        pack = result["packs"][0]
        assert [i["id"] for i in pack["items"]] == [str(mine.id)] and pack["omitted"] == 1
        assert "metric units" in pack["markdown"] and "Bob" not in pack["markdown"]

        async with engine.begin() as conn:
            await conn.execute(text("update memoryhub_ui_agent_owners set owner = 'bob' where tenant_id = :t"),
                               {"t": TENANT})
        assert (await module.context_packs())["packs"] == []
    async with engine.begin() as conn:
        assert (await conn.execute(text("select count(*) from audit_log where tenant_id = :t "
                                        "and event_type = 'packs.served'"), {"t": TENANT})).scalar() == 1
    await engine.dispose()
