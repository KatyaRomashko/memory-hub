"""Project membership resolution and auto-enrollment for RBAC.

Provides helpers that resolve which projects a user has access to,
based on project_memberships rows. Used by the authz layer and search
filters to include project-scoped memories in results.

Also provides auto-enrollment: when a user writes to a project they
are not yet a member of, ``ensure_project_membership`` creates the
project (if needed) and the membership row automatically — unless the
project is invite-only, archived or deleted.

Lifecycle and policies (#671): projects can be archived (read-only),
unarchived and soft-deleted together with their memories, and carry
optional policies (``ttl_days``, ``max_memories``, ``default_weight``)
that the memory write path applies. See docs/design/projects-lifecycle.md.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from memoryhub_core.models.memory import MemoryNode
from memoryhub_core.models.project import Project, ProjectMembership
from memoryhub_core.services.exceptions import (
    InvalidProjectPolicyError,
    LastAdminError,
    MembershipNotFoundError,
    ProjectAlreadyExistsError,
    ProjectArchivedError,
    ProjectDeletedError,
    ProjectInviteOnlyError,
    ProjectNotFoundError,
    ProjectQuotaExceededError,
)

logger = logging.getLogger(__name__)


async def get_projects_for_user(
    session: AsyncSession,
    user_id: str,
) -> set[str]:
    """Return the set of project IDs the user is a member of.

    Used by the search and authz layers to filter project-scoped memories.
    A user sees only memories whose scope_id matches one of their projects.
    """
    stmt = (
        select(ProjectMembership.project_id)
        .join(Project, ProjectMembership.project_id == Project.name)
        .where(ProjectMembership.user_id == user_id, Project.deleted_at.is_(None))
    )
    result = await session.execute(stmt)
    return {row[0] for row in result.all()}


async def get_projects_for_user_in_tenant(
    session: AsyncSession,
    user_id: str,
    tenant_id: str,
) -> set[str]:
    """Return the user's project memberships within one tenant."""
    stmt = (
        select(ProjectMembership.project_id)
        .join(Project, ProjectMembership.project_id == Project.name)
        .where(
            ProjectMembership.user_id == user_id,
            Project.tenant_id == tenant_id,
            Project.deleted_at.is_(None),
        )
    )
    result = await session.execute(stmt)
    return {row[0] for row in result.all()}

async def assert_project_membership(
    session: AsyncSession,
    user_id: str,
    project_id: str,
    tenant_id: str
) -> None:
    """Assert that a user is a member of a project, raising an error if not.

    Raises MembershipNotFoundError if the user is not a member of the project_id in tenant_id.
    """
    membership = select(ProjectMembership.id).join(
        Project, ProjectMembership.project_id == Project.name
    ).where(
        ProjectMembership.user_id == user_id,
        ProjectMembership.project_id == project_id,
        Project.name == project_id,
        Project.tenant_id == tenant_id,
    )
    if not (await session.execute(membership)).first():
        raise MembershipNotFoundError(project_id, user_id)


async def get_project(
    session: AsyncSession,
    project_name: str,
    include_deleted: bool = False,
) -> Project | None:
    """Fetch a project by name, or None if it doesn't exist (or was deleted)."""
    stmt = select(Project).where(Project.name == project_name)
    if not include_deleted:
        stmt = stmt.where(Project.deleted_at.is_(None))
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def ensure_project_membership(
    session: AsyncSession,
    project_id: str,
    user_id: str,
    tenant_id: str,
    description: str | None = None,
) -> tuple[set[str], bool]:
    """Ensure the user is a member of the project, auto-enrolling if allowed.

    Returns:
        A tuple of (project_ids, was_auto_enrolled) where project_ids is
        the user's full set of project memberships after any enrollment.

    Raises:
        ProjectInviteOnlyError: If the project exists and is invite-only.
        ProjectArchivedError: If the project is archived (read-only), for
            members too.
        ProjectDeletedError: If the project was deleted (its name is never
            reused).
    """
    project = await get_project(session, project_id, include_deleted=True)
    if project is not None:
        if project.deleted_at is not None:
            raise ProjectDeletedError(project_id)
        if project.archived_at is not None:
            raise ProjectArchivedError(project_id)

    # Fast path: user is already a member.
    project_ids = await get_projects_for_user(session, user_id)
    if project_id in project_ids:
        return project_ids, False

    if project is not None and project.invite_only:
        raise ProjectInviteOnlyError(project_id)

    if project is None:
        project = Project(
            name=project_id,
            description=description,
            created_by=user_id,
            tenant_id=tenant_id,
        )
        session.add(project)
        try:
            await session.flush()
        except IntegrityError:
            # Concurrent creation — project was created by another session.
            await session.rollback()
            project = await get_project(session, project_id)
            if project is not None and project.invite_only:
                raise ProjectInviteOnlyError(project_id) from None

    # Create the membership.
    membership = ProjectMembership(
        id=uuid.uuid4(),
        project_id=project_id,
        user_id=user_id,
        joined_by="auto-enroll",
    )
    session.add(membership)
    try:
        await session.flush()
    except IntegrityError:
        # Concurrent enrollment — membership was created by another session.
        await session.rollback()

    logger.info(
        "Auto-enrolled user %r in project %r", user_id, project_id,
    )

    # Re-query to get the authoritative membership set.
    project_ids = await get_projects_for_user(session, user_id)
    return project_ids, True


async def create_project(
    session: AsyncSession,
    name: str,
    tenant_id: str,
    created_by: str,
    description: str | None = None,
    invite_only: bool = False,
) -> Project:
    """Create a new project.

    Raises:
        ProjectAlreadyExistsError: If a project with this name already exists
            (a deleted one included: names are never reused).
    """
    existing = await get_project(session, name, include_deleted=True)
    if existing is not None:
        raise ProjectAlreadyExistsError(name)

    project = Project(
        name=name,
        description=description,
        invite_only=invite_only,
        tenant_id=tenant_id,
        created_by=created_by,
    )
    session.add(project)

    # Auto-enroll the creator as admin.
    membership = ProjectMembership(
        id=uuid.uuid4(),
        project_id=name,
        user_id=created_by,
        role="admin",
        joined_by=created_by,
    )
    session.add(membership)

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ProjectAlreadyExistsError(name) from exc

    logger.info("Created project %r by %r", name, created_by)
    return project


async def get_project_members(
    session: AsyncSession,
    project_name: str,
) -> list[dict]:
    """Return all members of a project with their roles.

    Raises:
        ProjectNotFoundError: If the project does not exist.
    """
    project = await get_project(session, project_name)
    if project is None:
        raise ProjectNotFoundError(project_name)

    stmt = (
        select(ProjectMembership)
        .where(ProjectMembership.project_id == project_name)
        .order_by(ProjectMembership.joined_at)
    )
    result = await session.execute(stmt)
    members = result.scalars().all()

    return [
        {
            "user_id": m.user_id,
            "role": m.role,
            "joined_at": m.joined_at.isoformat() if m.joined_at else None,
            "joined_by": m.joined_by,
        }
        for m in members
    ]


async def add_project_member(
    session: AsyncSession,
    project_name: str,
    user_id: str,
    added_by: str,
    role: str = "member",
) -> bool:
    """Add a user to a project.

    Returns True if the user was newly added, False if already a member.

    Raises:
        ProjectNotFoundError: If the project does not exist.
        ProjectArchivedError: If the project is archived.
    """
    project = await get_project(session, project_name)
    if project is None:
        raise ProjectNotFoundError(project_name)
    if project.archived_at is not None:
        # Archived projects take no new members (#671).
        raise ProjectArchivedError(project_name)

    membership = ProjectMembership(
        id=uuid.uuid4(),
        project_id=project_name,
        user_id=user_id,
        role=role,
        joined_by=added_by,
    )
    session.add(membership)
    try:
        await session.flush()
    except IntegrityError:
        # Already a member.
        await session.rollback()
        logger.info("User %r already a member of project %r", user_id, project_name)
        return False

    logger.info(
        "Added user %r to project %r as %r (by %r)",
        user_id, project_name, role, added_by,
    )
    return True


async def remove_project_member(
    session: AsyncSession,
    project_name: str,
    user_id: str,
) -> None:
    """Remove a user from a project.

    Raises:
        ProjectNotFoundError: If the project does not exist.
        MembershipNotFoundError: If the user is not a member.
        LastAdminError: If removing this user would leave no admins.
    """
    project = await get_project(session, project_name)
    if project is None:
        raise ProjectNotFoundError(project_name)

    stmt = (
        select(ProjectMembership)
        .where(
            ProjectMembership.project_id == project_name,
            ProjectMembership.user_id == user_id,
        )
    )
    result = await session.execute(stmt)
    membership = result.scalar_one_or_none()

    if membership is None:
        raise MembershipNotFoundError(project_name, user_id)

    # Guard: don't remove the last admin. The row lock keeps two concurrent
    # removals from both seeing a second admin.
    if membership.role == "admin":
        await lock_project(session, project_name)
        admin_count_stmt = (
            select(func.count())
            .select_from(ProjectMembership)
            .where(
                ProjectMembership.project_id == project_name,
                ProjectMembership.role == "admin",
            )
        )
        admin_count_result = await session.execute(admin_count_stmt)
        if admin_count_result.scalar_one() <= 1:
            raise LastAdminError(project_name)

    await session.delete(membership)
    await session.flush()

    logger.info("Removed user %r from project %r", user_id, project_name)


async def list_projects_for_tenant(
    session: AsyncSession,
    tenant_id: str,
    user_id: str | None = None,
    include_all_open: bool = False,
) -> list[dict]:
    """List projects visible to a user within a tenant.

    Args:
        tenant_id: Tenant boundary for project visibility.
        user_id: The requesting user. Required when include_all_open=False.
        include_all_open: If True, return all open projects in the tenant
            (with an ``is_member`` flag), plus any invite-only projects the
            user belongs to. If False, return only the user's projects.

    Returns:
        List of project dicts with keys: name, description, invite_only,
        created_at, created_by, is_member.
    """
    if include_all_open:
        # All open projects + user's invite-only projects.
        stmt = select(Project).where(Project.tenant_id == tenant_id, Project.deleted_at.is_(None))
        result = await session.execute(stmt)
        projects = result.scalars().all()

        user_project_ids: set[str] = set()
        if user_id:
            user_project_ids = await get_projects_for_user(session, user_id)

        project_names = [p.name for p in projects]
        counts = await memory_counts(session, tenant_id, project_names)

        out = []
        for p in projects:
            is_member = p.name in user_project_ids
            # Skip invite-only projects the user is not a member of.
            if p.invite_only and not is_member:
                continue
            out.append({
                "name": p.name,
                "description": p.description,
                "invite_only": p.invite_only,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "created_by": p.created_by,
                "is_member": is_member,
                "memory_count": counts.get(p.name, 0),
                **_lifecycle(p),
            })
        return out

    # Default: only the user's projects.
    if not user_id:
        return []

    user_project_ids = await get_projects_for_user(session, user_id)
    if not user_project_ids:
        return []

    stmt = (
        select(Project)
        .where(Project.name.in_(user_project_ids))
        .where(Project.tenant_id == tenant_id)
    )
    result = await session.execute(stmt)
    projects = result.scalars().all()
    project_names = [p.name for p in projects]
    counts = await memory_counts(session, tenant_id, project_names)

    return [
        {
            "name": p.name,
            "description": p.description,
            "invite_only": p.invite_only,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "created_by": p.created_by,
            "is_member": True,
            "memory_count": counts.get(p.name, 0),
            **_lifecycle(p),
        }
        for p in projects
    ]


async def memory_counts(
    session: AsyncSession,
    tenant_id: str,
    project_names: list[str],
) -> dict[str, int]:
    """Count current project-scoped memories per project in one query."""
    if not project_names:
        return {}
    stmt = (
        select(MemoryNode.scope_id, func.count(MemoryNode.id))
        .where(MemoryNode.scope == "project")
        .where(MemoryNode.tenant_id == tenant_id)
        .where(MemoryNode.is_current.is_(True))
        .where(MemoryNode.deleted_at.is_(None))
        .where(MemoryNode.scope_id.in_(project_names))
        .group_by(MemoryNode.scope_id)
    )
    result = await session.execute(stmt)
    return dict(result.all())


def _lifecycle(p: Project) -> dict:
    return {
        "archived": p.archived_at is not None,
        "archived_at": p.archived_at.isoformat() if p.archived_at else None,
        "policies": dict(p.policies or {}),
    }


async def member_counts(session: AsyncSession, project_names: list[str]) -> dict[str, int]:
    """Members per project in one query."""
    if not project_names:
        return {}
    stmt = (
        select(ProjectMembership.project_id, func.count(ProjectMembership.id))
        .where(ProjectMembership.project_id.in_(project_names))
        .group_by(ProjectMembership.project_id)
    )
    return dict((await session.execute(stmt)).all())


def project_summary(p: Project, *, memory_count: int = 0, member_count: int = 0) -> dict:
    """The project fields the management API returns."""
    return {
        "name": p.name,
        "description": p.description,
        "invite_only": p.invite_only,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "created_by": p.created_by,
        "archived": p.archived_at is not None,
        "archived_at": p.archived_at.isoformat() if p.archived_at else None,
        "archived_by": p.archived_by,
        "policies": dict(p.policies or {}),
        "member_count": member_count,
        "memory_count": memory_count,
    }


async def list_tenant_projects(
    session: AsyncSession,
    tenant_id: str,
    *,
    archived: bool | None = None,
) -> list[dict]:
    """Every live project in a tenant, invite-only ones included, with counts.

    For administrators: callers must check that the viewer may see every
    project. ``archived`` filters by archive state (``None``: both).
    """
    stmt = select(Project).where(Project.tenant_id == tenant_id, Project.deleted_at.is_(None))
    if archived is True:
        stmt = stmt.where(Project.archived_at.is_not(None))
    elif archived is False:
        stmt = stmt.where(Project.archived_at.is_(None))
    projects = (await session.execute(stmt.order_by(Project.name))).scalars().all()
    names = [p.name for p in projects]
    memories = await memory_counts(session, tenant_id, names)
    members = await member_counts(session, names)
    return [
        project_summary(p, memory_count=memories.get(p.name, 0), member_count=members.get(p.name, 0))
        for p in projects
    ]


# ---------------------------------------------------------------------------
# Lifecycle and policies (#671)
# ---------------------------------------------------------------------------

POLICY_KEYS = ("ttl_days", "max_memories", "default_weight")


def validate_policies(policies: dict | None) -> dict:
    """Return a clean policies dict; unknown keys and out-of-range values raise.

    A key set to ``None`` is dropped (the policy is cleared).
    """
    out: dict = {}
    for key, value in (policies or {}).items():
        if key not in POLICY_KEYS:
            raise InvalidProjectPolicyError(f"Unknown project policy '{key}'. Known: {', '.join(POLICY_KEYS)}")
        if value is None:
            continue
        if key == "ttl_days":
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3650:
                raise InvalidProjectPolicyError("ttl_days must be a whole number of days from 1 to 3650")
        elif key == "max_memories":
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10_000_000:
                raise InvalidProjectPolicyError("max_memories must be a whole number from 1 to 10,000,000")
        elif key == "default_weight":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
                raise InvalidProjectPolicyError("default_weight must be between 0.0 and 1.0")
            value = float(value)
        out[key] = value
    return out


async def lock_project(session: AsyncSession, name: str) -> Project | None:
    """Lock the project row until the transaction ends.

    Serialises check-then-write guards on one project (the last admin, the
    memory quota). PostgreSQL ``SELECT ... FOR UPDATE``; a no-op on SQLite.
    """
    return (await session.execute(select(Project).where(Project.name == name).with_for_update())).scalar_one_or_none()


async def assert_project_writable(session: AsyncSession, project_id: str) -> Project | None:
    """Raise if the project is archived or deleted; return it (or None when there is no project row).

    Deployments with project isolation off may hold project-scoped memories
    without a ``projects`` row; those writes are not governed by a lifecycle.
    """
    project = await get_project(session, project_id, include_deleted=True)
    if project is None:
        return None
    if project.deleted_at is not None:
        raise ProjectDeletedError(project_id)
    if project.archived_at is not None:
        raise ProjectArchivedError(project_id)
    return project


async def project_write_policies(
    session: AsyncSession,
    project_id: str,
    tenant_id: str,
    *,
    new_memory: bool = True,
) -> dict:
    """Policies for a write into a project, after the lifecycle checks.

    Enforces ``max_memories`` for new root memories (current, non-deleted root
    memories are counted; branches and new versions do not count) and returns
    the policies so the caller can apply ``ttl_days`` and ``default_weight``.
    """
    project = await assert_project_writable(session, project_id)
    if project is None:
        return {}
    policies = dict(project.policies or {})
    limit = policies.get("max_memories")
    if new_memory and limit:
        await lock_project(session, project_id)
        count = (
            await session.execute(
                select(func.count()).select_from(MemoryNode).where(
                    MemoryNode.tenant_id == tenant_id,
                    MemoryNode.scope == "project",
                    MemoryNode.scope_id == project_id,
                    MemoryNode.is_current.is_(True),
                    MemoryNode.deleted_at.is_(None),
                    MemoryNode.parent_id.is_(None),
                )
            )
        ).scalar_one()
        if count >= limit:
            raise ProjectQuotaExceededError(project_id, limit)
    return policies


def relevant_until_from_policies(policies: dict, now: datetime | None = None) -> datetime | None:
    """``ttl_days`` becomes the semantic expiry (``relevant_until``) of new memories."""
    days = (policies or {}).get("ttl_days")
    if not days:
        return None
    return (now or datetime.now(UTC)) + timedelta(days=int(days))


async def update_project(
    session: AsyncSession,
    name: str,
    *,
    description: str | None = None,
    invite_only: bool | None = None,
    policies: dict | None = None,
) -> Project:
    """Change description, enrollment or policies. The name never changes.

    ``policies`` is merged into the current ones; a key set to ``None`` is
    cleared. Turning a project invite-only keeps its members.
    """
    project = await get_project(session, name)
    if project is None:
        raise ProjectNotFoundError(name)
    if description is not None:
        project.description = description or None
    if invite_only is not None:
        project.invite_only = invite_only
    if policies is not None:
        merged = {**(project.policies or {}), **policies}
        project.policies = validate_policies(merged)
    await session.flush()
    return project


async def archive_project(session: AsyncSession, name: str, by: str) -> Project:
    """Make the project read-only. Members keep reading its memories."""
    project = await get_project(session, name)
    if project is None:
        raise ProjectNotFoundError(name)
    if project.archived_at is None:
        project.archived_at = datetime.now(UTC)
        project.archived_by = by
        await session.flush()
    return project


async def unarchive_project(session: AsyncSession, name: str) -> Project:
    """Make an archived project writable again."""
    project = await get_project(session, name)
    if project is None:
        raise ProjectNotFoundError(name)
    project.archived_at = None
    project.archived_by = None
    await session.flush()
    return project


async def project_memory_count(session: AsyncSession, name: str, tenant_id: str) -> int:
    """Live project memories, every version and branch included (what delete removes)."""
    return (
        await session.execute(
            select(func.count()).select_from(MemoryNode).where(
                MemoryNode.tenant_id == tenant_id,
                MemoryNode.scope == "project",
                MemoryNode.scope_id == name,
                MemoryNode.deleted_at.is_(None),
            )
        )
    ).scalar_one()


async def delete_project(session: AsyncSession, name: str, tenant_id: str, by: str) -> dict:
    """Soft-delete the project and every memory in it, in one transaction.

    Members lose access at once (deleted projects are left out of every
    membership lookup). The project row and memberships stay for audit and the
    name stays reserved. Returns how many memory rows were soft-deleted.
    """
    project = await get_project(session, name)
    if project is None or project.tenant_id != tenant_id:
        raise ProjectNotFoundError(name)
    now = datetime.now(UTC)
    result = await session.execute(
        update(MemoryNode)
        .where(
            MemoryNode.tenant_id == tenant_id,
            MemoryNode.scope == "project",
            MemoryNode.scope_id == name,
            MemoryNode.deleted_at.is_(None),
        )
        .values(deleted_at=now)
    )
    project.deleted_at = now
    project.deleted_by = by
    await session.flush()
    deleted = result.rowcount or 0
    logger.info("Deleted project %r by %r (%d memories)", name, by, deleted)
    return {"project": name, "memories_deleted": deleted, "deleted_at": now.isoformat()}


async def update_member_role(session: AsyncSession, name: str, user_id: str, role: str) -> bool:
    """Set a member's role (``member`` or ``admin``); False when it already was.

    Raises:
        ProjectNotFoundError, MembershipNotFoundError
        LastAdminError: demoting the only admin.
    """
    if role not in ("member", "admin"):
        raise ValueError("role must be 'member' or 'admin'")
    if await get_project(session, name) is None:
        raise ProjectNotFoundError(name)
    await lock_project(session, name)
    membership = (
        await session.execute(
            select(ProjectMembership).where(ProjectMembership.project_id == name, ProjectMembership.user_id == user_id)
        )
    ).scalar_one_or_none()
    if membership is None:
        raise MembershipNotFoundError(name, user_id)
    if membership.role == role:
        return False
    if membership.role == "admin":
        admins = (
            await session.execute(
                select(func.count())
                .select_from(ProjectMembership)
                .where(ProjectMembership.project_id == name, ProjectMembership.role == "admin")
            )
        ).scalar_one()
        if admins <= 1:
            raise LastAdminError(name)
    membership.role = role
    await session.flush()
    logger.info("Project %r: %r is now %s", name, user_id, role)
    return True
