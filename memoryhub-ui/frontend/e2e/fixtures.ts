import type { Page, Route } from '@playwright/test';

const now = '2026-10-19T12:00:00.000Z';
const memoryId = '11111111-1111-4111-8111-111111111111';
const otherMemoryId = '22222222-2222-4222-8222-222222222222';
const entityId = '33333333-3333-4333-8333-333333333333';
// range -> [bucket count, bucket seconds], mirroring ACTIVITY_RANGES in routes_dashboard.py.
const activityBuckets: Record<string, [number, number]> = {
  all: [14, 86400],
  '1h': [12, 300],
  '12h': [24, 1800],
  '1d': [24, 3600],
  '7d': [28, 21600],
  '30d': [30, 86400],
};
const activityFor = (range: string) => {
  const [count, bucket] = activityBuckets[range] ?? activityBuckets['7d'];
  const end = Date.parse(now);
  return Array.from({ length: count }, (_, index) => {
    const date = new Date(Math.floor((end - (count - 1 - index) * bucket * 1000) / (bucket * 1000)) * bucket * 1000).toISOString();
    const last = index === count - 1;
    return { date, searches: last ? 2 : 0, writes: last ? 1 : 0 };
  });
};

const node = (id: string, stub: string, depth: number) => ({
  id,
  stub,
  scope: 'project',
  scope_id: 'memoryhub-ui',
  owner_id: 'admin',
  weight: 0.8,
  version: 1,
  is_current: true,
  status: 'active',
  created_at: now,
  updated_at: now,
  depth,
});

const settings = {
  values: {
    retrieval: {
      max_results: 10,
      max_response_tokens: 4000,
      session_focus_weight: 0.4,
      full_content_threshold: 0.8,
      rerank: true,
      cache_optimized_ordering: true,
      realtime_push: true,
    },
    governance: { project_isolation: true, role_isolation: true, contradiction_threshold: 3, enterprise_approvers: 2 },
    audit: { read_detail: 'full', full_retention_days: 30, mcp_profile: 'full' },
  },
  meta: { updated_at: now, updated_by: 'admin', persisted: true },
  models: {
    embedding: { name: 'Embedding', status: 'healthy', detail: 'ready', latency_ms: 12, url: 'http://embedding.test/embed' },
    reranker: { name: 'Reranker', status: 'healthy', detail: 'ready', latency_ms: 18, url: 'http://reranker.test/rerank' },
  },
  mcp: { name: 'MCP', status: 'healthy', detail: 'ready', latency_ms: 4, public_url: 'https://mcp.test', internal_url: 'http://mcp.test' },
  tenant: { tenant_id: 'demo-tenant', auth_url: 'https://auth.test', cluster_domain: 'cluster.test' },
};

const commonMemory = {
  id: memoryId,
  stub: 'Run the MemoryHub UI locally with npm run dev',
  content_preview: 'Run the MemoryHub UI locally with npm run dev.',
  scope: 'project',
  scope_id: 'memoryhub-ui',
  owner_id: 'admin',
  actor_id: 'admin',
  driver_id: 'dashboard',
  domains: ['tooling', 'containers'],
  weight: 0.8,
  version: 1,
  is_current: true,
  status: 'active',
  content_type: 'text',
  branch_count: 0,
  branch_summary: [],
  open_contradictions: 0,
  near_duplicate: null,
  created_at: now,
  updated_at: now,
  deleted_at: null,
  score: null,
};

const overview = {
  tenant_id: 'demo-tenant',
  generated_at: now,
  memories: { current: 1, new_7d: 1, growth_pct: null },
  agents: { total: 1, active_now: 1, source: 'auth' },
  searches: { last_24h: 2, p95_latency_ms: 42 },
  contradictions: { open: 0, over_threshold: 0, threshold: 3 },
  approvals: { enterprise_pending: 0, pending_total: 0 },
  scopes: ['user', 'project', 'campaign', 'role', 'organizational', 'enterprise'].map((scope) => ({ scope, count: scope === 'project' ? 1 : 0, pending: 0 })),
  categories: [{ name: 'tooling', memory_count: 1 }, { name: 'containers', memory_count: 1 }],
  activity: activityFor('7d'),
  activity_range: '7d',
  activity_bucket_seconds: 21600,
  activity_source: 'memory',
  recent: [],
  recent_source: 'memory',
  health: [{ key: 'db', name: 'Database', status: 'healthy', detail: 'Ready', latency_ms: 4 }],
  context_assembly: { avg_tokens: 128, prefix_hit_rate: 0.5 },
};

const identity = {
  id: 'admin',
  name: 'Admin',
  type: 'user',
  scopes: ['memory:admin'],
  client: false,
  active: true,
  public: false,
  created_at: now,
  bound_to: null,
  roles: ['admin'],
  memory_count: 1,
  last_active: now,
  active_now: true,
};

const client = {
  client_id: 'demo-agent',
  client_name: 'Demo agent',
  identity_type: 'service',
  tenant_id: 'demo-tenant',
  default_scopes: ['memory:read', 'memory:write:user'],
  active: true,
  public: false,
  grant: 'client_credentials',
  last_used: now,
  status: 'active',
  created_at: now,
  updated_at: now,
};

const entities = {
  items: [{ id: entityId, name: 'MemoryHub UI', entity_type: 'object', aliases: ['memoryhub-ui'], memory_count: 1, projects: ['memoryhub-ui'], created_at: now, last_seen: now }],
  total: 1,
  page: 1,
  per_page: 20,
  has_more: false,
  counts: { all: 1, person: 0, organization: 0, location: 0, object: 1, event: 0 },
};

const respond = async (route: Route, body: unknown, status = 200) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

/**
 * Deterministic API fixture for UI smoke tests. It deliberately exercises the
 * real React screens while keeping tests independent from tenant data and auth.
 */
export async function mockDashboardApi(page: Page) {
  await page.route('**/*', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (!url.pathname.startsWith('/api/')) return route.continue();
    const path = url.pathname.replace(/^\/api/, '');

    if (path === '/auth/status') return respond(route, { mode: 'api-key', authenticated: true, api_key_login: true, user: 'admin', cluster_domain: 'cluster.test' });
    if (path === '/me') return respond(route, { username: 'admin', name: 'Admin User', email: 'admin@example.test', initials: 'AU', source: 'api-key', tenant_id: 'demo-tenant' });
    if (path === '/nav-counts') return respond(route, { review_queue: 0, memories: 1 });
    if (path === '/overview') {
      const range = url.searchParams.get('range') ?? '7d';
      const bucket = (activityBuckets[range] ?? activityBuckets['7d'])[1];
      return respond(route, { ...overview, activity: activityFor(range), activity_range: range, activity_bucket_seconds: bucket });
    }
    if (path === '/entities') return respond(route, entities);
    if (path === '/settings') return respond(route, settings);
    if (path === '/public-config') return respond(route, { mcp_url: 'https://mcp.test', auth_url: 'https://auth.test' });
    if (path === '/identities') return respond(route, { items: [identity], auth_available: true, roles: [{ name: 'admin', members: 1 }], counts: { all: 1, users: 1, user_agents: 0, service_agents: 0, roles: 1 } });
    if (path.startsWith('/identities/')) return respond(route, { ...identity, grant: 'password', scope_matrix: [], activity_24h: { searches: 1, writes: 1, denied: 0, total: 2 }, hourly: [], agents: [] });
    if (path === '/memories/browse') return respond(route, { items: [commonMemory], total: 1, page: 1, per_page: 20, search_mode: 'none', tab_counts: { current: 1, all: 1, quarantined: 0, deleted: 0 }, facets: { scopes: ['user', 'project', 'campaign', 'role', 'organizational', 'enterprise'].map((value) => ({ value, count: value === 'project' ? 1 : 0 })), writers: [{ value: 'admin', count: 1 }], projects: [{ value: 'memoryhub-ui', count: 1 }], domains: [{ value: 'tooling', count: 1 }], flags: { contradicted: 0, has_branches: 0, near_duplicate: null } } });
    if (path === '/graph/all') return respond(route, { nodes: [{ ...node(memoryId, 'Run the MemoryHub UI locally with npm run dev', 0), degree: 1 }, { ...node(otherMemoryId, 'The UI is served on port 3000', 0), degree: 1 }], edges: [{ id: 'rel-1', source: memoryId, target: otherMemoryId, type: 'related_to' }], total: 2, truncated: false, orphan_count: 0, hidden_links: 0, metadata_only: false });
    if (path === '/graph/neighbourhood') return respond(route, { focus: { ...node(memoryId, 'Run the MemoryHub UI locally with npm run dev', 0), content: commonMemory.content_preview, degree: 1, hidden_degree: 0 }, nodes: [node(memoryId, commonMemory.stub, 0), node(otherMemoryId, 'The UI is served on port 3000', 1)], edges: [{ id: 'rel-1', source: memoryId, target: otherMemoryId, type: 'related_to' }], hidden_count: 0, provenance: [], conflicts: [] });
    if (path === '/credentials') return respond(route, { auth_available: true, issuer: 'https://auth.test', clients: [client], api_keys: [], issuers: [{ name: 'OpenShift', kind: 'oauth-proxy', description: 'Cluster issuer', status: 'active' }] });
    if (path === '/projects') return respond(route, { items: [{ name: 'memoryhub-ui', description: 'UI project', invite_only: false, created_at: now, created_by: 'admin', memories: 1, last_write: now, members: 1, campaigns: [] }], total: 1, campaign_total: 0, viewer: 'admin' });
    if (path === '/campaigns') return respond(route, { items: [] });
    if (path.startsWith('/projects/')) return respond(route, { name: 'memoryhub-ui', description: 'UI project', invite_only: false, created_at: now, created_by: 'admin', memories: 1, last_write: now, members: 1, campaigns: [], agents_today: 1, members_list: [], focus_history: [], focus_available: true, loading_policy: 'default' });
    if (path === '/rules') return respond(route, []);
    if (path === '/rules/matches') return respond(route, { days: 7, available: true, counts: {} });
    if (path === '/review') return respond(route, { tab: 'approvals', counts: { approvals: 0, promotions: 0, contradictions: 0, quarantine: 0 }, items: [], also_waiting: [], approvers_required: 2, viewer: 'admin' });
    if (path === '/audit') return respond(route, { items: [], total: 0, page: 1, per_page: 50, event_types: [], operations: {}, denied_total: 0, retention_days: 30 });
    if (path === '/audit/forensics') return respond(route, { actor: 'admin', at: now, window_minutes: 60, events: 0, last_search_at: null, context: [], tokens: 0, findings: [] });

    if (request.method() === 'POST' && path === '/memories') return respond(route, { id: otherMemoryId }, 201);
    if (request.method() === 'POST' && path === '/clients') {
      const body = JSON.parse(request.postData() || '{}') as { client_id?: string; client_name?: string };
      return respond(route, { ...client, client_id: body.client_id || client.client_id, client_name: body.client_name || 'Created agent', client_secret: 'secret-for-test', api_key: 'key-for-test' }, 201);
    }
    if (request.method() === 'POST' && path === '/auth/logout') return respond(route, { ok: true });
    if (request.method() === 'POST' && path === '/search') return respond(route, { run_as: 'admin', retrieval: 'text', rerank: 'off', stages: [], total_ms: 1, candidates: 0, omitted_count: 0, weight_filtered: 0, returned: 0, full_count: 0, stub_count: 0, tokens: 0, results: [], assembled_context: '' });
    if (request.method() === 'POST' && path.startsWith('/rules/')) return respond(route, { matched: false, action: 'none', scanned: 1, rules: 0, matches: {} });
    if (request.method() === 'POST' && path.startsWith('/review/')) return respond(route, { id: 'review-1', status: 'approved' });
    if (request.method() === 'POST' && path === '/relationships') return respond(route, { id: 'rel-2' }, 201);
    if (request.method() === 'PUT' && path === '/settings') return respond(route, settings);
    if (request.method() === 'PATCH' || request.method() === 'DELETE') return respond(route, request.method() === 'DELETE' ? null : client);

    return respond(route, {});
  });
}

export { memoryId };
