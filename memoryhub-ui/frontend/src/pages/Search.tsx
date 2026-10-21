import React, { useEffect, useState } from 'react';
import { api } from '@/api/client';
import { PageHeader, Tabs, useApp } from '@/components/Shell';
import { copyText, Dropdown, Empty, ErrorState, Icon, ScopeLabel, Seg, SkLines, Standby, useToast } from '@/components/ui';
import type { HashRoute } from '@/hooks/useHashRoute';
import type { SearchRequest, SearchResponse } from '@/types';
import { fmtInt, truncate } from '@/utils/format';
import { memoryHref } from './common';

const ENTITY_ICON: Record<string, { color: string; d: string; label: string }> = {
  person: { label: 'Person', color: '#0066cc', d: 'M12 12a4 4 0 100-8 4 4 0 000 8zM4 21c1.5-4 4.5-6 8-6s6.5 2 8 6' },
  organization: { label: 'Organization', color: '#3d7317', d: 'M4 21V5l8-3v19M12 9h8v12M3 21h18' },
  location: { label: 'Location', color: '#009596', d: 'M12 21s-7-6.5-7-11a7 7 0 0114 0c0 4.5-7 11-7 11z' },
  object: { label: 'Object', color: '#ca6c0f', d: 'M12 2l9 5v10l-9 5-9-5V7zM3 7l9 5 9-5M12 12v10' },
  event: { label: 'Event', color: '#5e40be', d: 'M5 5h14a2 2 0 012 2v12a2 2 0 01-2 2H5a2 2 0 01-2-2V7a2 2 0 012-2zM3 10h18' },
};

const EntityGlyph: React.FC<{ type: string }> = ({ type }) => {
  const t = ENTITY_ICON[type] ?? ENTITY_ICON.object;
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={t.color} strokeWidth="2.2" aria-hidden="true" style={{ flex: 'none' }}>
      <path d={t.d} />
    </svg>
  );
};

/** Session focus picked from extracted entities instead of free text. */
const EntityFocusSelect: React.FC<{ value: string; onChange: (v: string) => void }> = ({ value, onChange }) => {
  const [q, setQ] = useState('');
  const [items, setItems] = useState<{ id: string; name: string; entity_type: string; memory_count: number }[] | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const t = window.setTimeout(() => {
      api
        .entities({ q: q.trim() || undefined, per_page: 30 })
        .then((r) => {
          setItems(r.items);
          setFailed(false);
        })
        .catch(() => setFailed(true));
    }, 200);
    return () => window.clearTimeout(t);
  }, [q]);
  const current = items?.find((i) => i.name === value);
  return (
    <Dropdown
      style={{ width: '100%' }}
      toggle={(p) => (
        <button type="button" className="sel" aria-labelledby="fo-l" aria-haspopup="listbox" style={{ width: '100%', justifyContent: 'space-between' }} {...p}>
          <span style={{ display: 'inline-flex', gap: 8, alignItems: 'center', minWidth: 0 }}>
            {value ? (
              <>
                <EntityGlyph type={current?.entity_type ?? 'object'} />
                <span className="ellipsis">{value}</span>
              </>
            ) : (
              <span className="muted">No session focus</span>
            )}
          </span>
          <Icon.Chevron />
        </button>
      )}
    >
      {(close) => (
        <div style={{ width: 340, maxWidth: '80vw' }} role="listbox" aria-label="Entities">
          <div style={{ padding: '6px 12px 8px' }}>
            <label className="vh" htmlFor="fo-q">
              Search entities
            </label>
            <input id="fo-q" className="in" autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search entities" />
          </div>
          <button
            type="button"
            role="option"
            aria-selected={!value}
            onClick={() => {
              onChange('');
              close();
            }}
          >
            <span className="muted">No session focus</span>
          </button>
          <hr />
          <div style={{ maxHeight: 280, overflowY: 'auto' }}>
            {failed && <div className="mh">Entities are not available.</div>}
            {!failed && items === null && <div className="mh">Loading…</div>}
            {items?.length === 0 && <div className="mh">No entities match.</div>}
            {items?.map((e) => (
              <button
                key={e.id}
                type="button"
                role="option"
                aria-selected={e.name === value}
                style={e.name === value ? { fontWeight: 600, background: '#f5f9fc' } : undefined}
                onClick={() => {
                  onChange(e.name);
                  close();
                }}
              >
                <span style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                  <EntityGlyph type={e.entity_type} />
                  <span className="ellipsis" style={{ flex: 1 }}>
                    {e.name}
                  </span>
                  <span className="hint">
                    {ENTITY_ICON[e.entity_type]?.label ?? e.entity_type} · {e.memory_count}
                  </span>
                </span>
              </button>
            ))}
          </div>
        </div>
      )}
    </Dropdown>
  );
};

const STAGE_COLORS: Record<string, string> = {
  embed: '#004d99',
  pgvector: '#0066cc',
  'full-text': '#0066cc',
  rbac: '#383838',
  focus: '#009596',
  rrf: '#5e40be',
  rerank: '#8f4700',
  assemble: '#3d7317',
};
const SAVED_KEY = 'memoryhub.search.saved';

interface Form extends SearchRequest {}

const sdkCall = (f: Form) =>
  [
    'results = await client.search(',
    `    ${JSON.stringify(f.query)},`,
    f.focus ? `    focus=${JSON.stringify(f.focus)},` : null,
    f.focus ? `    session_focus_weight=${f.session_focus_weight},` : null,
    f.project ? `    project_id=${JSON.stringify(f.project)},` : null,
    `    max_results=${f.max_results},`,
    `    mode=${JSON.stringify(f.mode)},`,
    f.weight_threshold ? `    weight_threshold=${f.weight_threshold},` : null,
    `    max_response_tokens=${f.max_response_tokens},`,
    f.include_branches ? '    include_branches=True,' : null,
    ')',
  ]
    .filter(Boolean)
    .join('\n');

const readSaved = (): Form[] => {
  try {
    return JSON.parse(window.localStorage.getItem(SAVED_KEY) || '[]') as Form[];
  } catch {
    return [];
  }
};

const Search: React.FC<{ route: HashRoute }> = ({ route }) => {
  const toast = useToast();
  const { standby, me } = useApp();
  const [form, setForm] = useState<Form>({
    query: route.params.get('q') || '',
    focus: '',
    identity: '',
    project: '',
    mode: 'full',
    max_results: 10,
    session_focus_weight: 0.4,
    weight_threshold: 0,
    max_response_tokens: 4000,
    current_only: true,
    include_branches: false,
    rerank: true,
  });
  const [identities, setIdentities] = useState<{ id: string; label: string }[]>([]);
  const [projects, setProjects] = useState<string[]>([]);
  const [result, setResult] = useState<SearchResponse | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [running, setRunning] = useState(false);
  const [tab, setTab] = useState<'ranked' | 'context' | 'json'>('ranked');
  const [saved, setSaved] = useState<Form[]>(readSaved);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    api
      .settings()
      .then((s) =>
        setForm((f) => ({
          ...f,
          max_results: s.values.retrieval.max_results,
          max_response_tokens: s.values.retrieval.max_response_tokens,
          session_focus_weight: s.values.retrieval.session_focus_weight,
          rerank: s.values.retrieval.rerank,
        })),
      )
      .catch(() => undefined);
    api
      .identities()
      .then((r) =>
        setIdentities(
          r.items.map((i) => ({
            id: i.id,
            label: `${i.name}${i.bound_to ? ` · ${i.bound_to}` : ''} (${i.type})`,
          })),
        ),
      )
      .catch(() => undefined);
    api
      .projects(false)
      .then((r) => setProjects(r.items.map((p) => p.name)))
      .catch(() => undefined);
  }, []);

  const set = <K extends keyof Form>(k: K, v: Form[K]) => setForm((f) => ({ ...f, [k]: v }));

  const run = async (override?: Form) => {
    const body = override ?? form;
    if (!body.query.trim()) return;
    setRunning(true);
    setError(null);
    setExpanded(false);
    try {
      setResult(
        await api.search({
          ...body,
          focus: body.focus?.trim() || undefined,
          identity: body.identity?.trim() || undefined,
          project: body.project?.trim() || undefined,
        }),
      );
    } catch (err) {
      setError(err instanceof Error ? err : new Error(String(err)));
    } finally {
      setRunning(false);
    }
  };

  const saveQuery = () => {
    const next = [form, ...saved.filter((s) => s.query !== form.query)].slice(0, 10);
    setSaved(next);
    try {
      window.localStorage.setItem(SAVED_KEY, JSON.stringify(next));
      toast('Query saved in this browser.');
    } catch {
      toast('Could not save in this browser.', 'err');
    }
  };

  const copySdk = async () => toast((await copyText(sdkCall(form))) ? 'SDK call copied.' : 'Copy failed — select the text instead.', 'ok');
  const results = result?.results ?? [];
  // Show every rank up to the last full result (at least five), then fold
  // the trailing stubs into the dashed "+N stub results" row.
  const lastFull = Math.max(5, ...results.filter((r) => r.delivery === 'full').map((r) => r.rank));
  const shownRanked = expanded ? results : results.filter((r) => r.rank <= lastFull);
  const hiddenStubs = results.length - shownRanked.length;
  const totalMs = result ? Math.max(1, result.stages.reduce((a, s) => a + s.ms, 0)) : 1;

  return (
    <>
      <PageHeader
        title="Search playground"
        description={
          <>
            Run <span className="mono">search_memory</span> exactly as an agent would — same RBAC, same two-vector retrieval — and see why each result ranked
            where it did.
          </>
        }
        actions={
          <>
            <Dropdown
              align="right"
              toggle={(p) => (
                <button type="button" className="btn bg" {...p}>
                  Saved queries
                </button>
              )}
            >
              {(close) => (
                <>
                  <button type="button" disabled={!form.query.trim()} onClick={() => { saveQuery(); close(); }}>
                    Save current query
                  </button>
                  {saved.length > 0 && <hr />}
                  {saved.map((s) => (
                    <button
                      key={s.query}
                      type="button"
                      onClick={() => {
                        setForm(s);
                        close();
                        void run(s);
                      }}
                    >
                      {truncate(s.query, 48)}
                    </button>
                  ))}
                  {saved.length === 0 && <div className="mh">No saved queries yet</div>}
                </>
              )}
            </Dropdown>
            <button type="button" className="btn bs" onClick={copySdk}>
              Copy as SDK call
            </button>
          </>
        }
      />
      <div className="body">
        <div className="row-wrap">
          <form
            className="card"
            style={{ flex: '1 1 320px', display: 'flex', flexDirection: 'column', gap: 18 }}
            onSubmit={(e) => {
              e.preventDefault();
              void run();
            }}
          >
            <h3>Query</h3>
            <div className="f">
              <label htmlFor="q">Query</label>
              <textarea id="q" className="in" required value={form.query} onChange={(e) => set('query', e.target.value)} placeholder="how should I run containers locally for this repo?" />
            </div>
            <div className="f">
              <span className="lbl-t" id="fo-l">
                Session focus
              </span>
              <EntityFocusSelect value={form.focus ?? ''} onChange={(v) => set('focus', v)} />
              <span className="hint">Pick an entity the session is about. Blended with the query via reciprocal-rank fusion.</span>
            </div>
            <div className="f">
              <label htmlFor="as">Run as identity</label>
              <input id="as" className="in" list="identity-list" value={form.identity} onChange={(e) => set('identity', e.target.value)} placeholder={`Dashboard (${me?.username ?? 'admin'}) — no RBAC filter`} />
              <datalist id="identity-list">
                {identities.map((i) => (
                  <option key={i.id} value={i.id}>
                    {i.label}
                  </option>
                ))}
              </datalist>
              <span className="hint">Results are filtered by this identity’s scopes and memberships.</span>
            </div>
            <div className="f">
              <label htmlFor="pj">Project context</label>
              <input id="pj" className="in" list="project-list" value={form.project} onChange={(e) => set('project', e.target.value)} />
              <datalist id="project-list">
                {projects.map((p) => (
                  <option key={p} value={p} />
                ))}
              </datalist>
            </div>
            <div className="f">
              <label>Response mode</label>
              <Seg
                label="Response mode"
                value={form.mode}
                onChange={(v) => set('mode', v)}
                options={[
                  { value: 'full', label: 'full' },
                  { value: 'index', label: 'index' },
                  { value: 'full_only', label: 'full_only' },
                ]}
              />
            </div>
            <div className="f">
              <label htmlFor="mr">Max results · {form.max_results}</label>
              <input id="mr" type="range" min={1} max={50} value={form.max_results} onChange={(e) => set('max_results', Number(e.target.value))} />
            </div>
            <div className="f">
              <label htmlFor="fw">Session focus weight · {form.session_focus_weight.toFixed(2)}</label>
              <input id="fw" type="range" min={0} max={100} value={Math.round(form.session_focus_weight * 100)} onChange={(e) => set('session_focus_weight', Number(e.target.value) / 100)} />
            </div>
            <div className="f">
              <label htmlFor="wt">Weight threshold · {form.weight_threshold.toFixed(2)}</label>
              <input id="wt" type="range" min={0} max={100} value={Math.round(form.weight_threshold * 100)} onChange={(e) => set('weight_threshold', Number(e.target.value) / 100)} />
            </div>
            <div className="f">
              <label htmlFor="tk">Max response tokens</label>
              <input id="tk" className="in" type="number" min={100} max={100000} value={form.max_response_tokens} onChange={(e) => set('max_response_tokens', Number(e.target.value) || 4000)} />
            </div>
            <label className="swrow">
              <span>Current versions only</span>
              <input type="checkbox" checked={form.current_only} onChange={(e) => set('current_only', e.target.checked)} />
            </label>
            <label className="swrow">
              <span>Include branches</span>
              <input type="checkbox" checked={form.include_branches} onChange={(e) => set('include_branches', e.target.checked)} />
            </label>
            <label className="swrow">
              <span>Cross-encoder rerank</span>
              <input type="checkbox" checked={form.rerank} onChange={(e) => set('rerank', e.target.checked)} />
            </label>
            <button type="submit" className="btn bp" style={{ justifyContent: 'center' }} disabled={running || standby || !form.query.trim()}>
              {running ? 'Running…' : 'Run search'}
            </button>
            {standby && <span className="hint">Searching starts once the tenant has its first memory.</span>}
          </form>

          <div className="col-main">
            {standby ? (
              <div className="card">
                <Standby compact={false}>
                  Pipeline timing, per-stage scores and the exact context an agent would receive appear here once there is something to search.
                </Standby>
              </div>
            ) : error ? (
              <div className="card">
                <ErrorState error={error} onRetry={() => void run()} title="The search failed" />
              </div>
            ) : !result && !running ? (
              <div className="card">
                <Empty title="Run a search">
                  Enter a query and choose who to run it as. You will see each pipeline stage’s timing and why every result ranked where it did.
                </Empty>
              </div>
            ) : running && !result ? (
              <>
                <div className="card">
                  <SkLines lines={2} />
                </div>
                <div className="card">
                  <SkLines lines={8} />
                </div>
              </>
            ) : (
              result && (
                <>
                  <div className="card">
                    <div className="ch">
                      <h3>Pipeline · {Math.round(result.total_ms)} ms</h3>
                      <span className="sm muted">
                        {result.returned} returned · {result.full_count} full · {result.stub_count} stubs · {fmtInt(result.tokens)} tokens ·{' '}
                        {result.omitted_count} filtered by RBAC
                      </span>
                    </div>
                    <div className="pipe" role="img" aria-label={result.stages.map((s) => `${s.name} ${s.ms} ms`).join(', ')}>
                      {result.stages.map((s) => (
                        <div key={s.name} style={{ flex: Math.max(s.ms, 0.5), background: STAGE_COLORS[s.name] ?? '#8a8d90' }} title={`${s.name} ${s.ms} ms`}>
                          {s.ms / totalMs > 0.12 ? `${s.name} · ${Math.round(s.ms)} ms` : s.ms / totalMs > 0.06 ? s.name : ''}
                        </div>
                      ))}
                    </div>
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 14, fontSize: 12, marginTop: 10 }} className="muted">
                      {result.stages.map((s) => (
                        <span key={s.name}>
                          {s.name}
                          {s.detail ? ` ${s.detail}` : ''} {s.ms < 1 ? '<1' : Math.round(s.ms)} ms
                        </span>
                      ))}
                      {result.rerank === 'not_configured' && <span>rerank not configured</span>}
                      {result.rerank === 'unavailable' && <span className="warn">reranker unavailable</span>}
                      <span>run as {result.run_as}</span>
                    </div>
                  </div>

                  <div className="card">
                    <Tabs
                      inCard
                      label="Result views"
                      value={tab}
                      onChange={setTab}
                      tabs={[
                        { value: 'ranked', label: 'Ranked results' },
                        { value: 'context', label: 'Assembled context' },
                        { value: 'json', label: 'Raw JSON' },
                      ]}
                    />
                    {tab === 'ranked' &&
                      (results.length === 0 ? (
                        <Empty compact title="No results">
                          {result.omitted_count > 0
                            ? `${result.omitted_count} candidate${result.omitted_count === 1 ? '' : 's'} matched but ${result.omitted_count === 1 ? 'is' : 'are'} outside this identity’s access.`
                            : 'Nothing matched this query. Try fewer words or a lower weight threshold.'}
                        </Empty>
                      ) : (
                        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                          {shownRanked.map((r) => (
                            <div className="res" key={r.id}>
                              <span className="rk">{r.rank}</span>
                              <div style={{ flex: 1, minWidth: 0 }}>
                                <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginBottom: 6 }}>
                                  <ScopeLabel scope={r.scope} />
                                  <span className="lbl g">{expanded ? 'full' : r.delivery}</span>
                                  <span className="sm muted">{r.scope === 'campaign' ? `campaign:${r.scope_id ?? r.owner_id}` : r.scope_id ?? r.owner_id}</span>
                                </div>
                                <a href={memoryHref(r.id)} style={{ fontWeight: 500, textDecoration: 'none', color: '#151515' }}>
                                  {r.delivery === 'full' && r.content ? r.content : truncate(r.stub, 80)}
                                </a>
                                <div className="sc">
                                  {r.scores.vector !== null && (
                                    <span>
                                      vector <b>{r.scores.vector.toFixed(2)}</b>
                                    </span>
                                  )}
                                  {r.scores.text !== null && (
                                    <span>
                                      text <b>{r.scores.text.toFixed(2)}</b>
                                    </span>
                                  )}
                                  {r.scores.focus !== null && (
                                    <span>
                                      focus <b>{r.scores.focus.toFixed(2)}</b>
                                    </span>
                                  )}
                                  <span>
                                    RRF <b>{r.scores.rrf.toFixed(4)}</b>
                                  </span>
                                  {r.scores.rerank !== null && (
                                    <span>
                                      rerank <b>{r.scores.rerank.toFixed(2)}</b>
                                    </span>
                                  )}
                                  <span>
                                    weight <b>{r.scores.weight.toFixed(2)}</b>
                                  </span>
                                  <span>
                                    tokens <b>{r.tokens}</b>
                                  </span>
                                </div>
                              </div>
                            </div>
                          ))}
                          {hiddenStubs > 0 && (
                            <div className="dashed muted">
                              + {hiddenStubs} stub result{hiddenStubs === 1 ? '' : 's'} (id, scope and first 80 characters) —{' '}
                              <button type="button" className="link-btn" onClick={() => setExpanded(true)}>
                                expand
                              </button>{' '}
                              to load full content with <span className="mono">read_memory</span>.
                            </div>
                          )}
                        </div>
                      ))}
                    {tab === 'context' && <pre>{result.assembled_context || '(empty)'}</pre>}
                    {tab === 'json' && <pre className="small">{JSON.stringify(result, null, 2)}</pre>}
                  </div>

                  <div className="card">
                    <div className="ch">
                      <h3>Equivalent SDK call</h3>
                      <button type="button" className="btn bl sm" onClick={copySdk}>
                        Copy
                      </button>
                    </div>
                    <pre className="dark">{sdkCall(form)}</pre>
                  </div>
                </>
              )
            )}
          </div>
        </div>
      </div>
    </>
  );
};

export default Search;

