# Project Governance

## Status

What exists (migration 012, shipped 2026-04-15):
- `projects` table with `name` (PK slug), `description`, `invite_only`, `tenant_id`, `created_at`, `created_by`
- `project_memberships` with FK to `projects.name`; `role` column supports `member` and `admin`
- Auto-enrollment: writing to an open project with `ensure_project_membership()` creates the project row and membership atomically
- `manage_project` MCP tool with `action="list"` and `filter=mine/all`
- `manage-memberships.py` script does raw SQL inserts, bypassing any future service-layer guards

What exists (migration 033, #671):
- `archived_at`/`archived_by`, `deleted_at`/`deleted_by` and a JSON `policies` column on `projects`
- Service functions in `services/project.py`: `update_project`, `archive_project`, `unarchive_project`, `delete_project` (cascading soft delete), `update_member_role`, `list_tenant_projects`, `validate_policies`
- Write-path guards in `services/memory.py`: archived and deleted projects refuse create/update/delete; `max_memories`, `default_weight` and `ttl_days` apply on create. They act only when a `projects` row exists, so deployments without project isolation behave as before
- BFF `/api/projects` (see Admin API), with caller identity from the oauth-proxy headers

What is still missing:
- MCP tools for explicit project creation, update and membership (the BFF API covers the dashboard)
- No ORM `relationship()` / `back_populates` pair between `Project` and `ProjectMembership` — the FK is modeled, but `project.memberships` ORM navigation is not available; membership queries go through `ProjectMembership` directly.
- No FK from `memory_nodes.scope_id` to `projects.name` (see Referential Integrity)
- The Projects panel in `memoryhub-ui` is not yet wired to the API

## Problem Statement

A projects table and auto-enrollment are necessary but not sufficient for governed collaboration. Without explicit lifecycle management, projects accumulate silently — any agent can create one by writing a project-scoped memory, and there is no way to shut one down, transfer ownership, or enforce access to invite-only projects without direct database manipulation. Invite-only is a flag with no enforcement mechanism at the API level: there is no way to add a member to an invite-only project without bypassing the application.

This matters because project-scoped memories are visibility-controlled. An invite-only project is only as private as the database access controls that protect it. The gap between the data model's intent and the actual enforcement creates a governance hole: administrators need an API that can manage the full project lifecycle without resorting to `psql`.

## Design

### Project Lifecycle

Projects have four states:

- **Active** — normal operation; auto-enrollment applies to open projects; invite-only projects reject unenrolled writes
- **Invite-only** — open for enrolled members; new members require explicit invite via the membership API
- **Archived** — read-only; no new writes or enrollments accepted; existing memories remain visible to enrolled members
- **Deleted** — soft-deleted; memories are archived to S3 (or flagged for retention) before the project row is removed

**Create.** The current implicit creation (first write auto-creates) remains for open projects. Explicit creation via the admin API is required for invite-only projects, since there is no write that would trigger auto-enrollment.

**Archive vs. Delete.** Archiving preserves the project and its memories but stops new writes. Deletion is destructive: project-scoped memories whose `scope_id` matches the project become orphaned unless we add a FK (see Referential Integrity). The lifecycle states map to two new columns on the `projects` table:

```sql
ALTER TABLE projects ADD COLUMN archived_at TIMESTAMPTZ;
ALTER TABLE projects ADD COLUMN archived_by VARCHAR(255);
```

Deletion uses soft-delete via `deleted_at` / `deleted_by` (same pattern as `memory_nodes`). The archive and delete states are enforced at the service layer in `services/project.py`, not at the database level.

**Cascade behavior.** When a project is archived, project-scoped memories become read-only — the write path checks `project.archived_at IS NULL` before accepting new memories. When a project is deleted, two options exist:

1. Cascade soft-delete all project-scoped `memory_nodes` (set `deleted_at`)
2. Retain memories but strip `scope_id` (orphan them into a `scope=project` without a project)

Option 1 is the correct default: deleting a project deletes its data. Operators who want to preserve memories should archive rather than delete. The cascade is implemented in the project delete service method, not via a database ON DELETE CASCADE, so it is visible in code and auditable.

### Admin API

The BFF (`memoryhub-ui/backend/src/routes.py`) gains a `/api/projects` resource following the same pattern as `/api/rules` (direct DB access, tenant-scoped) and `/api/clients` (proxied to auth service with `_admin_request`). Projects live in the MemoryHub DB directly, so no proxy needed.

**Endpoints** (implemented in `memoryhub-ui/backend/src/projects.py`):

```
GET    /api/projects                  List projects (?archived=, ?q=, ?sort=name|created_at|-created_at)
POST   /api/projects                  Create a project; the caller becomes its admin. Optional policies and members
GET    /api/projects/{name}           Project detail with members
PATCH  /api/projects/{name}           Update description, invite_only or policies (409 while archived)
POST   /api/projects/{name}/archive   Make the project read-only
POST   /api/projects/{name}/unarchive Make it writable again
DELETE /api/projects/{name}           Soft-delete with its memories. 409 {message, memory_count} unless ?force=true or empty

GET    /api/projects/{name}/members            List members
POST   /api/projects/{name}/members            Add a member (409 while archived)
PATCH  /api/projects/{name}/members/{user_id}  Change role (409 when demoting the last admin)
DELETE /api/projects/{name}/members/{user_id}  Remove a member, or leave (409 for the last admin)
```

Project names are slugs: `^[a-z0-9]+(?:-[a-z0-9]+)*$`, at most 63 characters. Names are never reused, deleted projects included. Policies are `ttl_days` (1–3650), `max_memories` (1–10,000,000) and `default_weight` (0.0–1.0); on PATCH they merge into the current ones and a field sent as `null` clears it.

**Caller identity.** The oauth-proxy sidecar forwards the signed-in user (`X-Forwarded-Preferred-Username` / `X-Forwarded-User`) and groups (`X-Forwarded-Groups`). The BFF trusts them only with `MEMORYHUB_UI_TRUST_PROXY_HEADERS=true`; without a user it answers 401. Platform admins are listed in `MEMORYHUB_UI_ADMIN_USERS` or belong to `MEMORYHUB_UI_ADMIN_GROUP` (default `memoryhub-admins`). The group check needs a proxy that sends `X-Forwarded-Groups`; the `ose-oauth-proxy` in `memoryhub-ui/openshift.yaml` does not, so list admins by name there. Turn trust on only when port 8080 is reachable solely through the proxy. With trust off, every request acts as `MEMORYHUB_UI_OPERATOR_NAME` (default `dashboard`) with platform-admin rights, as the single-operator dashboard always has.

**Authorization.** Everything is scoped to `settings.ui_tenant_id`; other tenants' projects answer 404.

| Action | Who |
|---|---|
| List / view | Anyone for open projects; invite-only ones only for members, the creator and platform admins (others get 404) |
| Create | Any signed-in user (self-service) |
| Update, archive, unarchive, delete, add/change/remove members | Platform admin, the project's creator, or a project admin |
| Leave | Any member, unless they are the last admin |

**Service layer.** `delete_project` soft-deletes every project memory (all versions and branches) and the project row in one transaction. Deleted projects drop out of membership lookups at once, so members lose access immediately.

### MCP Tools

Three new tools for the MCP server:

**`create_project`** — explicit project creation. Needed for invite-only projects (which cannot be auto-created on first write) and for projects where the caller wants to set metadata before any memories are written.

```
Input:  project_id, description?, invite_only=False
Output: project name, invite_only, created_at
```

Authorization: any authenticated user can create a project within their tenant.

**`update_project`** — update description or enrollment policy. Changing `invite_only` from `False` to `True` on a project with existing members should not remove existing members but must stop auto-enrollment.

```
Input:  project_id, description?, invite_only?
Output: updated project fields
```

Authorization: project admin role or the original `created_by` user.

**`manage_membership`** — add or remove a member from a project.

```
Input:  project_id, user_id, action=("add"|"remove"), role=("member"|"admin")?
Output: membership record or confirmation of removal
```

Authorization: project admin role or the original `created_by` user. This is the only API-level path for adding members to invite-only projects; it replaces the direct DB manipulation in `manage-memberships.py`.

These tools are designed following the fips-agents scaffold workflow (`/plan-tools` -> `/create-tools` -> `/exercise-tools`). This document describes the tool contracts; scaffold produces the implementation files.

### Project-Level Policies

Add a `projects_policies` table (or a `policies` JSONB column on `projects`) for per-project configuration:

```sql
ALTER TABLE projects ADD COLUMN policies JSONB NOT NULL DEFAULT '{}';
```

JSONB is sufficient at this scale; a separate table is over-engineering until multiple services need to join on policy fields. The column stores a structured object:

```json
{
  "ttl_days": null,
  "max_memories": null,
  "default_weight": 0.7
}
```

- `ttl_days` — if set, new project-scoped memories without an explicit `relevant_until` get one this many days after the write (offset from `created_at`). Overrides the system default. `null` means no expiry.
- `max_memories` — quota cap on active memories for the project. The write path checks count before inserting; it counts current, non-deleted root memories (branches and old versions do not count) and the MCP caller gets a `ToolError` from `ProjectQuotaExceededError`. `null` means no cap.
- `default_weight` — default relevance weight for new memories in this project (range 0.0–1.0). Useful for low-priority reference projects that should not dominate search results.

The `enrollment_policy` field (`invite_only`) already exists as a top-level column and stays there for indexed lookup performance.

### Referential Integrity

`memory_nodes.scope_id` has no FK to `projects.name`. Adding one:

**For:** prevents orphan scope references when a project is renamed (not supported in current design) or deleted; makes the data model self-describing; query planner can use it.

**Against:** every `write_memory` call to a project-scoped memory would trigger a FK check. For open projects, `ensure_project_membership` already creates the project row before the memory is inserted, so the row exists. For invite-only projects the same is true. The FK should succeed in practice.

**Cascade:** `ON DELETE SET NULL` is wrong — it silently orphans memories when a project is deleted, which is undetectable. `ON DELETE RESTRICT` is wrong — it blocks the project delete until memories are manually removed. `ON DELETE CASCADE` on `memory_nodes` would hard-delete memory rows, bypassing our soft-delete audit trail.

The correct approach: no database-level cascade on this FK. Instead, enforce it in the service layer: the `delete_project` service function soft-deletes associated memories first, then deletes the project row. This keeps the delete auditable and reversible (via `deleted_at`). The FK uses `ON DELETE RESTRICT` as a safety net — if the service layer fails to cascade, the DB rejects the project delete rather than orphaning rows.

Migration (013):

```sql
ALTER TABLE memory_nodes
  ADD CONSTRAINT fk_memory_nodes_scope_project
  FOREIGN KEY (scope_id) REFERENCES projects(name)
  ON DELETE RESTRICT
  DEFERRABLE INITIALLY DEFERRED;
```

Deferred constraint allows the service layer to soft-delete memories and the project in the same transaction without ordering concerns.

There is one backfill concern: existing `scope_id` values in `memory_nodes` that do not have a corresponding row in `projects` will violate the FK. Migration 013 must either backfill missing project rows from `memory_nodes.scope_id` (similar to what migration 012 did for `project_memberships`) or null out orphaned `scope_id` values. Backfill is preferred — it preserves the scoping intent.

### UI Integration

Add a **Projects** panel to `memoryhub-ui`, inserted in `NAV_ITEMS` between "Users & Agents" and "Curation Rules":

```typescript
{ id: 'projects', label: 'Projects', panel: 'projects' },
```

The panel is a new `ProjectManagement.tsx` component following the structure of `CurationRules.tsx` (toolbar + table + detail drawer) and `ClientManagement.tsx` (inline create form). Layout:

- **Table view:** columns for Name, Description, Invite Only badge, Archived badge, Member Count, Memory Count, Created By, Created At. Sortable by Name and Created At.
- **Toolbar actions:** Create Project button (opens inline form); filter by Archived.
- **Row actions:** Edit (description + invite_only toggle), Archive/Unarchive, Delete (with confirmation dialog that shows memory cascade count).
- **Detail drawer:** shows project metadata, policies (TTL, quota), and a members sub-table with Add/Remove member controls.

The members sub-table is the UI replacement for `manage-memberships.py`'s `add-project`/`remove-project` commands. The `role` dropdown in the Add Member form maps to the `admin`/`member` values in `project_memberships.role`.

PatternFly components: `Table`, `Toolbar`, `Drawer`, `Modal` (for delete confirmation), `Switch` (for invite_only toggle), `Badge` (for status). All consistent with existing panels.

## Dependencies

**Depends on:**
- Migration 012 (`projects` table) — already shipped
- Migration 033 (`archived_at`/`archived_by`/`deleted_at`/`deleted_by`/`policies` columns) — shipped with #671
- `fk_memory_nodes_scope_project` — not yet implemented

**Depended on by:**
- `manage-memberships.py` — remains functional but superseded for add/remove operations by the membership API; the script should be updated to call the HTTP API rather than direct SQL once the API is deployed, to ensure service-layer guards apply
- MCP write path — must check `archived_at IS NULL` before accepting project-scoped writes
- `memoryhub-ui` Projects panel

## Open Questions

**Rename support.** `projects.name` is the primary key and is used as the FK target in `project_memberships` and (after migration 013) `memory_nodes`. Renaming a project would require cascading updates across multiple tables. Renames are out of scope for this design; projects are named at creation and the name is immutable. If rename becomes a requirement, the right approach is a surrogate UUID PK with a separate unique slug column.

**Role semantics.** The `role` column on `project_memberships` has values `member` and `admin` but the codebase does not yet enforce what `admin` can do beyond what `member` can. This design uses `admin` to gate `update_project` and `manage_membership` operations. The exact permission matrix should be documented in `docs/design/governance.md` when implemented.

**Quota enforcement granularity.** `max_memories` counts all active (non-deleted, `is_current = true`) memories for the project. Whether this counts across all versions or just current nodes should be decided at implementation time. Counting only `is_current` rows is simpler and more useful (reflects what agents actually see).

**`manage-memberships.py` retirement timeline.** The script bypasses service-layer guards by writing raw SQL. Once the membership API is deployed and the UI panel is functional, the script should be deprecated. A migration note in the script's docstring is sufficient; a hard removal can happen once the cluster is confirmed running the new API.

**Invite-only "request to join" flow.** Agents can discover open projects via `manage_project(action="list", filter="all")` but invite-only projects are hidden from non-members entirely. There is no API path for an agent to signal "I'd like to join this invite-only project" short of out-of-band contact with an admin. A `request_project_membership` MCP tool (or equivalent admin-notification path) is a plausible follow-up but is out of scope for this design.
