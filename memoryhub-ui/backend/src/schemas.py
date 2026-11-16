"""Pydantic response models for the MemoryHub UI BFF API."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class GraphNode(BaseModel):
    id: str
    content: str
    stub: str
    scope: str
    weight: float
    branch_type: str | None
    owner_id: str
    version: int
    created_at: datetime
    updated_at: datetime
    metadata: dict | None = None
    parent_id: str | None = None


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    type: str  # "parent_child" | relationship_type value


class GraphResponse(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]


class ScopeCount(BaseModel):
    scope: str
    count: int


class RecentActivity(BaseModel):
    id: str
    stub: str
    scope: str
    owner_id: str
    updated_at: datetime
    action: str  # "created" or "updated"


class StatsResponse(BaseModel):
    total_memories: int
    scope_counts: list[ScopeCount]
    recent_activity: list[RecentActivity]
    mcp_health: bool


class MemoryDetail(BaseModel):
    id: str
    content: str
    stub: str
    scope: str
    weight: float
    branch_type: str | None
    owner_id: str
    version: int
    is_current: bool
    parent_id: str | None
    metadata: dict | None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None
    children_count: int
    relationships: list[GraphEdge]


class VersionEntry(BaseModel):
    id: str
    version: int
    is_current: bool
    stub: str
    content: str
    created_at: datetime


class RuleVersionEntry(BaseModel):
    id: str
    version: int
    is_current: bool
    name: str
    edited_by: str | None
    created_at: datetime


class SearchMatch(BaseModel):
    id: str
    score: float


class CreateClientRequest(BaseModel):
    client_id: str
    client_name: str
    identity_type: str = "user"
    tenant_id: str
    default_scopes: list[str] = ["memory:read"]


class UpdateClientRequest(BaseModel):
    client_name: str | None = None
    active: bool | None = None
    default_scopes: list[str] | None = None


class ClientResponse(BaseModel):
    client_id: str
    client_name: str
    identity_type: str
    tenant_id: str
    default_scopes: list[str]
    active: bool
    created_at: datetime
    updated_at: datetime


class ClientCreatedResponse(ClientResponse):
    client_secret: str
    api_key: str


class SecretRotatedResponse(BaseModel):
    client_id: str
    client_secret: str
    api_key: str


class ApiKeyRotatedResponse(BaseModel):
    client_id: str
    api_key: str


class PublicConfigResponse(BaseModel):
    """Public-facing URLs used by the UI to generate contributor welcome text.

    These are the URLs an external agent would use to reach MemoryHub from
    outside the cluster. The BFF itself uses the internal SVC addresses.
    """

    mcp_url: str
    auth_url: str


# --- Curation Rules ---

class CurationRuleResponse(BaseModel):
    id: str
    name: str
    description: str | None
    trigger: str
    tier: str
    config: dict
    action: str
    scope_filter: str | None
    layer: str
    owner_id: str | None
    override: bool
    enabled: bool
    priority: int
    version: int
    is_current: bool
    previous_version_id: str | None
    edited_by: str | None
    created_at: datetime
    updated_at: datetime


class CreateRuleRequest(BaseModel):
    name: str
    description: str | None = None
    trigger: str = "on_write"
    tier: str  # "regex" or "embedding"
    config: dict = {}
    action: str  # "block", "quarantine", "flag", etc.
    scope_filter: str | None = None
    layer: str = "user"
    owner_id: str | None = None
    override: bool = False
    enabled: bool = True
    priority: int = 100


class UpdateRuleRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    trigger: str | None = None
    tier: str | None = None
    config: dict | None = None
    action: str | None = None
    scope_filter: str | None = None
    layer: str | None = None
    owner_id: str | None = None
    override: bool | None = None
    enabled: bool | None = None
    priority: int | None = None
    edited_by: str | None = None


# --- Contradiction Reports ---

class ContradictionResponse(BaseModel):
    id: str
    memory_id: str
    observed_behavior: str
    confidence: float
    reporter: str
    created_at: datetime
    resolved: bool
    resolved_at: datetime | None


class ContradictionStatsResponse(BaseModel):
    total: int
    unresolved: int
    high_confidence: int  # > 0.8
    medium_confidence: int  # 0.5 - 0.8
    low_confidence: int  # < 0.5


class UpdateContradictionRequest(BaseModel):
    resolved: bool


# ---------------------------------------------------------------------------
# Projects (#671)
# ---------------------------------------------------------------------------

# Lowercase slug: letters, digits and single hyphens, like a Kubernetes name.
PROJECT_NAME_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
USER_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._@:+-]*$"

ProjectRole = Literal["member", "admin"]


class ProjectPolicies(BaseModel):
    """Per-project limits. On update, a field sent as null clears that policy."""

    ttl_days: int | None = Field(default=None, ge=1, le=3650)
    max_memories: int | None = Field(default=None, ge=1, le=10_000_000)
    default_weight: float | None = Field(default=None, ge=0.0, le=1.0)


class ProjectMemberInput(BaseModel):
    user_id: str = Field(min_length=1, max_length=255, pattern=USER_ID_PATTERN)
    role: ProjectRole = "member"


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=63, pattern=PROJECT_NAME_PATTERN)
    description: str | None = Field(default=None, max_length=2000)
    invite_only: bool = False
    policies: ProjectPolicies | None = None
    members: list[ProjectMemberInput] = Field(default_factory=list, max_length=200)


class UpdateProjectRequest(BaseModel):
    description: str | None = Field(default=None, max_length=2000)
    invite_only: bool | None = None
    policies: ProjectPolicies | None = None


class UpdateMemberRequest(BaseModel):
    role: ProjectRole


class ProjectResponse(BaseModel):
    name: str
    description: str | None
    invite_only: bool
    created_at: datetime | None
    created_by: str
    archived: bool
    archived_at: datetime | None
    archived_by: str | None
    policies: ProjectPolicies
    member_count: int
    memory_count: int
    # The caller's view of the project.
    my_role: ProjectRole | None
    can_manage: bool
    can_delete: bool


class ProjectMemberResponse(BaseModel):
    user_id: str
    role: ProjectRole
    joined_at: datetime | None
    joined_by: str | None


class ProjectDetailResponse(ProjectResponse):
    members: list[ProjectMemberResponse]


class ProjectListResponse(BaseModel):
    items: list[ProjectResponse]
    total: int
    # Any signed-in user may create a project (self-service).
    can_create: bool


class ProjectDeletedResponse(BaseModel):
    project: str
    memories_deleted: int
    deleted_at: datetime
