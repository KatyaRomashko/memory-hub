// API types for the MemoryHub dashboard BFF. Every figure on screen comes
// from these responses; nothing is hard-coded in the UI.

export type Scope = 'user' | 'project' | 'campaign' | 'role' | 'organizational' | 'enterprise' | string;

/** admin: manages the platform, sees memory metadata only.
 *  user: owns memories, reads what RBAC allows.
 *  operator: open development mode (no sign-in configured). */
export type Role = 'admin' | 'user' | 'operator';

export interface Me {
  username: string;
  name: string;
  email: string | null;
  initials: string;
  source: 'oauth-proxy' | 'api-key' | 'account' | 'anonymous';
  role: Role;
  tenant_id: string;
}

export type SignupMode = 'open' | 'domain' | 'invite' | 'closed';

export interface AuthStatus {
  mode: 'oauth-proxy' | 'api-key' | 'accounts' | 'open';
  authenticated: boolean;
  api_key_login: boolean;
  local_accounts: boolean;
  user: string | null;
  role: Role;
  cluster_domain: string | null;
  setup_required: boolean;
  setup_token_required?: boolean;
  accounts_available?: boolean;
  organization?: string | null;
  signup?: { mode: SignupMode; require_approval: boolean; allowed_domains: string[] };
  password_min_length?: number;
}

export interface SetupPayload {
  setup_token?: string;
  organization: { name: string; contact_email: string };
  admin: { username: string; email: string; display_name: string; password: string };
  signup: { mode: SignupMode; allowed_domains: string[]; require_approval: boolean };
  privacy: {
    admin_readable_scopes: string[];
    break_glass: boolean;
    break_glass_hours: number;
    break_glass_approvers: number;
    notify_owner: boolean;
  };
  security: { session_hours: number; password_min_length: number };
}

export interface RegisterPayload {
  username: string;
  email: string;
  display_name: string;
  password: string;
  job_title?: string;
  requested_projects: string[];
  invite_code?: string;
  accept_privacy: boolean;
}

export interface AccountRow {
  id: string;
  username: string;
  email: string;
  display_name: string;
  role: 'admin' | 'user';
  status: 'active' | 'pending' | 'disabled';
  job_title: string | null;
  requested_projects: string[];
  created_at: string;
  last_login_at: string | null;
  approved_by: string | null;
  invited_by: string | null;
}

export interface InviteRow {
  id: string;
  code: string;
  role: 'admin' | 'user';
  email: string | null;
  note: string | null;
  created_by: string;
  created_at: string;
  expires_at: string;
  used_by: string | null;
  state: 'open' | 'used' | 'expired';
}

export interface AccountsResponse {
  items: AccountRow[];
  counts: { all: number; pending: number; admins: number; disabled: number };
  invites: InviteRow[];
}

export interface InviteCreated {
  code: string;
  role: 'admin' | 'user';
  email: string | null;
  expires_at: string;
  link: string;
}

export interface NavCounts {
  review_queue: number;
  memories: number;
}

export type OverviewRange = 'all' | '1h' | '12h' | '1d' | '7d' | '30d';

export interface HealthItem {
  key: string;
  name: string;
  status: 'healthy' | 'degraded' | 'down' | 'not_configured' | 'idle';
  detail: string | null;
  latency_ms: number | null;
}

export interface AuditEvent {
  id: number | string;
  timestamp: string;
  event_type: string;
  operation: string;
  actor_id: string;
  driver_id: string | null;
  actor_type: string | null;
  scope: string;
  owner_id: string;
  memory_id: string | null;
  memory_stub: string | null;
  memory_version: number | null;
  decision: 'allowed' | 'denied';
  metadata: Record<string, unknown>;
  change?: {
    before: { version: number; content: string; weight: number };
    after: { version: number; content: string; weight: number };
  };
}

export interface Overview {
  tenant_id: string;
  generated_at: string;
  memories: { current: number; new_7d: number; growth_pct: number | null };
  agents: { total: number; active_now: number; source: 'auth' | 'memory' };
  searches: { last_24h: number; p95_latency_ms: number | null };
  contradictions: { open: number; over_threshold: number; threshold: number };
  approvals: { enterprise_pending: number; pending_total: number };
  scopes: { scope: Scope; count: number; pending: number }[];
  categories: { name: string; memory_count: number }[];
  /** Bucket start (ISO, UTC) with counts for that bucket. */
  activity: { date: string; searches: number; writes: number }[];
  activity_range: OverviewRange;
  activity_bucket_seconds: number;
  activity_source: 'audit' | 'memory';
  recent: AuditEvent[];
  recent_source: 'audit' | 'memory';
  health: HealthItem[];
  context_assembly: { avg_tokens: number | null; prefix_hit_rate: number | null };
}

export type EntityType = 'person' | 'organization' | 'location' | 'object' | 'event';

export interface EntityRow {
  id: string;
  name: string;
  entity_type: EntityType | string;
  aliases: string[];
  memory_count: number;
  projects: string[];
  created_at: string | null;
  last_seen: string | null;
}

export interface EntitiesResponse {
  items: EntityRow[];
  total: number;
  page: number;
  per_page: number;
  has_more: boolean;
  counts: { all: number; person: number; organization: number; location: number; object: number; event: number };
}

export interface MemoryRow {
  id: string;
  stub: string;
  content_preview: string;
  scope: Scope;
  scope_id: string | null;
  owner_id: string;
  actor_id: string | null;
  driver_id: string | null;
  domains: string[];
  weight: number;
  version: number;
  is_current: boolean;
  status: string;
  content_type: string;
  branch_count: number;
  branch_summary: { type: string; count: number }[];
  open_contradictions: number;
  near_duplicate: number | null;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
  score: number | null;
  entities: EntityRef[];
  /** Admins only: content, domains and entity names were withheld. */
  redacted?: boolean;
  entity_count?: number;
}

export interface EntityRef {
  id: string;
  name: string;
  type: string;
}

export interface Facet {
  value: string;
  count: number;
}

export interface BrowseResponse {
  items: MemoryRow[];
  total: number;
  page: number;
  per_page: number;
  search_mode: 'none' | 'semantic' | 'text' | 'metadata';
  metadata_only?: boolean;
  tab_counts: { current: number; all: number; quarantined: number; deleted: number };
  facets: {
    scopes: Facet[];
    writers: Facet[];
    projects: Facet[];
    domains: Facet[];
    flags: { contradicted: number; has_branches: number; near_duplicate: number | null };
  };
}

export interface BrowseParams {
  tab?: string;
  q?: string;
  mode?: string;
  scope?: string[];
  actor?: string[];
  project?: string;
  domain?: string[];
  min_weight?: number;
  flag?: string[];
  sort?: string;
  order?: string;
  page?: number;
  per_page?: number;
  entity?: string;
}

export interface NodeBrief {
  id: string;
  stub: string;
  scope: Scope;
  scope_id: string | null;
  owner_id: string;
  weight: number;
  version: number;
  is_current: boolean;
  status: string;
  created_at: string;
  updated_at: string;
  redacted?: boolean;
}

export interface MemoryContext {
  memory: NodeBrief & {
    content: string;
    logical_id: string | null;
    parent_id: string | null;
    branch_type: string | null;
    actor_id: string | null;
    driver_id: string | null;
    tenant_id: string;
    domains: string[];
    content_type: string;
    source: string | null;
    metadata: Record<string, unknown> | null;
    created_at: string;
    updated_at: string;
    deleted_at: string | null;
    expires_at: string | null;
    embedding_dims: number | null;
    injection: 'full' | 'stub';
    full_content_threshold: number;
  };
  versions: {
    id: string;
    version: number;
    is_current: boolean;
    content: string;
    weight: number;
    actor_id: string | null;
    driver_id: string | null;
    owner_id: string;
    created_at: string;
    deleted_at: string | null;
  }[];
  branches: { id: string; branch_type: string | null; content: string; actor_id: string; created_at: string }[];
  relationships: {
    id: string;
    type: string;
    direction: 'in' | 'out';
    created_by: string;
    created_at: string;
    other: NodeBrief;
  }[];
  relationships_omitted: number;
  contradictions: {
    id: string;
    observed_behavior: string;
    confidence: number;
    reporter: string;
    created_at: string;
    resolved: boolean;
  }[];
  contradiction_threshold: number;
  similar: (NodeBrief & { score: number })[];
  access_count: number;
}

export interface GraphAll {
  nodes: (NodeBrief & { depth: number; degree: number; redacted?: boolean })[];
  edges: { id: string; source: string; target: string; type: string }[];
  total: number;
  truncated: boolean;
  orphan_count: number;
  hidden_links: number;
  metadata_only: boolean;
}

export interface GraphNeighbourhood {
  focus: (NodeBrief & { content: string; degree: number; hidden_degree: number }) | null;
  nodes: (NodeBrief & { depth: number })[];
  edges: { id: string; source: string; target: string; type: string }[];
  hidden_count: number;
  provenance: NodeBrief[];
  conflicts: NodeBrief[];
}

export interface SearchRequest {
  query: string;
  focus?: string;
  identity?: string;
  project?: string;
  mode: 'full' | 'index' | 'full_only';
  max_results: number;
  session_focus_weight: number;
  weight_threshold: number;
  max_response_tokens: number;
  current_only: boolean;
  include_branches: boolean;
  rerank: boolean;
}

export interface SearchResult {
  rank: number;
  id: string;
  scope: Scope;
  scope_id: string | null;
  owner_id: string;
  stub: string;
  content: string | null;
  delivery: 'full' | 'stub';
  weight: number;
  version: number;
  scores: {
    vector: number | null;
    text: number | null;
    focus: number | null;
    rrf: number;
    rerank: number | null;
    weight: number;
  };
  tokens: number;
}

export interface SearchResponse {
  run_as: string;
  retrieval: 'vector' | 'text';
  rerank: 'on' | 'off' | 'unavailable' | 'not_configured';
  stages: { name: string; ms: number; detail?: string }[];
  total_ms: number;
  candidates: number;
  omitted_count: number;
  weight_filtered: number;
  returned: number;
  full_count: number;
  stub_count: number;
  tokens: number;
  results: SearchResult[];
  assembled_context: string;
}

export interface ProjectRow {
  name: string;
  description: string | null;
  invite_only: boolean;
  created_at: string;
  created_by: string;
  memories: number;
  last_write: string | null;
  members: number;
  campaigns: { id: string; name: string; status: string }[];
}

export interface ProjectDetail extends ProjectRow {
  agents_today: number;
  members_list: { user_id: string; role: string; joined_at: string; joined_by: string }[];
  focus_history: { focus: string; count: number }[] | null;
  focus_available: boolean;
  loading_policy: string;
}

export interface CampaignRow {
  id: string;
  name: string;
  description: string | null;
  status: string;
  default_ttl_days: number | null;
  projects: string[];
  memories: number;
  created_at: string;
  updated_at: string;
}

export interface IdentityRow {
  id: string;
  name: string;
  type: 'user' | 'user agent' | 'service';
  scopes: string[];
  client: boolean;
  active: boolean;
  public: boolean;
  created_at: string | null;
  bound_to: string | null;
  roles: string[];
  memory_count: number;
  last_active: string | null;
  active_now: boolean;
}

export interface IdentitiesResponse {
  items: IdentityRow[];
  auth_available: boolean;
  roles: { name: string; members: number }[];
  counts: { all: number; users: number; user_agents: number; service_agents: number; roles: number };
}

export interface IdentityDetail extends IdentityRow {
  grant: string | null;
  scope_matrix: { tier: string; read: boolean; write: string }[] | null;
  activity_24h: { searches: number; writes: number; denied: number; total: number };
  hourly: { hour: string; count: number }[];
  agents: { id: string; name: string }[];
}

export interface ClientResponse {
  client_id: string;
  client_name: string;
  identity_type: 'user' | 'service';
  tenant_id: string;
  default_scopes: string[];
  active: boolean;
  public?: boolean;
  created_at: string;
  updated_at: string;
}

export interface CredentialRow extends ClientResponse {
  grant: string;
  last_used: string | null;
  status: 'new' | 'active' | 'stale' | 'revoked';
}

export interface CredentialsResponse {
  auth_available: boolean;
  issuer: string | null;
  clients: CredentialRow[];
  api_keys: CredentialRow[];
  issuers: { name: string; kind?: string; description?: string; status?: string }[];
}

export interface ClientCreatedResponse extends ClientResponse {
  client_secret: string;
  api_key: string | null;
}

export interface SecretRotatedResponse {
  client_id: string;
  client_secret: string;
  api_key: string | null;
}

export interface ApiKeyRotatedResponse {
  client_id: string;
  api_key: string;
}

export interface PublicConfig {
  mcp_url: string;
  auth_url: string;
}

export interface CreateClientPayload {
  client_id: string;
  client_name: string;
  identity_type: 'user' | 'service';
  tenant_id: string;
  default_scopes: string[];
}

export interface UpdateClientPayload {
  client_name?: string;
  active?: boolean;
  default_scopes?: string[];
}

export interface CurationRule {
  id: string;
  name: string;
  description: string | null;
  trigger: string;
  tier: 'regex' | 'embedding' | string;
  config: Record<string, unknown>;
  action: string;
  scope_filter: string | null;
  layer: 'system' | 'organizational' | 'user' | string;
  owner_id: string | null;
  override: boolean;
  enabled: boolean;
  priority: number;
  version: number;
  is_current: boolean;
  previous_version_id: string | null;
  edited_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface RulePayload {
  name?: string;
  description?: string | null;
  trigger?: string;
  tier?: string;
  config?: Record<string, unknown>;
  action?: string;
  scope_filter?: string | null;
  layer?: string;
  owner_id?: string | null;
  override?: boolean;
  enabled?: boolean;
  priority?: number;
  edited_by?: string;
}

export interface RuleTestResult {
  matched: boolean;
  action: string;
  matches?: { text?: string; col?: number; line?: number; name?: string; detail?: string }[];
  score?: number;
  threshold?: number;
}

export interface ReviewSummary {
  kind: 'approval' | 'promotion' | 'contradiction' | 'quarantine' | 'history';
  id: string;
  stub: string;
  content: string;
  scope: Scope;
  owner_id: string;
  actor_id: string | null;
  created_at: string;
  updated_at: string;
  metadata: Record<string, unknown>;
  sources?: number;
  reports?: number;
}

export interface ReviewQueue {
  tab: string;
  counts: { approvals: number; promotions: number; contradictions: number; quarantine: number };
  items: (ReviewSummary | (AuditEvent & { kind: 'history' }))[];
  also_waiting: ReviewSummary[];
  approvers_required: number;
  viewer: string;
}

export interface ReviewItem {
  kind: string;
  id: string;
  content: string;
  stub: string;
  scope: Scope;
  scope_id: string | null;
  owner_id: string;
  status: string;
  weight: number;
  version: number;
  requested_by: string;
  requested_at: string;
  reason: string | null;
  injection: 'full' | 'stub';
  branches: { id: string; branch_type: string | null; content: string }[];
  sources: { id: string; stub: string; scope: Scope; owner_id: string }[];
  checks: {
    scan_clean: boolean;
    scan_findings: string[];
    similar: { id: string; stub: string; score: number } | null;
    enterprise_this_month: number;
    has_provenance: boolean;
  };
  contradictions: { id: string; observed_behavior: string; confidence: number; reporter: string; created_at: string }[];
  contradiction_threshold: number;
  history: { event_type: string; actor_id: string; timestamp: string; comment: string | null }[];
  reach_agents: number;
  approvers_required: number;
}

export interface AuditResponse {
  items: AuditEvent[];
  total: number;
  page: number;
  per_page: number;
  event_types: string[];
  operations: Record<string, string>;
  denied_total: number;
  retention_days: number;
}

export interface ForensicsResponse {
  actor: string;
  at: string;
  window_minutes: number;
  events: number;
  last_search_at: string | null;
  context: {
    id: string;
    current_id: string;
    stub: string;
    content: string;
    scope: Scope;
    version_then: number;
    version_now: number;
    changed_after: boolean;
    changed_at: string | null;
    changed_by: string | null;
    deleted: boolean;
    served_at: string;
    delivery: 'full' | 'stub';
    tokens: number;
  }[];
  tokens: number;
  findings: (ForensicsResponse['context'][number] & { other_actors_exposed: number })[];
}

export interface TenantSettingsValues {
  retrieval: {
    max_results: number;
    max_response_tokens: number;
    session_focus_weight: number;
    full_content_threshold: number;
    rerank: boolean;
    cache_optimized_ordering: boolean;
    realtime_push: boolean;
  };
  governance: {
    project_isolation: boolean;
    role_isolation: boolean;
    contradiction_threshold: number;
    enterprise_approvers: number;
  };
  audit: {
    read_detail: 'full' | 'summary' | 'sample';
    full_retention_days: number;
    mcp_profile: 'compact' | 'full';
  };
  organization: { name: string; contact_email: string };
  signup: { mode: SignupMode; allowed_domains: string[]; require_approval: boolean };
  privacy: SetupPayload['privacy'];
  security: SetupPayload['security'];
}

export interface SettingsResponse {
  values: TenantSettingsValues;
  meta: { updated_at: string | null; updated_by: string | null; persisted: boolean };
  models: {
    embedding: Partial<HealthItem> & { url: string | null };
    reranker: Partial<HealthItem> & { url: string | null };
  };
  mcp: Partial<HealthItem> & { public_url: string; internal_url: string };
  tenant: { tenant_id: string; auth_url: string; cluster_domain: string | null };
}
