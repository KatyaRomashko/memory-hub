"""Project management API for the dashboard (#671).

Self-service: any signed-in user may create a project and becomes its first
admin. A project is managed (settings, members, archive, delete) by a
platform admin, its creator, or one of its project admins. Any member may
leave a project, as long as an admin remains.

Visibility follows enrollment: open projects are listed for everyone in the
tenant; invite-only projects only for their members, their creator and
platform admins.
Another tenant's projects, and invite-only projects the caller cannot see,
answer 404 rather than 403 so their names do not leak.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from memoryhub_core.models.project import Project, ProjectMembership
from memoryhub_core.services import project as svc
from memoryhub_core.services.exceptions import (
    InvalidProjectPolicyError,
    LastAdminError,
    MembershipNotFoundError,
    ProjectAlreadyExistsError,
    ProjectArchivedError,
    ProjectNotFoundError,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.caller import Caller, CallerDep
from src.config import Settings, get_settings
from src.database import get_db
from src.schemas import (
    PROJECT_NAME_PATTERN,
    USER_ID_PATTERN,
    CreateProjectRequest,
    ProjectDeletedResponse,
    ProjectDetailResponse,
    ProjectListResponse,
    ProjectMemberInput,
    ProjectMemberResponse,
    ProjectResponse,
    UpdateMemberRequest,
    UpdateProjectRequest,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])

DbDep = Annotated[AsyncSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
NameParam = Annotated[str, Path(min_length=1, max_length=63, pattern=PROJECT_NAME_PATTERN)]
UserParam = Annotated[str, Path(min_length=1, max_length=255, pattern=USER_ID_PATTERN)]


# ---------------------------------------------------------------------------
# Access helpers
# ---------------------------------------------------------------------------


async def _roles(session: AsyncSession, username: str) -> dict[str, str]:
    """The caller's role in each project they belong to."""
    rows = await session.execute(
        select(ProjectMembership.project_id, ProjectMembership.role).where(ProjectMembership.user_id == username)
    )
    return {project_id: role for project_id, role in rows.all()}


def _can_see(p: dict | Project, caller: Caller, role: str | None) -> bool:
    invite_only = p["invite_only"] if isinstance(p, dict) else p.invite_only
    return not invite_only or _can_manage(p, caller, role) or role is not None


def _can_manage(p: dict | Project, caller: Caller, role: str | None) -> bool:
    created_by = p["created_by"] if isinstance(p, dict) else p.created_by
    return caller.is_admin or role == "admin" or created_by == caller.username


def _view(summary: dict, caller: Caller, role: str | None) -> dict:
    manage = _can_manage(summary, caller, role)
    return {**summary, "my_role": role, "can_manage": manage, "can_delete": manage}


async def _visible(session: AsyncSession, name: str, settings: Settings, caller: Caller) -> tuple[Project, str | None]:
    """The project and the caller's role in it, or 404."""
    project = await svc.get_project(session, name)
    if project is None or project.tenant_id != settings.ui_tenant_id:
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found.")
    role = (await _roles(session, caller.username)).get(name)
    if not _can_see(project, caller, role):
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found.")
    return project, role


async def _manageable(
    session: AsyncSession, name: str, settings: Settings, caller: Caller
) -> tuple[Project, str | None]:
    project, role = await _visible(session, name, settings, caller)
    if not _can_manage(project, caller, role):
        raise HTTPException(
            status_code=403, detail="Only the project's admins, its creator or a platform admin can do this."
        )
    return project, role


async def _summary(session: AsyncSession, project: Project, tenant_id: str) -> dict:
    memories = await svc.memory_counts(session, tenant_id, [project.name])
    members = await svc.member_counts(session, [project.name])
    return svc.project_summary(
        project, memory_count=memories.get(project.name, 0), member_count=members.get(project.name, 0)
    )


async def _detail(session: AsyncSession, project: Project, settings: Settings, caller: Caller) -> dict:
    role = (await _roles(session, caller.username)).get(project.name)
    summary = await _summary(session, project, settings.ui_tenant_id)
    members = await svc.get_project_members(session, project.name)
    return {**_view(summary, caller, role), "members": members}


def _archived_conflict(name: str) -> HTTPException:
    return HTTPException(status_code=409, detail=f"Project '{name}' is archived. Unarchive it first.")


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


@router.get("", response_model=ProjectListResponse)
async def list_projects(
    session: DbDep,
    settings: SettingsDep,
    caller: CallerDep,
    archived: Annotated[bool | None, Query(description="true: archived only; false: active only")] = None,
    q: Annotated[str | None, Query(max_length=100, description="Filter by name or description")] = None,
    sort: Literal["name", "created_at", "-created_at"] = "name",
):
    roles = await _roles(session, caller.username)
    rows = await svc.list_tenant_projects(session, settings.ui_tenant_id, archived=archived)
    items = [_view(p, caller, roles.get(p["name"])) for p in rows if _can_see(p, caller, roles.get(p["name"]))]
    if q:
        needle = q.lower()
        items = [p for p in items if needle in p["name"] or needle in (p["description"] or "").lower()]
    if sort != "name":
        items.sort(key=lambda p: p["created_at"] or "", reverse=sort.startswith("-"))
    return {"items": items, "total": len(items), "can_create": True}


@router.post("", response_model=ProjectDetailResponse, status_code=201)
async def create_project(body: CreateProjectRequest, session: DbDep, settings: SettingsDep, caller: CallerDep):
    try:
        project = await svc.create_project(
            session,
            body.name,
            settings.ui_tenant_id,
            caller.username,
            description=body.description,
            invite_only=body.invite_only,
        )
        if body.policies is not None:
            await svc.update_project(session, body.name, policies=body.policies.model_dump(exclude_none=True))
        # The creator is already admin; later entries for the same user win.
        extra = {m.user_id: m.role for m in body.members if m.user_id != caller.username}
        for user_id, role in extra.items():
            await svc.add_project_member(session, body.name, user_id, caller.username, role=role)
    except ProjectAlreadyExistsError:
        raise HTTPException(status_code=409, detail=f"Project '{body.name}' already exists.") from None
    except InvalidProjectPolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    detail = await _detail(session, project, settings, caller)
    await session.commit()
    return detail


@router.get("/{name}", response_model=ProjectDetailResponse)
async def get_project(name: NameParam, session: DbDep, settings: SettingsDep, caller: CallerDep):
    project, _ = await _visible(session, name, settings, caller)
    return await _detail(session, project, settings, caller)


@router.patch("/{name}", response_model=ProjectResponse)
async def update_project(
    name: NameParam, body: UpdateProjectRequest, session: DbDep, settings: SettingsDep, caller: CallerDep
):
    project, role = await _manageable(session, name, settings, caller)
    if project.archived_at is not None:
        raise _archived_conflict(name)
    sent = body.model_dump(exclude_unset=True)
    policies = None
    if "policies" in sent and body.policies is not None:
        # Only the policy fields that were sent; an explicit null clears one.
        policies = body.policies.model_dump(exclude_unset=True)
    try:
        project = await svc.update_project(
            session,
            name,
            description=("" if sent.get("description") is None else sent["description"])
            if "description" in sent
            else None,
            invite_only=sent.get("invite_only"),
            policies=policies,
        )
    except InvalidProjectPolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    view = _view(await _summary(session, project, settings.ui_tenant_id), caller, role)
    await session.commit()
    return view


@router.post("/{name}/archive", response_model=ProjectResponse)
async def archive_project(name: NameParam, session: DbDep, settings: SettingsDep, caller: CallerDep):
    _, role = await _manageable(session, name, settings, caller)
    project = await svc.archive_project(session, name, by=caller.username)
    view = _view(await _summary(session, project, settings.ui_tenant_id), caller, role)
    await session.commit()
    return view


@router.post("/{name}/unarchive", response_model=ProjectResponse)
async def unarchive_project(name: NameParam, session: DbDep, settings: SettingsDep, caller: CallerDep):
    _, role = await _manageable(session, name, settings, caller)
    project = await svc.unarchive_project(session, name)
    view = _view(await _summary(session, project, settings.ui_tenant_id), caller, role)
    await session.commit()
    return view


@router.delete("/{name}", response_model=ProjectDeletedResponse)
async def delete_project(
    name: NameParam,
    session: DbDep,
    settings: SettingsDep,
    caller: CallerDep,
    force: Annotated[bool, Query(description="Confirm deleting a project that still holds memories")] = False,
):
    await _manageable(session, name, settings, caller)
    count = await svc.project_memory_count(session, name, settings.ui_tenant_id)
    if count and not force:
        # The client shows this count in its confirmation, then retries with force=true.
        return JSONResponse(
            status_code=409,
            content={
                "detail": {
                    "message": f"Project '{name}' holds {count} memories. Deleting it deletes them too.",
                    "memory_count": count,
                }
            },
        )
    result = await svc.delete_project(session, name, settings.ui_tenant_id, by=caller.username)
    await session.commit()
    return result


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


@router.get("/{name}/members", response_model=list[ProjectMemberResponse])
async def list_members(name: NameParam, session: DbDep, settings: SettingsDep, caller: CallerDep):
    await _visible(session, name, settings, caller)
    return await svc.get_project_members(session, name)


@router.post("/{name}/members", response_model=ProjectMemberResponse, status_code=201)
async def add_member(
    name: NameParam, body: ProjectMemberInput, session: DbDep, settings: SettingsDep, caller: CallerDep
):
    await _manageable(session, name, settings, caller)
    try:
        added = await svc.add_project_member(session, name, body.user_id, caller.username, role=body.role)
    except ProjectArchivedError:
        raise _archived_conflict(name) from None
    if not added:
        raise HTTPException(status_code=409, detail=f"'{body.user_id}' is already a member of '{name}'.")
    members = await svc.get_project_members(session, name)
    await session.commit()
    return next(m for m in members if m["user_id"] == body.user_id)


@router.patch("/{name}/members/{user_id}", response_model=ProjectMemberResponse)
async def update_member(
    name: NameParam,
    user_id: UserParam,
    body: UpdateMemberRequest,
    session: DbDep,
    settings: SettingsDep,
    caller: CallerDep,
):
    await _manageable(session, name, settings, caller)
    try:
        await svc.update_member_role(session, name, user_id, body.role)
    except MembershipNotFoundError:
        raise HTTPException(status_code=404, detail=f"'{user_id}' is not a member of '{name}'.") from None
    except LastAdminError:
        raise HTTPException(status_code=409, detail=f"'{user_id}' is the last admin of '{name}'.") from None
    members = await svc.get_project_members(session, name)
    await session.commit()
    return next(m for m in members if m["user_id"] == user_id)


@router.delete("/{name}/members/{user_id}", status_code=204)
async def remove_member(name: NameParam, user_id: UserParam, session: DbDep, settings: SettingsDep, caller: CallerDep):
    # Anyone may leave; removing someone else takes manage rights.
    if user_id == caller.username:
        await _visible(session, name, settings, caller)
    else:
        await _manageable(session, name, settings, caller)
    try:
        await svc.remove_project_member(session, name, user_id)
    except ProjectNotFoundError:
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found.") from None
    except MembershipNotFoundError:
        raise HTTPException(status_code=404, detail=f"'{user_id}' is not a member of '{name}'.") from None
    except LastAdminError:
        raise HTTPException(
            status_code=409, detail=f"'{user_id}' is the last admin of '{name}'. Make someone else admin first."
        ) from None
    await session.commit()
