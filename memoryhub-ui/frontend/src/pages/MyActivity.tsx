import React, { useState } from 'react';
import { api, type AuditParams } from '@/api/client';
import { Hub, useApp } from '@/components/Shell';
import { Empty, ErrorState, Icon, Pager, ScopeLabel, Sk, SkLines, SkTable, useToast } from '@/components/ui';
import { useApi } from '@/hooks/useApi';
import { buildHash, navigate, type HashRoute } from '@/hooks/useHashRoute';
import type { AuditEvent, MyReads } from '@/types';
import { errorMessage, fmtInt, fmtMonthDay, fmtTime } from '@/utils/format';
import Operations from './Operations';
import WhoRead from './WhoRead';
import { memoryHref } from './common';

type View = 'log' | 'reads' | 'jobs' | 'incidents';

/**
 * My activity (user, #718): a personal projection of events about the viewer
 * and their data — their and their agents' changes, who read their memories,
 * background jobs on them and incident notices. Filters and counts come from
 * the BFF (GET /api/audit), so the page and the API give the same answer.
 */
const MyActivity: React.FC<{ route: HashRoute }> = ({ route }) => {
  const toast = useToast();
  const reads = useApi<MyReads>(() => api.myReads(90, 'others'), []);
  // Old links (#/audit?tab=denied, ?view=denied) open the log filtered to denied operations.
  const legacyDenied = route.params.get('tab') === 'denied' || route.params.get('view') === 'denied';
  const hubRoute = legacyDenied ? { ...route, params: new URLSearchParams({ decision: 'denied' }) } : route;
  const incidents = reads.data ? reads.data.summary?.incident_grants ?? 0 : null;

  const exportMine = async () => {
    try {
      const all: AuditEvent[] = [];
      for (let page = 1; page <= 25; page += 1) {
        const res = await api.audit({ hours: 24 * 90, page, per_page: 200 });
        all.push(...res.items);
        if (all.length >= res.total || res.items.length === 0) break;
      }
      const blob = new Blob([all.map((e) => JSON.stringify(e)).join('\n')], { type: 'application/x-ndjson' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `memoryhub-my-activity-${new Date().toISOString().slice(0, 10)}.jsonl`;
      a.click();
      URL.revokeObjectURL(a.href);
      toast(`Exported ${fmtInt(all.length)} events.`);
    } catch (e) {
      toast(errorMessage(e), 'err');
    }
  };

  const views: { value: View; label: string; count?: number | null; render: () => React.ReactNode }[] = [
    { value: 'log', label: 'Activity log', render: () => <ActivityLog route={hubRoute} /> },
    { value: 'reads', label: 'Who read my memories', render: () => <WhoRead route={route} /> },
    { value: 'jobs', label: 'Background jobs', render: () => <Operations /> },
    { value: 'incidents', label: 'Incident notices', count: incidents, render: () => <IncidentNotices data={reads.data} error={reads.error} reload={reads.reload} /> },
  ];

  return (
    <Hub
      title="My activity"
      description="A personal projection of events about you and your data: your and your agents’ changes, reads of your personal and project records, and background jobs. Not the tenant audit and not a raw log of other people."
      actions={
        <button type="button" className="btn bs" onClick={exportMine}>
          Export
        </button>
      }
      route={hubRoute}
      fallback="log"
      views={views}
    />
  );
};

/** Incident (break-glass) reads of your memories. MemoryHub has no incident access yet, so this stays empty. */
const IncidentNotices: React.FC<{ data: MyReads | null; error: Error | null; reload: () => void }> = ({ data, error, reload }) => {
  const events = (data?.events ?? []).filter((e) => e.break_glass);
  return (
    <div className="body">
      <section className="card">
        <div className="ch">
          <h2 data-h="3">Incident notices</h2>
          <span className="sm muted">Last 90 days</span>
        </div>
        {error && !data ? (
          <ErrorState error={error} onRetry={reload} compact />
        ) : !data ? (
          <SkLines lines={3} />
        ) : events.length === 0 ? (
          <Empty compact title="No incident access to your memories">
            Nobody can open your personal memories outside the normal rules. If incident access is ever added and used on yours, each read is listed here with its incident number.
          </Empty>
        ) : (
          events.map((e) => (
            <div key={String(e.id)} className="evrow">
              <span className="mono muted">{fmtMonthDay(e.timestamp)} {fmtTime(e.timestamp)}</span>
              <span style={{ flex: 1, minWidth: 0 }}>
                <b>{e.actor_id}</b> opened <a href={memoryHref(e.memory_id)}>{e.memory_stub}</a> under incident access
              </span>
              <span className="lbl red">break-glass</span>
            </div>
          ))
        )}
      </section>
    </div>
  );
};

/* ------------------------------------------------------------------ */
/* Activity log: what you and your agents changed, and what was denied */
/* ------------------------------------------------------------------ */

const OP_NAME: Record<string, string> = {
  'memory.write': 'write_memory',
  'memory.update': 'update_memory',
  'memory.read': 'read_memory',
  'memory.search': 'search_memory',
  'memory.delete': 'delete_memory',
  'memory.restore': 'restore_memory',
  'memory.relationship_created': 'create_relationship',
  'memory.contradiction_reported': 'report_contradiction',
  'admin.quarantine': 'quarantine',
  'admin.restore': 'restore',
  'job.retry': 'retry_job',
};
const opName = (op: string) => OP_NAME[op] ?? op.replace(/^memory\./, '');
const READ_OPS = new Set(['memory.read', 'memory.search']);

const PERIODS = [
  { value: '24', label: 'Last 24 hours', short: '24 h' },
  { value: '168', label: 'Last 7 days', short: '7 d' },
  { value: '720', label: 'Last 30 days', short: '30 d' },
  { value: '2160', label: 'Last 90 days', short: '90 d' },
];

const RULE_LABEL: Record<string, string> = { similarity_gate: 'near duplicate', curation_rule: 'curation rule' };
const ruleOf = (e: AuditEvent) => {
  const m = e.metadata || {};
  return (m.rule as string) || (m.reason as string) || '';
};

const ActivityLog: React.FC<{ route: HashRoute }> = ({ route }) => {
  const { me } = useApp();
  const p = route.params;
  const hours = p.get('hours') ?? '168';
  const actorF = (p.get('actor') ?? 'all') as NonNullable<AuditParams['actor']>;
  const opF = p.get('op') ?? '';
  const scopeF = (p.get('scope') ?? 'all') as NonNullable<AuditParams['scope']>;
  const decisionF = (p.get('decision') ?? '') as NonNullable<AuditParams['decision']>;
  const q = p.get('q') ?? '';
  const [text, setText] = useState(q);
  const [page, setPage] = useState(1);
  const perPage = 25;
  const period = PERIODS.find((x) => x.value === hours) ?? PERIODS[1];
  const set = (k: string, v: string | undefined) => {
    const next = new URLSearchParams(p);
    if (v) next.set(k, v);
    else next.delete(k);
    setPage(1);
    navigate(route.path, next, { replace: true });
  };

  const { data, error, loading, reload } = useApi(
    () =>
      api.audit({
        hours: Number(hours),
        actor: actorF,
        operation: opF ? [opF] : undefined,
        scope: scopeF,
        decision: decisionF,
        q: q || undefined,
        page,
        per_page: perPage,
      }),
    [hours, actorF, opF, scopeF, decisionF, q, page],
  );

  const s = data?.summary;
  const agents = new Set(data?.agents ?? []);
  const isMe = (e: AuditEvent) => e.actor_id === me?.username;
  const isMyAgent = (e: AuditEvent) => agents.has(e.actor_id);
  const maxActor = Math.max(1, ...(s?.by_actor ?? []).map((a) => a.count));
  const filtered = Boolean(q || actorF !== 'all' || opF || scopeF !== 'all' || decisionF);

  return (
    <div className="body">
      <form
        className="filters"
        onSubmit={(e) => {
          e.preventDefault();
          set('q', text.trim() || undefined);
        }}
      >
        <span className="in-icon">
          <Icon.Search size={15} />
          <label htmlFor="al-q" className="vh">
            Filter by memory, search text or ID
          </label>
          <input id="al-q" className="in" placeholder="Filter by memory, search text or ID" value={text} onChange={(e) => setText(e.target.value)} onBlur={() => set('q', text.trim() || undefined)} />
        </span>
        <FilterSelect id="al-period" label="Period" value={hours} onChange={(v) => set('hours', v === '168' ? undefined : v)} options={PERIODS.map((x) => [x.value, x.label])} />
        <FilterSelect
          id="al-actor"
          label="Actor"
          value={actorF}
          onChange={(v) => set('actor', v === 'all' ? undefined : v)}
          options={[
            ['all', 'Actor: me + my agents'],
            ['me', 'Actor: me'],
            ['agents', 'Actor: my agents'],
          ]}
        />
        <FilterSelect
          id="al-op"
          label="Operation"
          value={opF}
          onChange={(v) => set('op', v || undefined)}
          options={[['', 'Operation: all'], ...[...new Set([...(data?.event_types ?? []), ...(opF ? [opF] : [])])].sort().map((o) => [o, opName(o)] as [string, string])]}
        />
        <FilterSelect
          id="al-scope"
          label="Scope"
          value={scopeF}
          onChange={(v) => set('scope', v === 'all' ? undefined : v)}
          options={[
            ['all', 'Scope: personal + projects'],
            ['personal', 'Scope: personal'],
            ['projects', 'Scope: my projects'],
          ]}
        />
        <FilterSelect
          id="al-dec"
          label="Decision"
          value={decisionF}
          onChange={(v) => set('decision', v || undefined)}
          options={[
            ['', 'Decision: all'],
            ['permitted', 'Decision: permitted'],
            ['denied', 'Decision: denied'],
          ]}
        />
      </form>

      {error && !data ? (
        <div className="card">
          <ErrorState error={error} onRetry={reload} />
        </div>
      ) : (
        <>
          <div className="grid-kpi">
            <KpiTile label={`Changes by me · ${period.short}`} value={s ? s.changes_by_me : null} sub="dashboard edits, versions, relations" />
            <KpiTile
              label={`Writes by my agents · ${period.short}`}
              value={s ? s.agent_writes : null}
              sub={s ? `${s.agents_writing} agent${s.agents_writing === 1 ? '' : 's'} · ${s.agent_writes_personal} personal, ${s.agent_writes_project} project` : ''}
            />
            <KpiTile label="Blocked by policy" value={s ? s.blocked : null} sub="nothing was stored" />
            <KpiTile label="Deleted · restorable" value={s ? s.deleted_restorable : null} sub="memories you or your agents wrote" href={buildHash('/memories', { tab: 'deleted' })} />
          </div>

          <div className="row-wrap">
            <section className="card" style={{ flex: '999 1 620px', minWidth: 0, padding: '8px 12px 4px' }}>
              {loading && !data ? (
                <SkTable rows={6} cols={7} />
              ) : !data || data.items.length === 0 ? (
                <Empty compact title="Nothing in this period">
                  {filtered ? 'No events match these filters.' : 'Changes you and your agents make show up here.'}
                </Empty>
              ) : (
                <>
                  <div className="scroll">
                    <table className="tbl" style={{ minWidth: 680 }}>
                      <thead>
                        <tr>
                          <th>Time</th>
                          <th>Actor</th>
                          <th>Operation</th>
                          <th>Memory</th>
                          <th>Scope</th>
                          <th>Change</th>
                          <th>Decision</th>
                        </tr>
                      </thead>
                      <tbody>
                        {data.items.map((e) => (
                          <tr key={`${e.id}-${e.timestamp}`}>
                            <td className="mono" style={{ whiteSpace: 'nowrap' }}>
                              {new Date(e.timestamp).toDateString() === new Date().toDateString() ? (
                                fmtTime(e.timestamp).slice(0, 5)
                              ) : (
                                <>
                                  {fmtMonthDay(e.timestamp)}
                                  <div className="hint mono">{fmtTime(e.timestamp).slice(0, 5)}</div>
                                </>
                              )}
                            </td>
                            <td style={{ maxWidth: 160, whiteSpace: 'nowrap' }}>
                              {isMe(e) ? me?.name || e.actor_id : e.actor_id}
                              <div className="hint">{isMe(e) ? 'me · dashboard' : isMyAgent(e) ? 'my agent' : e.driver_id === me?.username ? `on behalf of me` : ''}</div>
                            </td>
                            <td className="mono sm">{opName(e.operation)}</td>
                            <td style={{ maxWidth: 150 }}>
                              {e.memory_id ? (
                                <a className="ellipsis" style={{ display: 'block' }} href={memoryHref(e.memory_id)} title={e.memory_stub ?? undefined}>
                                  {e.memory_stub ?? 'Memory you can no longer read'}
                                </a>
                              ) : e.operation === 'memory.search' ? (
                                <span className="sm">“{String(e.metadata?.query ?? 'search')}”</span>
                              ) : (
                                <span className="sm muted">{e.decision === 'denied' ? 'not stored' : '—'}</span>
                              )}
                            </td>
                            <td>{e.scope && e.scope !== 'session' && e.scope !== 'all' ? <ScopeLabel scope={e.scope}>{e.scope === 'user' ? 'Personal' : undefined}</ScopeLabel> : <span className="muted">—</span>}</td>
                            <td className="sm">
                              <ChangeCell e={e} />
                            </td>
                            <td className="sm">
                              {e.decision === 'denied' ? (
                                <>
                                  <span className="err">● Denied</span>
                                  {ruleOf(e) && <div className="hint" style={{ wordBreak: 'break-word' }}>{ruleOf(e)}</div>}
                                </>
                              ) : (
                                <span className="ok">● Permitted</span>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <Pager page={page} perPage={perPage} total={data.total} onPage={setPage} noun="events" />
                </>
              )}
            </section>

            <aside className="col-side" style={{ flex: '1 1 320px' }}>
              <section className="card">
                <h2 data-h="3" style={{ marginBottom: 14 }}>By actor · {period.short}</h2>
                {s && s.by_actor.length === 0 && <p className="sm muted">No changes in this period.</p>}
                {!s && <SkLines lines={3} />}
                <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                  {(s?.by_actor ?? []).map((a) => (
                    <div key={a.actor_id}>
                      <div className="sm" style={{ display: 'flex', justifyContent: 'space-between' }}>
                        <span>{a.yours ? 'Me (dashboard)' : a.actor_id}</span>
                        <span>{a.count}</span>
                      </div>
                      <div className="bar thin">
                        <span style={{ width: `${Math.max(2, (a.count / maxActor) * 100)}%` }} />
                      </div>
                    </div>
                  ))}
                </div>
                <a className="sm" href={buildHash('/identities')} style={{ display: 'inline-block', marginTop: 14 }}>
                  Manage my agents
                </a>
              </section>
              <section className="card">
                <div className="ch">
                  <h2 data-h="3">Blocked writes</h2>
                  <span className="lbl red">{s?.blocked ?? 0}</span>
                </div>
                <p className="sm" style={{ margin: '0 0 6px' }}>
                  Your agents tried to store something a rule forbids. Nothing was saved; the rule name tells you why.
                </p>
                {(s?.blocked_recent ?? []).map((b) => (
                  <div key={`${b.id}-${b.timestamp}`} className="evrow" style={{ alignItems: 'center' }}>
                    <span className="lbl red">{b.rule || 'policy'}</span>
                    <span className="sm">
                      {b.actor_id} · {fmtMonthDay(b.timestamp)}
                      {b.blocked_by ? ` · ${RULE_LABEL[b.blocked_by] ?? b.blocked_by}` : ''}
                    </span>
                  </div>
                ))}
                <a className="sm" href={buildHash('/curation')} style={{ display: 'inline-block', marginTop: 10 }}>
                  See rules that apply to me
                </a>
              </section>
            </aside>
          </div>
        </>
      )}
    </div>
  );
};

const FilterSelect: React.FC<{ id: string; label: string; value: string; onChange: (v: string) => void; options: [string, string][] }> = ({ id, label, value, onChange, options }) => (
  <>
    <label htmlFor={id} className="vh">
      {label}
    </label>
    <select id={id} className="in auto" value={value} onChange={(e) => onChange(e.target.value)}>
      {options.map(([v, l]) => (
        <option key={v} value={v}>
          {l}
        </option>
      ))}
    </select>
  </>
);

const KpiTile: React.FC<{ label: string; value: number | null; sub: string; href?: string }> = ({ label, value, sub, href }) => {
  const body = (
    <>
      <div className="sm">{label}</div>
      <div className="kpi">{value === null ? <Sk w={60} h={30} /> : fmtInt(value)}</div>
      <div className="sm muted">{sub}</div>
    </>
  );
  return href ? (
    <a className="card link-plain" href={href} style={{ color: 'inherit' }}>
      {body}
    </a>
  ) : (
    <div className="card">{body}</div>
  );
};

/** What the operation changed: a new memory, a new version with a diff link, or a weight change. */
const ChangeCell: React.FC<{ e: AuditEvent }> = ({ e }) => {
  if (e.decision === 'denied' || READ_OPS.has(e.operation)) return <span className="muted">—</span>;
  const c = e.change;
  if (c) {
    if (c.before.version !== c.after.version)
      return (
        <a href={buildHash(`/memory/${e.memory_id}`, { tab: 'versions' })}>
          v{c.before.version} → v{c.after.version}
          {c.before.weight !== c.after.weight ? ` · weight ${c.before.weight.toFixed(2)} → ${c.after.weight.toFixed(2)}` : ' · view diff'}
        </a>
      );
  }
  if (e.operation === 'memory.write' && e.memory_id) return <>new · v{e.memory_version ?? 1}</>;
  return <span className="muted">—</span>;
};

export default MyActivity;
