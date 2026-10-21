"""Role-based access for dashboard reads.

Two rules, enforced here on the server so the browser never receives what a
viewer may not see:

* **Users** read memories through the same RBAC predicate agents get: their
  own user-scope memories, projects they belong to, roles assigned to them,
  campaigns their projects are enrolled in, and every organizational and
  enterprise memory.
* **Platform admins** see every memory's metadata (scope, owner, writer,
  weight, version, links, flags, activity) but never the content of personal,
  project, campaign or role memories. Only scopes listed in the tenant's
  ``privacy.admin_readable_scopes`` (organizational and enterprise by default)
  stay readable.

The open-mode ``operator`` (no sign-in configured, development only) keeps the
legacy tenant-wide view.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from memoryhub_core.models.campaign import CampaignMembership
from memoryhub_core.models.memory import MemoryNode
from memoryhub_core.models.project import ProjectMembership
from memoryhub_core.models.role import RoleAssignment
from sqlalchemy import and_, false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import Settings, get_settings
from src.ui_common import DEFAULT_TENANT_SETTINGS, Viewer, load_tenant_settings, viewer_from_request

HIDDEN_TEXT = "Content hidden"
OPEN_SCOPES_DEFAULT = tuple(DEFAULT_TENANT_SETTINGS["privacy"]["admin_readable_scopes"])


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def current_viewer(request: Request, settings: Annotated[Settings, Depends(get_settings)]) -> Viewer:
    return viewer_from_request(request, settings)


def admin_viewer(request: Request, settings: Annotated[Settings, Depends(get_settings)]) -> Viewer:
    viewer = viewer_from_request(request, settings)
    if not viewer.can_admin:
        raise HTTPException(status_code=403, detail="Only platform admins can do this")
    return viewer


def content_viewer(request: Request, settings: Annotated[Settings, Depends(get_settings)]) -> Viewer:
    """For endpoints whose whole purpose is reading memory content."""
    viewer = viewer_from_request(request, settings)
    if viewer.redacts_content:
        raise HTTPException(
            status_code=403,
            detail="Memory content is confidential. Platform admins see metadata only; request break-glass access.",
        )
    return viewer


def legacy_viewer(request: Request, settings: Annotated[Settings, Depends(get_settings)]) -> Viewer:
    """The original (pre-redesign) read endpoints are not role-aware.

    They stay available in open development mode only; signed-in viewers use
    the dashboard endpoints, which filter and redact per role.
    """
    viewer = viewer_from_request(request, settings)
    if viewer.role != "operator":
        raise HTTPException(status_code=403, detail="This endpoint is not available to signed-in viewers")
    return viewer


ViewerDep = Annotated[Viewer, Depends(current_viewer)]
AdminDep = Annotated[Viewer, Depends(admin_viewer)]
ContentDep = Annotated[Viewer, Depends(content_viewer)]


# ---------------------------------------------------------------------------
# Read predicate for users
# ---------------------------------------------------------------------------


@dataclass
class Grants:
    identity: str
    projects: set[str] = field(default_factory=set)
    roles: set[str] = field(default_factory=set)
    campaigns: set[str] = field(default_factory=set)


async def load_grants(db: AsyncSession, tenant_id: str, identity: str) -> Grants:
    projects = set(
        (await db.execute(select(ProjectMembership.project_id).where(ProjectMembership.user_id == identity)))
        .scalars()
        .all()
    )
    roles = set(
        (
            await db.execute(
                select(RoleAssignment.role_name).where(
                    RoleAssignment.user_id == identity, RoleAssignment.tenant_id == tenant_id
                )
            )
        )
        .scalars()
        .all()
    )
    campaigns: set[str] = set()
    if projects:
        campaigns = {
            str(c)
            for c in (
                await db.execute(
                    select(CampaignMembership.campaign_id).where(CampaignMembership.project_id.in_(list(projects)))
                )
            )
            .scalars()
            .all()
        }
    return Grants(identity=identity, projects=projects, roles=roles, campaigns=campaigns)


def readable_clause(grants: Grants, node=MemoryNode):
    """SQL version of the agent read RBAC for one identity."""
    sid = node.scope_id
    clauses = [
        node.scope.in_(("organizational", "enterprise")),
        and_(node.scope == "user", node.owner_id == grants.identity),
    ]
    if grants.projects:
        clauses.append(and_(node.scope == "project", or_(sid.in_(grants.projects), node.owner_id.in_(grants.projects))))
    if grants.roles:
        clauses.append(and_(node.scope == "role", or_(sid.in_(grants.roles), node.owner_id.in_(grants.roles))))
    if grants.campaigns:
        clauses.append(
            and_(node.scope == "campaign", or_(sid.in_(grants.campaigns), node.owner_id.in_(grants.campaigns)))
        )
    return or_(*clauses)


def readable_python(grants: Grants):
    def visible(n: Any) -> bool:
        if n.scope in ("organizational", "enterprise"):
            return True
        key = n.scope_id or n.owner_id
        if n.scope == "user":
            return n.owner_id == grants.identity
        if n.scope == "project":
            return key in grants.projects
        if n.scope == "role":
            return key in grants.roles
        if n.scope == "campaign":
            return key in grants.campaigns
        return False

    return visible


@dataclass
class Access:
    """Everything an endpoint needs to filter and redact for one viewer."""

    viewer: Viewer
    grants: Grants | None
    open_scopes: tuple[str, ...]

    def clause(self, node=MemoryNode):
        """Extra WHERE clause, or None when the viewer is not row-restricted."""
        if self.grants is None:
            return None
        return readable_clause(self.grants, node)

    def filters(self, node=MemoryNode) -> list:
        clause = self.clause(node)
        return [clause] if clause is not None else []

    def can_read_row(self, n: Any) -> bool:
        if self.grants is None:
            return True
        return readable_python(self.grants)(n)

    def hides(self, scope: str | None) -> bool:
        return self.viewer.redacts_content and (scope or "") not in self.open_scopes

    def redact(self, item: dict, scope: str | None = None) -> dict:
        """Strip content-bearing fields from a serialised memory for admins."""
        if not self.hides(scope if scope is not None else item.get("scope")):
            item.setdefault("redacted", False)
            return item
        out = dict(item)
        sid = str(out.get("id", ""))
        short = f"mem_{sid.replace('-', '')[:4]}…{sid.replace('-', '')[-3:]}" if sid else "memory"
        for key in ("stub", "title"):
            if key in out:
                out[key] = short
        for key in ("content", "content_preview", "text", "observed_behavior", "memory_stub"):
            if key in out and out[key] is not None:
                out[key] = HIDDEN_TEXT
        if "domains" in out:
            out["domains"] = []
        if "entities" in out:
            out["entity_count"] = len(out["entities"] or [])
            out["entities"] = []
        if "metadata" in out:
            out["metadata"] = None
        out["redacted"] = True
        return out

    def redact_tree(self, obj: Any, scope: str | None = None) -> Any:
        """Redact every serialised memory inside a nested response.

        Dicts carrying ``content`` / ``stub`` / ``content_preview`` are memories;
        their own ``scope`` wins, otherwise they inherit the enclosing one (for
        versions and branches of a memory).
        """
        if not self.viewer.redacts_content:
            return obj
        if isinstance(obj, list):
            return [self.redact_tree(x, scope) for x in obj]
        if not isinstance(obj, dict):
            return obj
        own = obj.get("scope", scope)
        if any(
            k in obj for k in ("content", "content_preview", "stub", "observed_behavior", "memory_stub")
        ) and self.hides(own):
            obj = self.redact(obj, own)
        return {
            k: self.redact_tree(v, own) if isinstance(v, (list, dict)) and k != "metadata" else v
            for k, v in obj.items()
        }

    def ensure_readable(self, n: Any) -> None:
        if n is None or not self.can_read_row(n):
            raise HTTPException(status_code=404, detail="Memory not found")


async def access_for(db: AsyncSession, settings: Settings, viewer: Viewer) -> Access:
    grants = await load_grants(db, settings.ui_tenant_id, viewer.username) if viewer.restricted else None
    open_scopes = OPEN_SCOPES_DEFAULT
    if viewer.redacts_content:
        privacy = (await load_tenant_settings(db, settings.ui_tenant_id)).get("privacy", {})
        scopes = privacy.get("admin_readable_scopes")
        if isinstance(scopes, list):
            open_scopes = tuple(s for s in scopes if s in ("organizational", "enterprise"))
    return Access(viewer=viewer, grants=grants, open_scopes=open_scopes)


def nothing():
    return false()
