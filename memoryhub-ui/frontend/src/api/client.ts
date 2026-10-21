import type {
  AccountRow,
  AccountsResponse,
  GraphAll,
  InviteCreated,
  RegisterPayload,
  SetupPayload,
  ApiKeyRotatedResponse,
  AuditResponse,
  AuthStatus,
  BrowseParams,
  BrowseResponse,
  CampaignRow,
  ClientCreatedResponse,
  ClientResponse,
  CreateClientPayload,
  CredentialsResponse,
  CurationRule,
  ForensicsResponse,
  GraphNeighbourhood,
  IdentitiesResponse,
  IdentityDetail,
  Me,
  MemoryContext,
  NavCounts,
  Overview,
  ProjectDetail,
  ProjectRow,
  PublicConfig,
  ReviewItem,
  ReviewQueue,
  RulePayload,
  RuleTestResult,
  SearchRequest,
  SearchResponse,
  SecretRotatedResponse,
  SettingsResponse,
  TenantSettingsValues,
  UpdateClientPayload,
  AuditEvent,
  EntitiesResponse,
  EntityType,
  OverviewRange,
} from '@/types';

const BASE = '/api';

export class ApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(detail || `Request failed (${status})`);
    this.status = status;
    this.detail = detail;
  }
}

/** Fired when the BFF says the dev API-key session is missing or expired. */
export const AUTH_REQUIRED_EVENT = 'memoryhub:auth-required';

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      method,
      credentials: 'same-origin',
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, 'The dashboard backend is not reachable.');
  }
  if (!response.ok) {
    let detail = '';
    try {
      const data = await response.json();
      detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail ?? data);
    } catch {
      detail = await response.text().catch(() => '');
    }
    if (response.status === 401 && !path.startsWith('/auth/')) {
      window.dispatchEvent(new Event(AUTH_REQUIRED_EVENT));
    }
    throw new ApiError(response.status, detail || response.statusText);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

const get = <T,>(path: string) => request<T>('GET', path);
const post = <T,>(path: string, body: unknown = {}) => request<T>('POST', path, body);
const patch = <T,>(path: string, body: unknown) => request<T>('PATCH', path, body);
const put = <T,>(path: string, body: unknown) => request<T>('PUT', path, body);
const del = <T,>(path: string) => request<T>('DELETE', path);

const qs = (params: Record<string, unknown>): string => {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') return;
    if (Array.isArray(value)) value.forEach((v) => search.append(key, String(v)));
    else search.set(key, String(value));
  });
  const text = search.toString();
  return text ? `?${text}` : '';
};
const enc = encodeURIComponent;

export const api = {
  // Session
  me: () => get<Me>('/me'),
  authStatus: () => get<AuthStatus>('/auth/status'),
  login: (api_key: string, username?: string, role: 'admin' | 'user' = 'admin') =>
    post<{ ok: boolean; role: string }>('/auth/login', { api_key, username, role }),
  passwordLogin: (login: string, password: string) =>
    post<{ ok: boolean; role: string }>('/auth/login', { login, password }),
  setup: (body: SetupPayload) => post<{ ok: boolean; role: string }>('/auth/setup', body),
  register: (body: RegisterPayload) =>
    post<{ ok: boolean; role: string; status: 'active' | 'pending' }>('/auth/register', body),
  logout: () => post<{ ok: boolean }>('/auth/logout'),
  accounts: () => get<AccountsResponse>('/accounts'),
  updateAccount: (id: string, body: { status?: 'active' | 'disabled'; role?: 'admin' | 'user' }) =>
    patch<AccountRow>(`/accounts/${enc(id)}`, body),
  createInvite: (body: { role: 'admin' | 'user'; email?: string; note?: string; expires_days: number }) =>
    post<InviteCreated>('/invites', body),
  navCounts: () => get<NavCounts>('/nav-counts'),
  publicConfig: () => get<PublicConfig>('/public-config'),

  // Overview & memories
  overview: (range: OverviewRange = '7d') => get<Overview>(`/overview${qs({ range })}`),
  entities: (params: { q?: string; type?: EntityType | 'all'; project?: string; page?: number; per_page?: number } = {}) =>
    get<EntitiesResponse>(`/entities${qs(params)}`),
  browse: (params: BrowseParams) => get<BrowseResponse>(`/memories/browse${qs(params as Record<string, unknown>)}`),
  exportUrl: (scope?: string[]) => `${BASE}/memories/export${qs({ scope })}`,
  memoryContext: (id: string) => get<MemoryContext>(`/memory/${enc(id)}/context`),
  memoryAccessLog: (id: string, page = 1) =>
    get<{ total: number; items: AuditEvent[] }>(`/memory/${enc(id)}/access-log${qs({ page })}`),
  writeMemory: (body: {
    content: string;
    scope: string;
    weight: number;
    owner_id?: string;
    scope_id?: string;
    domains?: string[];
  }) => post<{ id: string }>('/memories', body),
  newVersion: (id: string, body: { content?: string; weight?: number; domains?: string[] }) =>
    post<{ id: string; version: number }>(`/memory/${enc(id)}/versions`, body),
  addBranch: (id: string, body: { branch_type: string; content: string }) =>
    post<{ id: string }>(`/memory/${enc(id)}/branches`, body),
  reportContradiction: (id: string, body: { observed_behavior: string; confidence: number }) =>
    post<{ unresolved: number }>(`/memory/${enc(id)}/contradictions`, body),
  createRelationship: (body: { source_id: string; target_id: string; relationship_type: string }) =>
    post<{ id: string }>('/relationships', body),
  deleteMemory: (id: string) => del<void>(`/memory/${enc(id)}`),
  bulk: (body: {
    action: 'weight' | 'delete' | 'promote' | 'relate';
    ids: string[];
    weight?: number;
    target_scope?: string;
    target_scope_id?: string;
    relationship_type?: string;
    target_id?: string;
  }) => post<{ done: string[]; errors: { id: string; detail: string }[] }>('/memories/bulk', body),
  promote: (id: string, target_scope: string) =>
    post<{ id: string }>(`/memory/${enc(id)}/promote${qs({ target_scope })}`),

  // Graph & search
  neighbourhood: (params: { focus?: string; depth?: number; types?: string[] }) =>
    get<GraphNeighbourhood>(`/graph/neighbourhood${qs(params)}`),
  graphAll: (params: { types?: string[]; include_superseded?: boolean; max_nodes?: number } = {}) =>
    get<GraphAll>(`/graph/all${qs(params)}`),
  search: (body: SearchRequest) => post<SearchResponse>('/search', body),

  // Projects & campaigns
  projects: (mine: boolean) =>
    get<{ items: ProjectRow[]; total: number; campaign_total: number; viewer: string }>(`/projects${qs({ mine })}`),
  project: (name: string) => get<ProjectDetail>(`/projects/${enc(name)}`),
  createProject: (body: { name: string; description?: string; invite_only: boolean }) =>
    post<{ name: string }>('/projects', body),
  updateProject: (name: string, body: { description?: string | null; invite_only?: boolean }) =>
    patch<{ name: string }>(`/projects/${enc(name)}`, body),
  addMember: (name: string, body: { user_id: string; role: string }) =>
    post<{ ok: boolean }>(`/projects/${enc(name)}/members`, body),
  removeMember: (name: string, userId: string) => del<void>(`/projects/${enc(name)}/members/${enc(userId)}`),
  campaigns: () => get<{ items: CampaignRow[] }>('/campaigns'),
  createCampaign: (body: { name: string; description?: string; default_ttl_days?: number; projects: string[] }) =>
    post<{ id: string }>('/campaigns', body),
  updateCampaign: (id: string, body: { description?: string; status?: string; default_ttl_days?: number | null }) =>
    patch<{ id: string }>(`/campaigns/${enc(id)}`, body),
  enrollProject: (id: string, project_id: string) => post<{ ok: boolean }>(`/campaigns/${enc(id)}/projects`, { project_id }),

  // Identities & credentials
  identities: () => get<IdentitiesResponse>('/identities'),
  identity: (id: string) => get<IdentityDetail>(`/identities/${enc(id)}`),
  assignRole: (user_id: string, role_name: string) => post<{ ok: boolean }>('/roles', { user_id, role_name }),
  credentials: () => get<CredentialsResponse>('/credentials'),
  createClient: (payload: CreateClientPayload) => post<ClientCreatedResponse>('/clients', payload),
  updateClient: (clientId: string, payload: UpdateClientPayload) =>
    patch<ClientResponse>(`/clients/${enc(clientId)}`, payload),
  rotateSecret: (clientId: string) => post<SecretRotatedResponse>(`/clients/${enc(clientId)}/rotate-secret`),
  rotateApiKey: (clientId: string) => post<ApiKeyRotatedResponse>(`/clients/${enc(clientId)}/rotate-api-key`),

  // Curation
  rules: () => get<CurationRule[]>('/rules'),
  createRule: (payload: RulePayload) => post<CurationRule>('/rules', payload),
  updateRule: (id: string, payload: RulePayload) => patch<CurationRule>(`/rules/${enc(id)}`, payload),
  deleteRule: (id: string) => del<void>(`/rules/${enc(id)}`),
  ruleMatches: () => get<{ days: number; available: boolean; counts: Record<string, number> }>('/rules/matches'),
  testRule: (body: {
    tier: string;
    sample: string;
    pattern?: string;
    pattern_set?: string;
    reference?: string;
    threshold?: number;
    action: string;
  }) => post<RuleTestResult>('/rules/test', body),
  batchScan: () => post<{ scanned: number; rules: number; matches: Record<string, number> }>('/rules/scan'),

  // Review
  reviewQueue: (tab: string) => get<ReviewQueue>(`/review${qs({ tab })}`),
  reviewItem: (id: string, kind: string) => get<ReviewItem>(`/review/${enc(id)}${qs({ kind })}`),
  reviewDecision: (id: string, decision: string, comment?: string) =>
    post<{ id: string; status: string }>(`/review/${enc(id)}/decision`, { decision, comment }),

  // Audit
  audit: (params: { operation?: string[]; decision?: string; actor?: string; hours?: number; page?: number; per_page?: number }) =>
    get<AuditResponse>(`/audit${qs(params)}`),
  forensics: (params: { actor: string; at: string; session_id?: string; window_minutes?: number }) =>
    get<ForensicsResponse>(`/audit/forensics${qs(params)}`),

  // Settings
  settings: () => get<SettingsResponse>('/settings'),
  saveSettings: (values: Partial<TenantSettingsValues>) => put<SettingsResponse>('/settings', values),
};
