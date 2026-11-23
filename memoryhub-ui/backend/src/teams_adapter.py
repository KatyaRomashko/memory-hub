"""Projects & teams (#720): teams, campaigns, project roles and self-service joining.

- Teams are dashboard groups of people (``memoryhub_ui_teams``), linked to
  projects for orientation. A team does not grant project access: access stays
  with project membership, which MCP and the BFF already enforce.
- Campaigns enroll projects. A person sees campaigns that enroll one of their
  projects, and, for projects they administer, active campaigns they may join.
  Enrolling or leaving is the project admin's call.
- Project roles are the ones the backend enforces: the project's creator
  (Owner), project admins (Project Manager) and members (Contributor).
- A person may join an open (not invite-only) project themselves, as MCP
  auto-enrollment already allows their agents to.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from memoryhub_core.models.campaign import Campaign, CampaignMembership
from memoryhub_core.models.memory import MemoryNode
from memoryhub_core.models.project import Project, ProjectMembership
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Column, DateTime, ForeignKey, String, Text, Uuid, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import declarative_base

from src.authorization import Access, AccessDep
from src.config import Settings, get_settings
from src.database import get_db
from src.human_adapters import _membership, _project, _record_human_audit

router = APIRouter()
DbDep = Annotated[AsyncSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
TeamBase = declarative_base()


class Team(TeamBase):
    __tablename__ = "memoryhub_ui_teams"
    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id = Column(String(255), nullable=False)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    created_by = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


class TeamMember(TeamBase):
    __tablename__ = "memoryhub_ui_team_members"
    team_id = Column(Uuid, ForeignKey("memoryhub_ui_teams.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(String(255), primary_key=True)
    role = Column(String(20), nullable=False, default="member")
    added_by = Column(String(255), nullable=False)
    added_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


class TeamProject(TeamBase):
    __tablename__ = "memoryhub_ui_team_projects"
    team_id = Column(Uuid, ForeignKey("memoryhub_ui_teams.id", ondelete="CASCADE"), primary_key=True)
    project_id = Column(String(255), primary_key=True)
    linked_by = Column(String(255), nullable=False)
    linked_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


class TeamBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    members: list[TeamMemberBody] = Field(default_factory=list, max_length=200)


class TeamPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)


class TeamMemberBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(min_length=1, max_length=255)
    role: Literal["lead", "member"] = "member"


TeamBody.model_rebuild()


# --- project roles --------------------------------------------------------------

PROJECT_ROLES = [
    {"role": "owner", "label": "Owner", "stored_as": "created_by",
     "permissions": ["read project memories", "write project memories", "leave the project"],
     "note": "The project's creator. Ownership is recorded, not transferable from the dashboard yet."},
    {"role": "manager", "label": "Project Manager", "stored_as": "admin",
     "permissions": ["read project memories", "write project memories", "review contradictions",
                     "enroll the project in campaigns", "add and remove members (with a platform admin)"],
     "note": "Membership role admin."},
    {"role": "contributor", "label": "Contributor", "stored_as": "member",
     "permissions": ["read project memories", "write project memories", "leave the project"],
     "note": "Membership role member."},
]


@router.get("/api/project-roles")
async def project_roles():
    """Project roles the backend enforces and what each one may do."""
    return {"items": PROJECT_ROLES}


@router.post("/api/projects/{name}/join")
async def join_project(name: str, db: DbDep, access: AccessDep):
    """Join an open project as a contributor. Invite-only projects are joined by invitation."""
    project = await _project(db, access, name, lock=True)
    if project.invite_only:
        raise HTTPException(status_code=403, detail="Invite-only project: ask a project admin to add you")
    if await _membership(db, access, name):
        raise HTTPException(status_code=409, detail="Already a member")
    db.add(ProjectMembership(project_id=name, user_id=access.principal.username, role="member",
                             joined_by=access.principal.username))
    _record_human_audit(db, access, "project.member_added", "project", access.principal.username,
                        metadata={"project_id": name, "user_id": access.principal.username, "role": "member",
                                  "action": "self_join"})
    await db.commit()
    return {"ok": True, "project": name, "role": "contributor"}


# --- teams --------------------------------------------------------------------------

def _known_person(settings: Settings, user_id: str) -> bool:
    """Teams are groups of people: dashboard users and admins, not agents."""
    return user_id in {name.strip() for raw in (settings.ui_allowed_users, settings.ui_admin_users)
                       for name in raw.split(",") if name.strip()}

async def _team_rows(db: AsyncSession, teams: list[Team]) -> list[dict]:
    ids = [t.id for t in teams]
    members: dict[uuid.UUID, list[dict]] = {i: [] for i in ids}
    projects: dict[uuid.UUID, list[str]] = {i: [] for i in ids}
    if ids:
        for m in (await db.execute(select(TeamMember).where(TeamMember.team_id.in_(ids)).order_by(
                TeamMember.role, TeamMember.user_id))).scalars():
            members[m.team_id].append({"user_id": m.user_id, "role": m.role, "added_at": m.added_at.isoformat()})
        for p in (await db.execute(select(TeamProject).where(TeamProject.team_id.in_(ids)).order_by(
                TeamProject.project_id))).scalars():
            projects[p.team_id].append(p.project_id)
    return [{"id": str(t.id), "name": t.name, "description": t.description, "created_by": t.created_by,
             "created_at": t.created_at.isoformat(), "members": members[t.id], "projects": projects[t.id]}
            for t in teams]


async def _team(db: AsyncSession, access: Access, team_id: uuid.UUID, *, lock: bool = False) -> Team:
    stmt = select(Team).where(Team.id == team_id, Team.tenant_id == access.principal.tenant_id)
    team = (await db.execute(stmt.with_for_update() if lock else stmt)).scalar_one_or_none()
    if team is None:
        raise HTTPException(status_code=404, detail="Team not found")
    if access.principal.role != "admin" and await _team_role(db, team.id, access.principal.username) is None:
        raise HTTPException(status_code=404, detail="Team not found")
    return team


async def _team_role(db: AsyncSession, team_id: uuid.UUID, user: str) -> str | None:
    return (await db.execute(select(TeamMember.role).where(
        TeamMember.team_id == team_id, TeamMember.user_id == user))).scalar_one_or_none()


async def _may_manage(db: AsyncSession, access: Access, team: Team) -> bool:
    return access.principal.role == "admin" or await _team_role(db, team.id, access.principal.username) == "lead"


@router.get("/api/teams")
async def list_teams(db: DbDep, access: AccessDep):
    """Teams the person belongs to; every team of the tenant for a platform admin."""
    stmt = select(Team).where(Team.tenant_id == access.principal.tenant_id)
    if access.principal.role != "admin":
        stmt = stmt.where(Team.id.in_(select(TeamMember.team_id).where(
            TeamMember.user_id == access.principal.username)))
    items = await _team_rows(db, list((await db.execute(stmt.order_by(Team.name))).scalars()))
    people = {m["user_id"] for t in items for m in t["members"]}
    return {"items": items, "counts": {"teams": len(items), "people": len(people)}}


@router.get("/api/teams/{team_id}")
async def team_detail(team_id: uuid.UUID, db: DbDep, access: AccessDep):
    team = await _team(db, access, team_id)
    row = (await _team_rows(db, [team]))[0]
    row["can_manage"] = await _may_manage(db, access, team)
    row["my_role"] = await _team_role(db, team.id, access.principal.username)
    return row


@router.post("/api/teams", status_code=201)
async def create_team(body: TeamBody, db: DbDep, settings: SettingsDep, access: AccessDep):
    """Platform admins create teams and name their first members and lead."""
    if access.principal.role != "admin":
        raise HTTPException(status_code=403, detail="Team creation forbidden")
    tenant = access.principal.tenant_id
    for m in body.members:
        if not _known_person(settings, m.user_id):
            raise HTTPException(status_code=422, detail=f"Unknown person: {m.user_id}")
    team = Team(tenant_id=tenant, name=body.name.strip(), description=body.description,
                created_by=access.principal.username)
    db.add(team)
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(status_code=409, detail="A team with this name exists") from None
    for m in {m.user_id: m for m in body.members}.values():
        db.add(TeamMember(team_id=team.id, user_id=m.user_id, role=m.role, added_by=access.principal.username))
    _record_human_audit(db, access, "team.created", "team", access.principal.username,
                        metadata={"team_id": str(team.id), "name": team.name})
    await db.commit()
    return (await _team_rows(db, [team]))[0]


@router.patch("/api/teams/{team_id}")
async def update_team(team_id: uuid.UUID, body: TeamPatch, db: DbDep, access: AccessDep):
    team = await _team(db, access, team_id, lock=True)
    if not await _may_manage(db, access, team):
        raise HTTPException(status_code=403, detail="Team management forbidden")
    changes = body.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"]:
        team.name = changes["name"].strip()
    if "description" in changes:
        team.description = changes["description"]
    _record_human_audit(db, access, "team.updated", "team", access.principal.username,
                        metadata={"team_id": str(team.id), "fields": sorted(changes)})
    try:
        await db.commit()
    except IntegrityError:
        raise HTTPException(status_code=409, detail="A team with this name exists") from None
    return (await _team_rows(db, [team]))[0]


@router.delete("/api/teams/{team_id}", status_code=204)
async def delete_team(team_id: uuid.UUID, db: DbDep, access: AccessDep):
    if access.principal.role != "admin":
        raise HTTPException(status_code=403, detail="Team deletion forbidden")
    team = await _team(db, access, team_id, lock=True)
    _record_human_audit(db, access, "team.deleted", "team", access.principal.username,
                        metadata={"team_id": str(team.id), "name": team.name})
    await db.delete(team)
    await db.commit()


@router.post("/api/teams/{team_id}/members")
async def add_team_member(team_id: uuid.UUID, body: TeamMemberBody, db: DbDep, settings: SettingsDep,
                          access: AccessDep):
    """A platform admin or the team's lead adds people; only a platform admin names another lead."""
    team = await _team(db, access, team_id, lock=True)
    if not await _may_manage(db, access, team):
        raise HTTPException(status_code=403, detail="Team management forbidden")
    if body.role == "lead" and access.principal.role != "admin":
        raise HTTPException(status_code=403, detail="Only a platform admin names a team lead")
    if not _known_person(settings, body.user_id):
        raise HTTPException(status_code=422, detail="Unknown person")
    if await _team_role(db, team.id, body.user_id) is not None:
        raise HTTPException(status_code=409, detail="Already in the team")
    db.add(TeamMember(team_id=team.id, user_id=body.user_id, role=body.role, added_by=access.principal.username))
    _record_human_audit(db, access, "team.member_added", "team", access.principal.username,
                        metadata={"team_id": str(team.id), "user_id": body.user_id, "role": body.role})
    await db.commit()
    return {"ok": True}


@router.delete("/api/teams/{team_id}/members/{user_id}", status_code=204)
async def remove_team_member(team_id: uuid.UUID, user_id: str, db: DbDep, access: AccessDep):
    """Leave a team yourself, or remove someone as its lead or a platform admin. A team keeps one lead."""
    team = await _team(db, access, team_id, lock=True)
    self_leave = user_id == access.principal.username
    if not self_leave and not await _may_manage(db, access, team):
        raise HTTPException(status_code=403, detail="Team management forbidden")
    role = await _team_role(db, team.id, user_id)
    if role is None:
        raise HTTPException(status_code=404, detail="Not in the team")
    if role == "lead":
        leads = (await db.execute(select(func.count()).select_from(TeamMember).where(
            TeamMember.team_id == team.id, TeamMember.role == "lead"))).scalar_one()
        if leads <= 1:
            raise HTTPException(status_code=409, detail="A team keeps at least one lead")
    await db.execute(delete(TeamMember).where(TeamMember.team_id == team.id, TeamMember.user_id == user_id))
    _record_human_audit(db, access, "team.member_removed", "team", access.principal.username,
                        metadata={"team_id": str(team.id), "user_id": user_id})
    await db.commit()


@router.put("/api/teams/{team_id}/projects/{project}")
async def link_team_project(team_id: uuid.UUID, project: str, db: DbDep, access: AccessDep):
    """Mark a project as one the team works on. It grants no project access."""
    team = await _team(db, access, team_id, lock=True)
    if not await _may_manage(db, access, team):
        raise HTTPException(status_code=403, detail="Team management forbidden")
    await _project(db, access, project)
    if access.principal.role != "admin" and not await _membership(db, access, project):
        raise HTTPException(status_code=403, detail="Link only projects you belong to")
    exists = (await db.execute(select(TeamProject).where(
        TeamProject.team_id == team.id, TeamProject.project_id == project))).scalar_one_or_none()
    if exists is None:
        db.add(TeamProject(team_id=team.id, project_id=project, linked_by=access.principal.username))
        _record_human_audit(db, access, "team.project_linked", "team", access.principal.username,
                            metadata={"team_id": str(team.id), "project_id": project})
        await db.commit()
    return {"ok": True}


@router.delete("/api/teams/{team_id}/projects/{project}", status_code=204)
async def unlink_team_project(team_id: uuid.UUID, project: str, db: DbDep, access: AccessDep):
    team = await _team(db, access, team_id, lock=True)
    if not await _may_manage(db, access, team):
        raise HTTPException(status_code=403, detail="Team management forbidden")
    result = await db.execute(delete(TeamProject).where(
        TeamProject.team_id == team.id, TeamProject.project_id == project))
    if not result.rowcount:
        raise HTTPException(status_code=404, detail="Project not linked")
    _record_human_audit(db, access, "team.project_unlinked", "team", access.principal.username,
                        metadata={"team_id": str(team.id), "project_id": project})
    await db.commit()


# --- campaigns -------------------------------------------------------------------------

async def _my_projects(db: AsyncSession, access: Access) -> tuple[set[str], set[str]]:
    # Project names are global; only memberships in this tenant's projects count.
    rows = (await db.execute(select(ProjectMembership.project_id, ProjectMembership.role).join(
        Project, Project.name == ProjectMembership.project_id,
    ).where(ProjectMembership.user_id == access.principal.username,
            Project.tenant_id == access.principal.tenant_id))).all()
    return {p for p, _ in rows}, {p for p, r in rows if r == "admin"}


async def _campaign_rows(db: AsyncSession, access: Access, campaigns: list[Campaign]) -> list[dict]:
    tenant = access.principal.tenant_id
    mine, managed = await _my_projects(db, access)
    ids = [c.id for c in campaigns]
    enrolled: dict[uuid.UUID, list[str]] = {i: [] for i in ids}
    if ids:
        for m in (await db.execute(select(CampaignMembership).where(
                CampaignMembership.campaign_id.in_(ids)).order_by(CampaignMembership.project_id))).scalars():
            enrolled[m.campaign_id].append(m.project_id)
    counts = dict((await db.execute(select(MemoryNode.owner_id, func.count()).where(
        MemoryNode.tenant_id == tenant, MemoryNode.scope == "campaign", MemoryNode.deleted_at.is_(None),
        MemoryNode.is_current.is_(True), MemoryNode.owner_id.in_([str(i) for i in ids] or [""]),
    ).group_by(MemoryNode.owner_id))).all())
    rows = []
    for c in campaigns:
        projects = enrolled[c.id]
        rows.append({
            "id": str(c.id), "name": c.name, "description": c.description, "status": c.status,
            "default_ttl_days": c.default_ttl.days if c.default_ttl else None,
            "projects": projects, "memories": counts.get(str(c.id), 0),
            "my_projects": sorted(set(projects) & mine),
            # Projects the person administers that could join or leave this campaign.
            "can_enroll": sorted(managed - set(projects)) if c.status == "active" else [],
            "can_leave": sorted(managed & set(projects)),
        })
    return rows


@router.get("/api/campaigns")
async def list_campaigns(db: DbDep, access: AccessDep):
    """Campaigns that enroll one of the person's projects, and active ones their projects may join."""
    tenant = access.principal.tenant_id
    stmt = select(Campaign).where(Campaign.tenant_id == tenant)
    if access.principal.role != "admin":
        mine, managed = await _my_projects(db, access)
        enrolled_in = select(CampaignMembership.campaign_id).where(CampaignMembership.project_id.in_(mine or {""}))
        visible = [Campaign.id.in_(enrolled_in)]
        if managed:
            visible.append(Campaign.status == "active")
        stmt = stmt.where(or_(*visible))
    items = await _campaign_rows(db, access, list((await db.execute(stmt.order_by(Campaign.name))).scalars()))
    return {"items": items, "total": len(items)}


async def _campaign(db: AsyncSession, access: Access, campaign_id: uuid.UUID) -> Campaign:
    campaign = (await db.execute(select(Campaign).where(
        Campaign.id == campaign_id, Campaign.tenant_id == access.principal.tenant_id))).scalar_one_or_none()
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


@router.post("/api/campaigns/{campaign_id}/projects/{project}")
async def enroll_project(campaign_id: uuid.UUID, project: str, db: DbDep, access: AccessDep):
    """A project admin enrolls their project in an active campaign."""
    campaign = await _campaign(db, access, campaign_id)
    await _project(db, access, project, lock=True)
    if not await _membership(db, access, project, admin=True):
        raise HTTPException(status_code=403, detail="Only a project admin enrolls the project")
    if campaign.status != "active":
        raise HTTPException(status_code=409, detail="Campaign is not active")
    exists = (await db.execute(select(CampaignMembership).where(
        CampaignMembership.campaign_id == campaign.id, CampaignMembership.project_id == project))).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(status_code=409, detail="Project already enrolled")
    db.add(CampaignMembership(campaign_id=campaign.id, project_id=project, enrolled_by=access.principal.username))
    _record_human_audit(db, access, "campaign.project_enrolled", "campaign", str(campaign.id),
                        metadata={"campaign_id": str(campaign.id), "project_id": project})
    await db.commit()
    return {"ok": True}


@router.delete("/api/campaigns/{campaign_id}/projects/{project}", status_code=204)
async def leave_campaign(campaign_id: uuid.UUID, project: str, db: DbDep, access: AccessDep):
    """A project admin takes their project out of a campaign. Campaign memories stay with the campaign."""
    campaign = await _campaign(db, access, campaign_id)
    await _project(db, access, project, lock=True)
    if not await _membership(db, access, project, admin=True):
        raise HTTPException(status_code=403, detail="Only a project admin takes the project out")
    result = await db.execute(delete(CampaignMembership).where(
        CampaignMembership.campaign_id == campaign.id, CampaignMembership.project_id == project))
    if not result.rowcount:
        raise HTTPException(status_code=404, detail="Project not enrolled")
    _record_human_audit(db, access, "campaign.project_left", "campaign", str(campaign.id),
                        metadata={"campaign_id": str(campaign.id), "project_id": project})
    await db.commit()
