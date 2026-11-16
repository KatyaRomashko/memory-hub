"""Project lifecycle and policies (#671): archive, soft delete, policies, roles, listing."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from memoryhub_core.models.memory import MemoryNode
from memoryhub_core.models.project import Project, ProjectMembership
from memoryhub_core.models.schemas import MemoryNodeCreate, MemoryNodeUpdate, MemoryScope
from memoryhub_core.services.exceptions import (
    InvalidProjectPolicyError,
    LastAdminError,
    ProjectAlreadyExistsError,
    ProjectArchivedError,
    ProjectDeletedError,
    ProjectQuotaExceededError,
)
from memoryhub_core.services.memory import create_memory, delete_memory, update_memory
from memoryhub_core.services.project import (
    add_project_member,
    archive_project,
    create_project,
    delete_project,
    ensure_project_membership,
    get_project,
    get_project_members,
    get_projects_for_user,
    list_projects_for_tenant,
    list_tenant_projects,
    project_memory_count,
    remove_project_member,
    unarchive_project,
    update_member_role,
    update_project,
    validate_policies,
)

T = "default"


@pytest.fixture
async def team(async_session):
    async_session.add(Project(name="team-x", created_by="ray", tenant_id=T))
    await async_session.flush()
    async_session.add_all(
        [
            ProjectMembership(id=uuid.uuid4(), project_id="team-x", user_id="carol", role="admin", joined_by="ray"),
            ProjectMembership(id=uuid.uuid4(), project_id="team-x", user_id="alice", role="member", joined_by="ray"),
        ]
    )
    await async_session.commit()


def _memory(content="decision", scope_id="team-x", parent_id=None):
    now = datetime.now(UTC)
    mid = uuid.uuid4()
    return MemoryNode(
        id=mid,
        logical_id=mid,
        content=content,
        stub=content,
        scope="project",
        scope_id=scope_id,
        owner_id="alice",
        tenant_id=T,
        status="active",
        weight=0.7,
        is_current=True,
        version=1,
        storage_type="inline",
        parent_id=parent_id,
        created_at=now,
        updated_at=now,
    )


def _project_write(content="new decision", **extra) -> MemoryNodeCreate:
    return MemoryNodeCreate(content=content, scope=MemoryScope.PROJECT, scope_id="team-x", owner_id="alice", **extra)


# -- archive -------------------------------------------------------------------


async def test_archived_project_refuses_writes_and_joins_but_stays_readable(async_session, team):
    await archive_project(async_session, "team-x", by="carol")
    with pytest.raises(ProjectArchivedError, match="read-only"):
        await ensure_project_membership(async_session, "team-x", "alice", T)  # member
    with pytest.raises(ProjectArchivedError):
        await ensure_project_membership(async_session, "team-x", "dave", T)  # would auto-enroll
    with pytest.raises(ProjectArchivedError):
        await add_project_member(async_session, "team-x", "dave", "carol")
    assert "team-x" in await get_projects_for_user(async_session, "alice")


async def test_archived_project_memories_cannot_be_created_changed_or_deleted(async_session, team, embedding_service):
    existing = _memory()
    async_session.add(existing)
    await async_session.commit()
    await archive_project(async_session, "team-x", by="carol")
    with pytest.raises(ProjectArchivedError):
        await create_memory(_project_write(), async_session, embedding_service, tenant_id=T, skip_curation=True)
    with pytest.raises(ProjectArchivedError):
        await update_memory(existing.id, MemoryNodeUpdate(content="changed"), async_session, embedding_service)
    with pytest.raises(ProjectArchivedError):
        await delete_memory(existing.id, async_session)


async def test_unarchive_restores_writes(async_session, team, embedding_service):
    await archive_project(async_session, "team-x", by="carol")
    project = await unarchive_project(async_session, "team-x")
    assert project.archived_at is None and project.archived_by is None
    memory, _ = await create_memory(_project_write(), async_session, embedding_service, tenant_id=T, skip_curation=True)
    assert memory is not None


# -- delete --------------------------------------------------------------------


async def test_delete_soft_deletes_project_and_every_memory(async_session, team):
    root = _memory()
    async_session.add(root)
    await async_session.flush()
    async_session.add(_memory("branch", parent_id=root.id))
    async_session.add(_memory("other project", scope_id="other"))
    await async_session.commit()
    assert await project_memory_count(async_session, "team-x", T) == 2

    result = await delete_project(async_session, "team-x", T, by="ray")
    assert result["memories_deleted"] == 2
    assert await project_memory_count(async_session, "team-x", T) == 0
    assert await get_project(async_session, "team-x") is None
    # Members lose access at once; the row stays for audit.
    assert "team-x" not in await get_projects_for_user(async_session, "alice")
    assert (await get_project(async_session, "team-x", include_deleted=True)).deleted_by == "ray"


async def test_deleted_project_names_are_never_reused(async_session, team):
    await delete_project(async_session, "team-x", T, by="ray")
    with pytest.raises(ProjectAlreadyExistsError):
        await create_project(async_session, "team-x", T, "ray")
    with pytest.raises(ProjectDeletedError):
        await ensure_project_membership(async_session, "team-x", "dave", T)


async def test_deleted_projects_are_left_out_of_lists(async_session, team):
    await delete_project(async_session, "team-x", T, by="ray")
    assert await list_projects_for_tenant(async_session, T, user_id="alice", include_all_open=True) == []
    assert await list_tenant_projects(async_session, T) == []


# -- policies ------------------------------------------------------------------


def test_policies_are_validated():
    assert validate_policies({"ttl_days": 30, "max_memories": 5, "default_weight": 1}) == {
        "ttl_days": 30,
        "max_memories": 5,
        "default_weight": 1.0,
    }
    assert validate_policies({"ttl_days": None}) == {}
    for bad in ({"ttl_days": 0}, {"max_memories": -1}, {"default_weight": 1.2}, {"ttl_days": True}, {"color": "red"}):
        with pytest.raises(InvalidProjectPolicyError):
            validate_policies(bad)


async def test_update_merges_and_clears_policies_and_keeps_members(async_session, team):
    await update_project(async_session, "team-x", policies={"ttl_days": 30, "max_memories": 10})
    project = await update_project(
        async_session, "team-x", description="Team X", invite_only=True, policies={"ttl_days": None}
    )
    assert project.policies == {"max_memories": 10}
    assert project.description == "Team X" and project.invite_only is True
    assert {m["user_id"] for m in await get_project_members(async_session, "team-x")} == {"carol", "alice"}


async def test_quota_caps_new_root_memories(async_session, team, embedding_service):
    await update_project(async_session, "team-x", policies={"max_memories": 1})
    root = _memory()
    async_session.add(root)
    await async_session.commit()
    with pytest.raises(ProjectQuotaExceededError, match="limit of 1"):
        await create_memory(_project_write(), async_session, embedding_service, tenant_id=T, skip_curation=True)
    # Branches do not count against the quota.
    branch, _ = await create_memory(
        _project_write("why", parent_id=root.id, branch_type="rationale"),
        async_session,
        embedding_service,
        tenant_id=T,
        skip_curation=True,
    )
    assert branch is not None


async def test_default_weight_and_ttl_apply_when_the_caller_sets_none(async_session, team, embedding_service):
    await update_project(async_session, "team-x", policies={"default_weight": 0.3, "ttl_days": 10})
    memory, _ = await create_memory(_project_write(), async_session, embedding_service, tenant_id=T, skip_curation=True)
    assert memory.weight == pytest.approx(0.3)
    expected = datetime.now(UTC) + timedelta(days=10)
    assert abs((memory.relevant_until.replace(tzinfo=UTC) - expected).total_seconds()) < 60

    explicit, _ = await create_memory(
        _project_write("explicit", weight=0.9, relevant_until=datetime(2030, 1, 1, tzinfo=UTC)),
        async_session,
        embedding_service,
        tenant_id=T,
        skip_curation=True,
    )
    assert explicit.weight == pytest.approx(0.9)
    assert explicit.relevant_until.year == 2030


async def test_project_writes_without_a_project_row_are_not_governed(async_session, embedding_service):
    # Project isolation off: project memories may exist without a projects row.
    memory, _ = await create_memory(
        MemoryNodeCreate(content="loose", scope=MemoryScope.PROJECT, scope_id="no-row", owner_id="alice"),
        async_session,
        embedding_service,
        tenant_id=T,
        skip_curation=True,
    )
    assert memory is not None and memory.weight == pytest.approx(0.7)


# -- members -------------------------------------------------------------------


async def test_roles_change_but_the_last_admin_stays(async_session, team):
    assert await update_member_role(async_session, "team-x", "alice", "admin") is True
    assert await update_member_role(async_session, "team-x", "alice", "admin") is False
    assert await update_member_role(async_session, "team-x", "carol", "member") is True
    with pytest.raises(LastAdminError):
        await update_member_role(async_session, "team-x", "alice", "member")
    with pytest.raises(LastAdminError):
        await remove_project_member(async_session, "team-x", "alice")


async def test_members_show_who_added_them(async_session, team):
    members = {m["user_id"]: m for m in await get_project_members(async_session, "team-x")}
    assert members["alice"]["joined_by"] == "ray" and members["carol"]["role"] == "admin"


# -- tenant listing --------------------------------------------------------------


async def test_tenant_listing_counts_members_and_live_memories(async_session, team):
    async_session.add(Project(name="secret", created_by="ray", tenant_id=T, invite_only=True))
    async_session.add(Project(name="elsewhere", created_by="ray", tenant_id="other"))
    gone = _memory("gone")
    gone.deleted_at = datetime.now(UTC)
    async_session.add_all([_memory("a"), _memory("b"), gone])
    await async_session.commit()

    rows = {r["name"]: r for r in await list_tenant_projects(async_session, T)}
    assert set(rows) == {"team-x", "secret"}  # invite-only included, other tenant not
    assert rows["team-x"]["member_count"] == 2
    assert rows["team-x"]["memory_count"] == 2


async def test_tenant_listing_filters_by_archive_state(async_session, team):
    async_session.add(Project(name="old", created_by="ray", tenant_id=T))
    await async_session.commit()
    await archive_project(async_session, "old", by="ray")
    assert [r["name"] for r in await list_tenant_projects(async_session, T, archived=True)] == ["old"]
    assert [r["name"] for r in await list_tenant_projects(async_session, T, archived=False)] == ["team-x"]
