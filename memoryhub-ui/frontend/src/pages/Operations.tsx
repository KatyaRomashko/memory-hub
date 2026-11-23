import React, { useState } from 'react';
import { api } from '@/api/client';
import { PageHeader } from '@/components/Shell';
import { Empty, ErrorState, Seg, SelectMenu, SkTable, useToast } from '@/components/ui';
import { useApi } from '@/hooks/useApi';
import { buildHash } from '@/hooks/useHashRoute';
import type { JobRow, OperationsResponse } from '@/types';
import { errorMessage, fmtInt, fmtRelative, shortId } from '@/utils/format';

const fmtMs = (ms: number | null | undefined) => (ms === null || ms === undefined ? '—' : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`);

const STATUS: Record<string, { cls: string; label: string }> = {
  ok: { cls: 'grn', label: 'done' },
  failed: { cls: 'red', label: 'failed' },
  running: { cls: 'blue', label: 'running' },
};

/** Background work on your memories: entity extraction and conversation extraction (#718). */
const Operations: React.FC<{ days?: string; initialStatus?: string; description?: React.ReactNode }> = ({ days: fixedDays, initialStatus, description }) => {
  const toast = useToast();
  const [ownDays, setDays] = useState('7');
  const days = fixedDays ?? ownDays;
  const [status, setStatus] = useState(initialStatus ?? '');
  const [kind, setKind] = useState('');
  const [corr, setCorr] = useState<string | null>(null);
  const { data, error, loading, reload } = useApi<OperationsResponse>(
    () => api.operations({ days: Number(days), status: status || undefined, kind: kind || undefined }),
    [days, status, kind],
    15000,
  );
  const items = (data?.items ?? []).filter((j) => !corr || j.correlation_id === corr);
  const retry = async (j: JobRow) => {
    try {
      await api.retryJob(j.id);
      toast(`${j.label} ran again.`);
    } catch (e) {
      toast(errorMessage(e), 'err');
    } finally {
      reload();
    }
  };
  return (
    <>
      <PageHeader
        title="Background jobs"
        description={
          description ?? 'Work that ran in the background on memories you and your agents wrote: entity extraction after each write, and extraction of memories from conversations.'
        }
        actions={
          fixedDays ? undefined : <Seg
            label="Period"
            value={days}
            onChange={setDays}
            options={[
              { value: '1', label: '24 h' },
              { value: '7', label: '7 d' },
              { value: '30', label: '30 d' },
            ]}
          />
        }
      />
      <div className="body">
        {data && data.kinds.length > 0 && (
          <div className="kgrid" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))' }}>
            {data.kinds.map((k) => (
              <button key={k.kind} type="button" className={`card kcard opk ${kind === k.kind ? 'on' : ''}`} onClick={() => setKind(kind === k.kind ? '' : k.kind)}>
                <div className="sm b6">{k.label}</div>
                <div className="statv" style={{ margin: '6px 0 2px' }}>
                  {fmtInt(k.ok + k.failed + k.running)}
                </div>
                <div className="hint">
                  {k.failed ? <span className="err">{k.failed} failed · </span> : null}
                  {k.avg_ms !== undefined ? `avg ${fmtMs(k.avg_ms)} · p95 ${fmtMs(k.p95_ms)}` : ''}
                </div>
              </button>
            ))}
          </div>
        )}
        <section className="card" style={{ padding: 0 }}>
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', padding: '14px 20px', flexWrap: 'wrap' }}>
            <SelectMenu
              label="Status"
              prefix="Status: "
              value={status}
              onChange={setStatus}
              options={[
                { value: '', label: 'Any' },
                { value: 'failed', label: 'Failed' },
                { value: 'running', label: 'Running' },
                { value: 'ok', label: 'Done' },
              ]}
            />
            {kind && (
              <span className="chip">
                {data?.kinds.find((k) => k.kind === kind)?.label ?? kind}
                <button type="button" aria-label="Clear kind" onClick={() => setKind('')}>
                  ✕
                </button>
              </span>
            )}
            {corr && (
              <span className="chip">
                Correlation {shortId(corr, 'run')}
                <button type="button" aria-label="Clear correlation" onClick={() => setCorr(null)}>
                  ✕
                </button>
              </span>
            )}
            <span className="sm muted" style={{ marginLeft: 'auto' }}>
              {data?.scope === 'tenant' ? 'All users' : 'Only your jobs'} · refreshes every 15 s
            </span>
          </div>
          {error && !data ? (
            <ErrorState error={error} onRetry={reload} />
          ) : loading && !data ? (
            <SkTable rows={6} cols={6} />
          ) : items.length === 0 ? (
            <Empty compact title="No jobs in this period">Jobs appear when your agents write memories (entity extraction) or memories are extracted from your conversations.</Empty>
          ) : (
            <div className="scroll">
              <table className="tbl mid">
                <thead>
                  <tr>
                    <th>Job</th>
                    <th>Status</th>
                    {data?.scope === 'tenant' && <th>For</th>}
                    <th>Trigger</th>
                    <th>Duration</th>
                    <th>Attempts</th>
                    <th>Correlation</th>
                    <th>Started</th>
                    <th><span className="vh">Actions</span></th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((j) => (
                    <tr key={j.id}>
                      <td>
                        <div className="b6">{j.label}</div>
                        <div className="hint">{detailLine(j)}</div>
                        {j.error && <div className="sm err">{j.error}</div>}
                      </td>
                      <td>
                        <span className={`lbl ${j.stale ? 'ylw' : STATUS[j.status]?.cls ?? 'g'}`}>{j.stale ? 'stuck' : STATUS[j.status]?.label ?? j.status}</span>
                      </td>
                      {data?.scope === 'tenant' && <td className="sm mono">{j.owner}</td>}
                      <td className="sm">{j.trigger}</td>
                      <td className="sm">{fmtMs(j.duration_ms)}</td>
                      <td className="sm">{j.attempts}</td>
                      <td>
                        <button type="button" className="link-btn sm mono" onClick={() => setCorr(j.correlation_id)} title="Show jobs from the same run">
                          {shortId(j.correlation_id, 'run')}
                        </button>
                      </td>
                      <td className="sm muted" style={{ whiteSpace: 'nowrap' }}>
                        {fmtRelative(j.started_at)}
                      </td>
                      <td>
                        {j.retryable && (
                          <button type="button" className="btn bl sm" onClick={() => retry(j)}>
                            Retry
                          </button>
                        )}
                        {link(j)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
        <p className="hint">
          Not traced here: agents’ own MCP calls (see the Activity log), embedding and LLM calls. Duration is not recorded for these jobs yet.
        </p>
      </div>
    </>
  );
};

const detailLine = (j: JobRow): string => {
  const d = j.detail as Record<string, unknown>;
  const parts: string[] = [];
  if (typeof d.memory_stub === 'string') parts.push(d.memory_stub);
  if (typeof d.entities === 'number') parts.push(`${d.entities} entities`);
  if (typeof d.messages === 'number') parts.push(`${d.messages} messages`);
  if (Array.isArray(d.window)) parts.push(`messages ${d.window[0]}–${d.window[1]}`);
  return parts.join(' · ');
};

const link = (j: JobRow) => {
  const d = j.detail as Record<string, string>;
  if (d.memory_id) return <a className="btn bl sm" href={buildHash(`/memory/${d.memory_id}`)}>Open</a>;
  return null;
};

export default Operations;
