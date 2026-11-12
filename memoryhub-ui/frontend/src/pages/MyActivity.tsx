import React from 'react';
import { api } from '@/api/client';
import { Hub, useApp } from '@/components/Shell';
import { Empty, ErrorState, Icon, SkLines, useToast } from '@/components/ui';
import { useApi } from '@/hooks/useApi';
import { buildHash, type HashRoute } from '@/hooks/useHashRoute';
import type { AuditEvent, MyReads, MyRequest, MyRequestsResponse } from '@/types';
import { errorMessage, fmtInt, fmtMonthDay, fmtTime } from '@/utils/format';
import Audit from './Audit';
import Connect from './Connect';
import Dreaming from './Dreaming';
import Operations from './Operations';
import Quality from './Quality';
import Verify from './Verify';
import WhoRead from './WhoRead';
import { memoryHref } from './common';
import { RequestSentence, RequestStatus } from './requests';

type View = 'changes' | 'reads' | 'connections' | 'requests' | 'jobs' | 'dreaming' | 'checks' | 'incidents';

/**
 * My activity (user): a personal projection of events about the viewer and
 * their data. Everything that used to have its own menu item for a user —
 * readers, connections, background jobs, Dreaming, retrieval checks — is a view here.
 */
const MyActivity: React.FC<{ route: HashRoute }> = ({ route }) => {
  const toast = useToast();
  const { me } = useApp();
  const labs = Boolean(me?.labs);
  const reads = useApi<MyReads>(() => api.myReads(90, 'others'), []);
  const incidents = reads.data ? reads.data.summary?.incident_grants ?? reads.data.events.filter((e) => e.break_glass).length : null;

  const exportMine = async () => {
    try {
      const all: AuditEvent[] = [];
      for (let page = 1; page <= 25; page += 1) {
        const res = await api.audit({ page, per_page: 200 });
        all.push(...res.items);
        if (all.length >= res.total) break;
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

  const views: { value: View; label: string; count?: number | null; render: () => React.ReactNode; showTitle?: boolean }[] = [
    { value: 'changes', label: 'My changes & agents', render: () => <Audit route={route} /> },
    { value: 'reads', label: 'Who read my memories', render: () => <WhoRead route={route} /> },
    {
      value: 'connections',
      label: 'Connections',
      showTitle: true,
      render: () => (
        <>
          <Connect />
          <Verify />
        </>
      ),
    },
    { value: 'requests', label: 'Requests', render: () => <RequestsLog /> },
    { value: 'jobs', label: 'Background jobs', render: () => <Operations /> },
    ...(labs
      ? [
          { value: 'dreaming' as const, label: 'Dreaming', render: () => <Dreaming /> },
          { value: 'checks' as const, label: 'Retrieval checks', render: () => <Quality route={route} /> },
        ]
      : []),
    { value: 'incidents', label: 'Incident notices', count: incidents, render: () => <IncidentNotices data={reads.data} error={reads.error} reload={reads.reload} /> },
  ];

  return (
    <Hub
      title="My activity"
      description="A personal projection of events about you and your data: your and your agents’ changes, reads of your personal and project records, your connections, requests and jobs. Not the tenant audit and not a raw log of other people."
      actions={
        <button type="button" className="btn bs" onClick={exportMine}>
          Export
        </button>
      }
      route={route}
      fallback="changes"
      views={views}
    />
  );
};

/** What happened to your requests, newest first. The requests themselves live in My requests. */
const RequestsLog: React.FC = () => {
  const { data, error, loading, reload } = useApi<MyRequestsResponse>(() => api.myRequests(), []);
  type Row = { at: string; r: MyRequest; what: string };
  const rows: Row[] = [];
  (data?.items ?? []).forEach((r) => {
    rows.push({ at: r.submitted_at, r, what: 'You submitted' });
    if (r.state === 'decided' && r.decided_at) rows.push({ at: r.decided_at, r, what: `${r.decided_by ?? 'A reviewer'} ${r.outcome === 'rejected' ? 'rejected' : 'approved'}` });
    if (r.state === 'withdrawn' && r.withdrawn_at) rows.push({ at: r.withdrawn_at, r, what: 'You withdrew' });
  });
  rows.sort((a, b) => b.at.localeCompare(a.at));
  return (
    <div className="body">
      <section className="card">
        <div className="ch">
          <h3>Request events</h3>
          <a className="sm" href={buildHash('/review')}>
            Open My requests
          </a>
        </div>
        {error && !data ? (
          <ErrorState error={error} onRetry={reload} compact />
        ) : loading && !data ? (
          <SkLines lines={5} />
        ) : rows.length === 0 ? (
          <Empty compact title="No requests yet">
            Promotions, project access and restore requests you submit show up here as they move.
          </Empty>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column' }}>
            {rows.map((x, i) => (
              <div key={`${x.r.id}-${i}`} className="evrow">
                <span className="mono muted">{fmtMonthDay(x.at)}</span>
                <span style={{ flex: 1, minWidth: 0 }}>
                  {x.what}: <RequestSentence r={x.r} />
                  {x.r.reason && x.r.state === 'decided' && x.what !== 'You submitted' && <span className="muted"> — “{x.r.reason}”</span>}
                </span>
                {x.what === 'You submitted' && <RequestStatus r={x.r} short />}
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
};

/** Incident (break-glass) reads of your memories. Each carries its incident number. */
const IncidentNotices: React.FC<{ data: MyReads | null; error: Error | null; reload: () => void }> = ({ data, error, reload }) => {
  const events = (data?.events ?? []).filter((e) => e.break_glass);
  return (
    <div className="body">
      <section className="card">
        <div className="ch">
          <h3>Incident notices</h3>
          <span className="sm muted">Last 90 days</span>
        </div>
        {error && !data ? (
          <ErrorState error={error} onRetry={reload} compact />
        ) : !data ? (
          <SkLines lines={3} />
        ) : events.length === 0 ? (
          <Empty compact title="No incident access to your memories">
            A super-user can open personal memories only with an incident reference. If that ever happens to yours, each read is listed here with its incident number and you
            are notified.
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
        <div className="alert ok-a" style={{ marginTop: 14 }}>
          <Icon.Shield stroke="#3d7317" style={{ flex: 'none', marginTop: 1 }} />
          <div>Any incident read of your memory appears here with its incident number.</div>
        </div>
      </section>
    </div>
  );
};

export default MyActivity;
