import React, { useState } from 'react';
import { api } from '@/api/client';
import { PageHeader, useApp } from '@/components/Shell';
import { ErrorState, Icon, ScopeLabel, Sk, SkLines, Standby } from '@/components/ui';
import { useApi } from '@/hooks/useApi';
import { buildHash } from '@/hooks/useHashRoute';
import { ActivityChart } from '@/components/charts';
import type { ContradictionStats, Onboarding, StatsResponse, UsageSummary, UserOverview, UserOverviewRange } from '@/types';
import { fmtInt, fmtRelative, scopeMeta, truncate } from '@/utils/format';
import { memoryHref, WriteMemoryModal } from './common';

/** A KPI tile. With an href the whole tile is a link to the records behind the number. */
const KpiCard: React.FC<{ label: string; href?: string; loading: boolean; value?: React.ReactNode; sub?: React.ReactNode }> = ({
  label,
  href,
  loading,
  value,
  sub,
}) => {
  const body = (
    <>
      <div className="sm muted kpi-label">
        {label}
        {href && (
          <span className="kpi-go">
            <Icon.Arrow size={15} />
          </span>
        )}
      </div>
      {loading ? (
        <>
          <Sk w="55%" h={34} style={{ margin: '8px 0 6px' }} />
          <Sk w="70%" h={12} />
        </>
      ) : (
        <>
          <div className="kpi">{value}</div>
          <div className="sm">{sub}</div>
        </>
      )}
    </>
  );
  return href ? (
    <a className="card kpi-card" href={href}>
      {body}
    </a>
  ) : (
    <div className="card">{body}</div>
  );
};

interface OverviewData {
  stats: StatsResponse;
  contradictions: ContradictionStats;
}

/**
 * Overview for both roles, from GET /api/stats (memory counts by scope, recent
 * writes, MCP health) and GET /api/contradictions/stats. Both are already
 * limited to what the viewer may see.
 */
const Overview: React.FC = () => {
  const { role, tenant, refreshCounts } = useApp();
  const [writing, setWriting] = useState(false);
  const { data, error, loading, reload } = useApi<OverviewData>(
    async () => {
      const [stats, contradictions] = await Promise.all([api.stats(), api.contradictionStats()]);
      return { stats, contradictions };
    },
    [],
    60000,
  );
  const first = loading && !data;
  const total = data?.stats.total_memories ?? 0;
  const empty = !!data && total === 0;
  const isAdmin = role === 'admin';
  const scopes = [...(data?.stats.scope_counts ?? [])].sort((a, b) => b.count - a.count);

  return (
    <>
      <PageHeader
        title={isAdmin ? 'Tenant overview' : 'Overview'}
        crumbs={[{ label: isAdmin ? 'Tenant overview' : 'Overview' }]}
        description={
          isAdmin ? (
            <>
              Governed agent memory for <b>{tenant || '…'}</b> in the scopes you moderate. Personal memories are counted, never shown.
            </>
          ) : (
            <>
              The memories you can read in tenant <b>{tenant || '…'}</b>: your own and what your projects and organization share with you.
            </>
          )
        }
        actions={
          <>
            <button
              type="button"
              className="btn bs"
              onClick={() => {
                reload();
                refreshCounts();
              }}
            >
              Refresh
            </button>
            {isAdmin ? (
              <a className="btn bp" href={buildHash('/review')}>
                Open contradictions
              </a>
            ) : (
              <button type="button" className="btn bp" onClick={() => setWriting(true)}>
                <Icon.Plus />
                Write memory
              </button>
            )}
          </>
        }
      />
      <div className="body">
        {error && !data ? (
          <div className="card">
            <ErrorState error={error} onRetry={reload} />
          </div>
        ) : (
          <>
            {data && !data.stats.mcp_health && (
              <div className="alert warn-a" role="status">
                <Icon.Info style={{ flex: 'none', marginTop: 1 }} />
                <div>
                  <b>The MCP server did not answer its health check.</b> Agents may not be able to read or write memories right now.
                </div>
              </div>
            )}

            <div className="grid-kpi">
              <KpiCard
                label={isAdmin ? 'Current memories' : 'Memories you can read'}
                loading={first}
                href={buildHash('/memories')}
                value={empty ? <span className="standby-val">0</span> : fmtInt(total)}
                sub={
                  empty ? (
                    <span className="standby-pulse">
                      <i />
                      Waiting for the first write
                    </span>
                  ) : (
                    <span className="muted">Current versions, not deleted</span>
                  )
                }
              />
              <KpiCard
                label="Open contradictions"
                loading={first}
                href={isAdmin ? buildHash('/review') : buildHash('/memories', { flag: 'contradicted' })}
                value={fmtInt(data?.contradictions.unresolved)}
                sub={<span className="muted">{fmtInt(data?.contradictions.total)} reported in total</span>}
              />
              <KpiCard
                label="High-confidence reports"
                loading={first}
                value={fmtInt(data?.contradictions.high_confidence)}
                sub={
                  <span className="muted">
                    {fmtInt(data?.contradictions.medium_confidence)} medium · {fmtInt(data?.contradictions.low_confidence)} low
                  </span>
                }
              />
              <KpiCard
                label="MCP server"
                loading={first}
                value={data?.stats.mcp_health ? 'Healthy' : 'Unreachable'}
                sub={<span className="muted">Health check from the dashboard backend</span>}
              />
            </div>

            {!isAdmin && <UserBlocks />}

            <div className="grid-2-1">
              <div className="card span-2">
                <div className="ch">
                  <h2 data-h="3">Recent activity</h2>
                  <a className="sm" href={buildHash('/memories', { sort: 'updated' })}>
                    Open Memory browser
                  </a>
                </div>
                {first ? (
                  <SkLines lines={6} gap={22} />
                ) : empty ? (
                  <Standby>Memories appear here as agents and people write them.</Standby>
                ) : !data?.stats.recent_activity.length ? (
                  <p className="sm muted">No activity recorded yet.</p>
                ) : (
                  <div className="scroll">
                    <table className="tbl">
                      <thead>
                        <tr>
                          <th>When</th>
                          <th>Memory</th>
                          <th>Owner</th>
                          <th>Scope</th>
                          <th>Change</th>
                        </tr>
                      </thead>
                      <tbody>
                        {data.stats.recent_activity.map((r) => (
                          <tr key={`${r.id}-${r.updated_at}`}>
                            <td className="muted" style={{ whiteSpace: 'nowrap' }}>
                              {fmtRelative(r.updated_at)}
                            </td>
                            <td style={{ maxWidth: 420 }}>
                              <a href={memoryHref(r.id)}>{truncate(r.stub, 110)}</a>
                            </td>
                            <td>{r.owner_id}</td>
                            <td>
                              <ScopeLabel scope={r.scope} />
                            </td>
                            <td className="muted">{r.action}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>

              <div className="card">
                <div className="ch">
                  <h2 data-h="3">{isAdmin ? 'Memories by scope' : 'My memories by scope'}</h2>
                </div>
                {first ? (
                  <SkLines lines={6} gap={22} />
                ) : empty || scopes.length === 0 ? (
                  <Standby>Counts for each scope appear here.</Standby>
                ) : (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                    {scopes.map((s) => {
                      const pct = total ? Math.round((s.count / total) * 100) : 0;
                      return (
                        <a
                          key={s.scope}
                          href={buildHash('/memories', { scope: s.scope })}
                          className="link-plain"
                          style={{ display: 'flex', flexDirection: 'column', gap: 6, color: 'inherit' }}
                        >
                          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                            <ScopeLabel scope={s.scope} />
                            <span className="sm">
                              <b>{fmtInt(s.count)}</b> <span className="muted">{`${pct}%`}</span>
                            </span>
                          </div>
                          <div className="bar">
                            <span style={{ width: `${s.count ? Math.max(1, pct) : 0}%`, background: scopeMeta(s.scope).color }} />
                          </div>
                        </a>
                      );
                    })}
                  </div>
                )}
              </div>
            </div>
          </>
        )}
      </div>
      {writing && <WriteMemoryModal onClose={() => setWriting(false)} onSaved={() => reload()} />}
    </>
  );
};

/* ------------------------------------------------------------------ */
/* User overview (#711): operations over time, agent usage, categories, */
/* open requests and getting started. Each block hides without data.   */
/* ------------------------------------------------------------------ */

const USER_RANGES: { value: UserOverviewRange; label: string; title: string }[] = [
  { value: '1d', label: '24 h', title: 'Your writes and searches in the last 24 hours, per hour' },
  { value: '7d', label: '7 days', title: 'Your writes and searches in the last 7 days, per day' },
  { value: '30d', label: '30 days', title: 'Your writes and searches in the last 30 days, per day' },
];

const UserBlocks: React.FC = () => {
  const [range, setRange] = useState<UserOverviewRange>('7d');
  const { data, error, loading, reload } = useApi<UserOverview>(() => api.userOverview(range), [range], 60000);
  const selected = USER_RANGES.find((r) => r.value === range) ?? USER_RANGES[1];
  const ready = Boolean(data && data.activity_range === range);
  const quiet = ready && data!.totals.writes + data!.totals.searches === 0;
  return (
    <>
      <GettingStarted />
      {!(error && !data) && (
        <div className="card ops">
          <div className="ops-head">
            <div>
              <h2 data-h="3">Memory operations</h2>
              <div className="ops-sub">
                {selected.title}
                {ready && ` · ${fmtInt(data!.totals.writes)} writes, ${fmtInt(data!.totals.searches)} searches by you and your agents`}
              </div>
            </div>
            <div className="ops-tools">
              <div className="ops-range" role="group" aria-label="Memory operation range">
                {USER_RANGES.map((item) => (
                  <button key={item.value} type="button" className={range === item.value ? 'on' : ''} aria-pressed={range === item.value} onClick={() => setRange(item.value)}>
                    {item.label}
                  </button>
                ))}
              </div>
              <button type="button" className="ops-refresh" onClick={reload} disabled={loading} aria-label="Refresh memory operations" title="Refresh">
                <Icon.Refresh size={15} />
              </button>
            </div>
          </div>
          {!ready ? (
            <Sk h={290} r={8} />
          ) : quiet ? (
            <Standby>Writes and searches by you and your agents chart here.</Standby>
          ) : (
            <ActivityChart data={data!.activity} bucketSeconds={data!.activity_bucket_seconds} range={range} />
          )}
        </div>
      )}
      <div className="row-wrap">
        <AgentUsage />
        {data && data.categories.length > 0 && (
          <section className="card" style={{ flex: '1 1 300px' }}>
            <h2 data-h="3" style={{ marginBottom: 12 }}>
              Memory categories
            </h2>
            <div className="chips">
              {data.categories.map((c) => (
                <a key={c.name} className="tag link-plain" href={buildHash('/memories', { domain: c.name })}>
                  {c.name} {fmtInt(c.memory_count)}
                </a>
              ))}
            </div>
            {data.categories_truncated && <p className="hint">The largest categories of the memories you can read.</p>}
          </section>
        )}
        {data?.requests && (
          <section className="card" style={{ flex: '0 1 240px' }}>
            <div className="ch">
              <h2 data-h="3">My open requests</h2>
              <a className="sm" href={buildHash('/review')}>
                My requests
              </a>
            </div>
            <div className="kpi">{fmtInt(data.requests.open)}</div>
            <div className="sm muted">{data.requests.open === 1 ? 'request is' : 'requests are'} waiting for a decision</div>
          </section>
        )}
      </div>
    </>
  );
};

/** Getting started: what a new user still has to do. Hidden once done or dismissed. */
const GettingStarted: React.FC = () => {
  const { data, setData } = useApi<Onboarding>(() => api.onboarding(), []);
  if (!data?.steps || data.dismissed || data.done === data.total) return null;
  const dismiss = () => {
    setData({ ...data, dismissed: true });
    api.dismissOnboarding().catch(() => undefined);
  };
  return (
    <section className="card" aria-label="Getting started">
      <div className="ch">
        <div>
          <h2 data-h="3">Getting started</h2>
          <p className="sm muted" style={{ margin: '4px 0 0' }}>
            {data.done} of {data.total} done. Each step takes a minute.
          </p>
        </div>
        <button type="button" className="btn bl sm" onClick={dismiss}>
          Hide
        </button>
      </div>
      <div className="bar thin" style={{ marginTop: 10 }}>
        <span style={{ width: `${Math.round((data.done / data.total) * 100)}%` }} />
      </div>
      <div className="onb">
        {data.steps.map((s) => (
          <a key={s.id} href={buildHash(s.href)} className={s.done ? 'done' : ''}>
            <span className="chk" aria-hidden="true">
              {s.done ? '✓' : ''}
            </span>
            <span>
              <span className="sm b6" style={{ display: 'block' }}>
                {s.title}
                {s.done && <span className="vh"> (done)</span>}
              </span>
              <span className="hint">{s.body}</span>
            </span>
          </a>
        ))}
      </div>
    </section>
  );
};

/** Do my agents act on what search hands them? Hidden until there is something to measure. */
const AgentUsage: React.FC = () => {
  const { data } = useApi<UsageSummary>(() => api.usageSummary(30), []);
  if (!data || !data.served) return null;
  const pct = data.use_rate === null ? null : Math.round(data.use_rate * 100);
  return (
    <section className="card" aria-label="Agent usage" style={{ flex: '2 1 420px' }}>
      <div className="ch">
        <h2 data-h="3">Do your agents use what they get?</h2>
        <span className="sm muted">Last {data.days} days</span>
      </div>
      <div style={{ display: 'flex', gap: 28, flexWrap: 'wrap', alignItems: 'flex-start' }}>
        <div style={{ flex: '0 1 240px' }}>
          <div className="statv">{pct === null ? '—' : `${pct}%`}</div>
          <div className="hint">
            of {fmtInt(data.used + data.skipped)} search hits were acted on — read, updated, linked or reported by the same agent within 30 minutes. {fmtInt(data.full)} more
            were delivered in full (weight ≥ 0.80).
          </div>
        </div>
        <div style={{ flex: '1 1 280px', minWidth: 0 }}>
          <div className="sm b6" style={{ marginBottom: 6 }}>
            Skipped most often
          </div>
          {data.skipped_most.length === 0 ? (
            <p className="sm muted" style={{ margin: 0 }}>
              Nothing stands out — your agents act on what they get.
            </p>
          ) : (
            data.skipped_most.slice(0, 5).map((m) => (
              <a key={m.id} className="conn link-plain" href={memoryHref(m.id)}>
                <span className="ellipsis" style={{ flex: 1, minWidth: 0 }}>
                  {m.stub}
                </span>
                <span className="hint" style={{ whiteSpace: 'nowrap' }}>
                  skipped {m.skipped} of {m.served}
                </span>
              </a>
            ))
          )}
        </div>
      </div>
    </section>
  );
};

export default Overview;
