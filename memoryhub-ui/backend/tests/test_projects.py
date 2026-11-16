"""Project management API (#671) against an in-memory SQLite database."""

import uuid
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from memoryhub_core.models.memory import MemoryNode
from memoryhub_core.models.project import Project, ProjectMembership
from sqlalchemy import Text, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.config import Settings, get_settings
from src.database import get_db
from src.main import app

TENANT = "default"
ADMIN = {"x-forwarded-user": "ops"}
CAROL = {"x-forwarded-user": "carol"}  # project admin of team-x
ALICE = {"x-forwarded-user": "alice"}  # member of team-x
BOB = {"x-forwarded-user": "bob"}  # no memberships
RAY = {"x-forwarded-user": "ray"}  # created team-x and secret


@pytest.fixture
def portable_memory_table():
    """memory_nodes uses PostgreSQL-only types; swap them for this module's SQLite DB."""
    from memoryhub_local.models.dialect import JsonEncodedList, JsonEncodedVector

    table = MemoryNode.__table__
    swaps = [
        (table.c.embedding, JsonEncodedVector()),
        (table.c.domains, JsonEncodedList()),
        (table.c.search_vector, Text()),
    ]
    saved = [(col, col.type, col.server_default, col.computed) for col, _ in swaps]
    removed = {i for i in table.indexes if any(c.name in ("domains", "search_vector") for c in i.columns)}
    for col, new_type in swaps:
        col.type = new_type
        col.server_default = None
        col.computed = None
    table.indexes -= removed
    yield
    for col, old_type, default, computed in saved:
        col.type, col.server_default, col.computed = old_type, default, computed
    table.indexes |= removed


@pytest.fixture
async def factory(portable_memory_table):
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        tables = [Project.__table__, ProjectMembership.__table__, MemoryNode.__table__]
        await conn.run_sync(lambda c: Project.metadata.create_all(c, tables=tables))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _memory(scope_id: str, tenant: str = TENANT) -> MemoryNode:
    now = datetime.now(UTC)
    mid = uuid.uuid4()
    return MemoryNode(
        id=mid,
        logical_id=mid,
        content="m",
        stub="m",
        scope="project",
        scope_id=scope_id,
        owner_id="alice",
        tenant_id=tenant,
        status="active",
        weight=0.7,
        is_current=True,
        version=1,
        storage_type="inline",
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
async def seeded(factory):
    async with factory() as s:
        s.add_all(
            [
                Project(name="team-x", created_by="ray", tenant_id=TENANT, description="Team X"),
                Project(name="secret", created_by="ray", tenant_id=TENANT, invite_only=True),
                Project(name="theirs", created_by="zed", tenant_id="other"),
            ]
        )
        await s.flush()
        s.add_all(
            [
                ProjectMembership(id=uuid.uuid4(), project_id="team-x", user_id="carol", role="admin", joined_by="ray"),
                ProjectMembership(
                    id=uuid.uuid4(), project_id="team-x", user_id="alice", role="member", joined_by="ray"
                ),
                ProjectMembership(id=uuid.uuid4(), project_id="secret", user_id="ray", role="admin", joined_by="ray"),
                _memory("team-x"),
                _memory("team-x"),
            ]
        )
        await s.commit()
    return factory


def _client(factory, **settings) -> AsyncClient:
    config = Settings(ui_tenant_id=TENANT, **settings)

    async def db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_settings] = lambda: config
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def api(seeded):
    async with _client(seeded, ui_trust_proxy_headers=True, ui_admin_users="ops") as client:
        yield client
    app.dependency_overrides.clear()


# -- identity ------------------------------------------------------------------


async def test_signed_in_user_is_required_when_trusting_the_proxy(api):
    assert (await api.get("/api/projects")).status_code == 401


async def test_admin_group_header_grants_platform_admin(api):
    r = await api.get(
        "/api/projects", headers={"x-forwarded-user": "gina", "x-forwarded-groups": "devs,memoryhub-admins"}
    )
    assert {p["name"] for p in r.json()["items"]} == {"team-x", "secret"}


async def test_operator_mode_acts_as_platform_admin(seeded):
    async with _client(seeded) as client:
        r = await client.get("/api/projects", headers=BOB)  # headers ignored without trust
        created = await client.post("/api/projects", json={"name": "ops-made"})
    app.dependency_overrides.clear()
    assert {p["name"] for p in r.json()["items"]} == {"team-x", "secret"}
    assert created.json()["created_by"] == "dashboard"


# -- list and detail -------------------------------------------------------------


async def test_list_hides_invite_only_projects_and_other_tenants(api):
    body = (await api.get("/api/projects", headers=BOB)).json()
    assert [p["name"] for p in body["items"]] == ["team-x"]
    assert body["can_create"] is True
    team = body["items"][0]
    assert team["memory_count"] == 2 and team["member_count"] == 2
    assert team["my_role"] is None and team["can_manage"] is False


async def test_list_shows_roles_and_rights(api):
    items = {p["name"]: p for p in (await api.get("/api/projects", headers=RAY)).json()["items"]}
    assert set(items) == {"team-x", "secret"}
    assert items["team-x"]["my_role"] is None and items["team-x"]["can_manage"] is True  # creator
    assert items["secret"]["my_role"] == "admin"


async def test_list_filters_and_searches(api):
    await api.post("/api/projects/team-x/archive", headers=CAROL)
    assert (await api.get("/api/projects?archived=true", headers=ADMIN)).json()["total"] == 1
    assert [p["name"] for p in (await api.get("/api/projects?archived=false", headers=ADMIN)).json()["items"]] == [
        "secret"
    ]
    assert [p["name"] for p in (await api.get("/api/projects?q=team", headers=ADMIN)).json()["items"]] == ["team-x"]


async def test_detail_lists_members(api):
    body = (await api.get("/api/projects/team-x", headers=ALICE)).json()
    assert {m["user_id"]: m["role"] for m in body["members"]} == {"carol": "admin", "alice": "member"}
    assert body["my_role"] == "member" and body["can_delete"] is False


async def test_hidden_and_foreign_projects_answer_404(api):
    assert (await api.get("/api/projects/secret", headers=BOB)).status_code == 404
    assert (await api.get("/api/projects/theirs", headers=ADMIN)).status_code == 404
    assert (await api.get("/api/projects/Bad_Name", headers=ADMIN)).status_code == 422


# -- create ----------------------------------------------------------------------


async def test_any_signed_in_user_creates_and_becomes_admin(api):
    r = await api.post(
        "/api/projects",
        headers=BOB,
        json={
            "name": "bobs-lab",
            "description": "Lab",
            "invite_only": True,
            "policies": {"ttl_days": 30, "default_weight": 0.5},
            "members": [{"user_id": "alice"}, {"user_id": "carol", "role": "admin"}, {"user_id": "bob"}],
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["my_role"] == "admin" and body["invite_only"] is True
    assert body["policies"] == {"ttl_days": 30, "max_memories": None, "default_weight": 0.5}
    assert {m["user_id"]: m["role"] for m in body["members"]} == {"bob": "admin", "alice": "member", "carol": "admin"}


@pytest.mark.parametrize("name", ["Team", "a_b", "-x", "x-", "a--b", "a" * 64, ""])
async def test_create_requires_a_slug_name(api, name):
    assert (await api.post("/api/projects", headers=BOB, json={"name": name})).status_code == 422


async def test_create_rejects_bad_policies_and_taken_names(api):
    bad = await api.post("/api/projects", headers=BOB, json={"name": "p", "policies": {"default_weight": 2}})
    assert bad.status_code == 422
    assert (await api.post("/api/projects", headers=BOB, json={"name": "team-x"})).status_code == 409
    assert (await api.post("/api/projects", headers=BOB, json={"name": "theirs"})).status_code == 409


# -- update ----------------------------------------------------------------------


async def test_managers_update_settings_and_policies(api):
    r = await api.patch(
        "/api/projects/team-x",
        headers=CAROL,
        json={"description": "New", "invite_only": True, "policies": {"max_memories": 100, "ttl_days": 7}},
    )
    assert r.status_code == 200
    assert r.json()["invite_only"] is True and r.json()["description"] == "New"
    r = await api.patch("/api/projects/team-x", headers=RAY, json={"policies": {"ttl_days": None}})
    assert r.json()["policies"] == {"ttl_days": None, "max_memories": 100, "default_weight": None}
    # Invite-only keeps existing members.
    assert (await api.get("/api/projects/team-x", headers=ALICE)).status_code == 200


async def test_members_and_outsiders_cannot_manage(api):
    assert (await api.patch("/api/projects/team-x", headers=ALICE, json={"description": "x"})).status_code == 403
    assert (await api.post("/api/projects/team-x/archive", headers=BOB)).status_code == 403
    assert (await api.delete("/api/projects/team-x?force=true", headers=ALICE)).status_code == 403


# -- archive -----------------------------------------------------------------------


async def test_archive_is_read_only_and_reversible(api):
    r = await api.post("/api/projects/team-x/archive", headers=CAROL)
    assert r.json()["archived"] is True and r.json()["archived_by"] == "carol"
    assert (await api.patch("/api/projects/team-x", headers=CAROL, json={"description": "x"})).status_code == 409
    assert (await api.post("/api/projects/team-x/members", headers=CAROL, json={"user_id": "bob"})).status_code == 409
    assert (await api.get("/api/projects/team-x", headers=ALICE)).status_code == 200
    r = await api.post("/api/projects/team-x/unarchive", headers=CAROL)
    assert r.json()["archived"] is False
    assert (await api.post("/api/projects/team-x/members", headers=CAROL, json={"user_id": "bob"})).status_code == 201


# -- delete ------------------------------------------------------------------------


async def test_delete_asks_for_confirmation_with_the_memory_count(api, seeded):
    r = await api.delete("/api/projects/team-x", headers=CAROL)
    assert r.status_code == 409
    assert r.json()["detail"]["memory_count"] == 2

    r = await api.delete("/api/projects/team-x?force=true", headers=CAROL)
    assert r.status_code == 200 and r.json()["memories_deleted"] == 2
    assert (await api.get("/api/projects/team-x", headers=ADMIN)).status_code == 404
    assert (await api.post("/api/projects", headers=BOB, json={"name": "team-x"})).status_code == 409
    async with seeded() as s:
        live = (await s.execute(select(MemoryNode).where(MemoryNode.deleted_at.is_(None)))).scalars().all()
    assert live == []


async def test_empty_projects_delete_without_force(api):
    assert (await api.delete("/api/projects/secret", headers=RAY)).json()["memories_deleted"] == 0


# -- members -----------------------------------------------------------------------


async def test_add_change_and_remove_members(api):
    r = await api.post("/api/projects/team-x/members", headers=CAROL, json={"user_id": "bob", "role": "admin"})
    assert r.status_code == 201 and r.json()["joined_by"] == "carol"
    assert (await api.post("/api/projects/team-x/members", headers=CAROL, json={"user_id": "bob"})).status_code == 409
    r = await api.patch("/api/projects/team-x/members/bob", headers=CAROL, json={"role": "member"})
    assert r.json()["role"] == "member"
    assert (await api.delete("/api/projects/team-x/members/bob", headers=CAROL)).status_code == 204
    assert (await api.delete("/api/projects/team-x/members/bob", headers=CAROL)).status_code == 404
    listed = (await api.get("/api/projects/team-x/members", headers=ALICE)).json()
    assert {m["user_id"] for m in listed} == {"carol", "alice"}


async def test_last_admin_cannot_be_demoted_or_removed(api):
    assert (
        await api.patch("/api/projects/team-x/members/carol", headers=CAROL, json={"role": "member"})
    ).status_code == 409
    assert (await api.delete("/api/projects/team-x/members/carol", headers=CAROL)).status_code == 409


async def test_members_may_leave_but_not_remove_others(api):
    assert (await api.delete("/api/projects/team-x/members/carol", headers=ALICE)).status_code == 403
    assert (await api.delete("/api/projects/team-x/members/alice", headers=ALICE)).status_code == 204


async def test_member_input_is_validated(api):
    r = await api.post("/api/projects/team-x/members", headers=CAROL, json={"user_id": "x y", "role": "owner"})
    assert r.status_code == 422
