import React, { useState } from 'react';
import { api, ApiError } from '@/api/client';
import { PageHeader, Tabs } from '@/components/Shell';
import { Empty, ErrorState, Icon, SkTable, useToast } from '@/components/ui';
import { useApi } from '@/hooks/useApi';
import { buildHash, navigate, type HashRoute } from '@/hooks/useHashRoute';
import type { MyRequest, MyRequestsResponse } from '@/types';
import { fmtMonthDay } from '@/utils/format';
import { ConfirmModal, memoryHref } from './common';
import { NewRequestModal, REQUEST_KIND, RequestQueue, RequestStatus, requestIds, ScopeChain } from './requests';

type Tab = 'open' | 'decided' | 'withdrawn' | 'decide';

/**
 * My requests (user, #719): the promotions and shared-knowledge submissions the
 * viewer made (GET /api/me/requests). Users submit and withdraw; a project admin
 * or platform admin decides. A user who administers a project also gets the
 * requests they may decide in "To decide".
 */
const MyRequests: React.FC<{ route: HashRoute }> = ({ route }) => {
  const toast = useToast();
  const raw = route.params.get('tab');
  const tab: Tab = raw === 'decided' || raw === 'withdrawn' || raw === 'decide' ? raw : 'open';
  const { data, error, loading, reload } = useApi<MyRequestsResponse>(() => api.myRequests(), [], 60000);
  const [creating, setCreating] = useState(false);
  const [withdrawing, setWithdrawing] = useState<MyRequest | null>(null);
  const setTab = (t: Tab) => navigate('/review', t === 'open' ? undefined : { tab: t }, { replace: true });

  const items = data?.items ?? [];
  const open = items.filter((r) => r.state === 'open');
  const decided = items.filter((r) => r.state === 'decided').sort((a, b) => (b.decided_at ?? '').localeCompare(a.decided_at ?? ''));
  const withdrawn = items.filter((r) => r.state === 'withdrawn');
  const toDecide = data?.to_decide ?? 0;

  return (
    <>
      <PageHeader
        title="My requests"
        description="Your promotions and shared-knowledge submissions. A project admin or platform admin decides; you follow the status and see the reason."
        actions={
          <button type="button" className="btn bp" onClick={() => setCreating(true)}>
            <Icon.Plus />
            New request
          </button>
        }
        tabs={
          <Tabs
            label="Request state"
            value={tab}
            onChange={setTab}
            tabs={[
              { value: 'open', label: 'Open', count: data?.counts?.open ?? null },
              { value: 'decided', label: 'Decided', count: data?.counts?.decided ?? null },
              { value: 'withdrawn', label: 'Withdrawn', count: data?.counts?.withdrawn ?? null },
              ...(toDecide > 0 || tab === 'decide' ? [{ value: 'decide' as Tab, label: 'To decide', count: toDecide }] : []),
            ]}
          />
        }
      />
      <div className="body">
        {tab !== 'decide' && (
          <div className="alert" role="note">
            <Icon.Info stroke="#0066cc" style={{ flex: 'none', marginTop: 1 }} />
            <div>
              <b>You can submit and withdraw, not decide your own.</b> Only your own submissions are listed here.
            </div>
          </div>
        )}

        {tab === 'decide' ? (
          <RequestQueue onChanged={reload} />
        ) : error && !data ? (
          <div className="card">
            <ErrorState error={error} onRetry={reload} title={error instanceof ApiError && error.status === 404 ? 'My requests is not available on this server yet' : 'Your requests could not be loaded'} />
          </div>
        ) : loading && !data ? (
          <div className="card">
            <SkTable rows={4} cols={5} />
          </div>
        ) : tab === 'open' ? (
          <>
            <section className="card">
              <div className="ch">
                <h2 data-h="3">Open</h2>
                <span className="sm muted">
                  {open.length} request{open.length === 1 ? '' : 's'}
                </span>
              </div>
              {open.length === 0 ? (
                <Empty compact title="Nothing waiting">
                  Promote a memory or submit it as shared knowledge with New request.
                </Empty>
              ) : (
                <div className="scroll">
                  <table className="tbl" style={{ minWidth: 820 }}>
                    <thead>
                      <tr>
                        <th>Request</th>
                        <th>Type</th>
                        <th>Target scope</th>
                        <th>Submitted</th>
                        <th>Status</th>
                        <th>
                          <span className="vh">Actions</span>
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {open.map((r) => (
                        <tr key={r.id}>
                          <td style={{ maxWidth: 240 }}>
                            <RequestTitle r={r} />
                            <div>{requestIds(r)}</div>
                          </td>
                          <td className="sm">{REQUEST_KIND[r.kind]}</td>
                          <td>
                            <ScopeChain r={r} />
                          </td>
                          <td className="sm">
                            {fmtMonthDay(r.submitted_at)}
                            <div className="hint">by you · {r.submitted_via ?? 'dashboard'}</div>
                          </td>
                          <td>
                            <RequestStatus r={r} />
                            {r.note && (
                              <div className="hint" style={{ marginTop: 4, maxWidth: 240 }}>
                                {r.note}
                              </div>
                            )}
                          </td>
                          <td>
                            <button type="button" className="btn bl sm" onClick={() => setWithdrawing(r)}>
                              Withdraw
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>

            <div className="row-wrap">
              <section className="card" style={{ flex: '999 1 560px', minWidth: 0 }}>
                <div className="ch">
                  <h2 data-h="3">Recently decided</h2>
                  <a className="sm" href={buildHash('/review', { tab: 'decided' })}>
                    All decided
                  </a>
                </div>
                <DecidedTable rows={decided.slice(0, 3)} />
              </section>
              <HowRequestsMove />
            </div>
          </>
        ) : tab === 'decided' ? (
          <section className="card">
            <div className="ch">
              <h2 data-h="3">Decided</h2>
              <span className="sm muted">{decided.length} requests</span>
            </div>
            <DecidedTable rows={decided} />
          </section>
        ) : (
          <section className="card">
            <div className="ch">
              <h2 data-h="3">Withdrawn</h2>
              <span className="sm muted">{withdrawn.length} requests</span>
            </div>
            {withdrawn.length === 0 ? (
              <Empty compact title="Nothing withdrawn">
                Requests you withdraw before a decision are kept here.
              </Empty>
            ) : (
              <div className="scroll">
                <table className="tbl" style={{ minWidth: 640 }}>
                  <thead>
                    <tr>
                      <th>Request</th>
                      <th>Type</th>
                      <th>Target scope</th>
                      <th>Withdrawn</th>
                    </tr>
                  </thead>
                  <tbody>
                    {withdrawn.map((r) => (
                      <tr key={r.id}>
                        <td style={{ maxWidth: 260 }}>
                          <RequestTitle r={r} />
                          <div>{requestIds(r)}</div>
                        </td>
                        <td className="sm">{REQUEST_KIND[r.kind]}</td>
                        <td>
                          <ScopeChain r={r} />
                        </td>
                        <td className="sm">{r.withdrawn_at ? fmtMonthDay(r.withdrawn_at) : '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        )}
      </div>

      {creating && <NewRequestModal onClose={() => setCreating(false)} onCreated={() => reload()} />}
      {withdrawing && (
        <ConfirmModal
          title="Withdraw request?"
          confirmLabel="Withdraw"
          onClose={() => setWithdrawing(null)}
          onConfirm={async () => {
            await api.withdrawMyRequest(withdrawing.id);
            toast('Request withdrawn. It left the review queue.');
            reload();
          }}
        >
          <b>{withdrawing.title}</b> leaves the review queue. Nothing changes in your memory; you can submit it again later.
        </ConfirmModal>
      )}
    </>
  );
};

const RequestTitle: React.FC<{ r: MyRequest }> = ({ r }) => (r.memory_id ? <a href={memoryHref(r.memory_id)}>{r.title}</a> : <span className="b6">{r.title}</span>);

const DecidedTable: React.FC<{ rows: MyRequest[] }> = ({ rows }) =>
  rows.length === 0 ? (
    <p className="sm muted" style={{ margin: 0 }}>
      No decisions yet.
    </p>
  ) : (
    <div className="scroll">
      <table className="tbl" style={{ minWidth: 560 }}>
        <thead>
          <tr>
            <th>Request</th>
            <th>Outcome</th>
            <th>Decided by</th>
            <th>Reason given</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id}>
              <td style={{ maxWidth: 220 }}>
                <RequestTitle r={r} />
                <div className="hint">
                  {REQUEST_KIND[r.kind]}
                  {r.to_label ? ` to ${r.to_label}` : ''}
                </div>
              </td>
              <td>
                <RequestStatus r={r} />
                {r.result_memory_id && (
                  <div className="hint">
                    <a href={memoryHref(r.result_memory_id)}>Published memory</a>
                  </div>
                )}
              </td>
              <td className="sm">
                {r.decided_by ?? '—'}
                <div className="hint">
                  {[r.decided_by_role === 'project_admin' ? 'Project admin' : r.decided_by_role ? 'Platform admin' : null, r.decided_at ? fmtMonthDay(r.decided_at) : null]
                    .filter(Boolean)
                    .join(' · ')}
                </div>
              </td>
              <td className="sm">{r.reason || '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );

const HowRequestsMove: React.FC = () => (
  <aside className="card" style={{ flex: '1 1 300px' }}>
    <h2 data-h="3" style={{ marginBottom: 14 }}>
      How a request moves
    </h2>
    <ol className="steps">
      <li className="done">
        <span>1</span>You submit
      </li>
      <li className="on">
        <span>2</span>A reviewer reads this record only
      </li>
      <li>
        <span>3</span>Decision + reason
      </li>
      <li>
        <span>4</span>Approved: published at the target scope
      </li>
    </ol>
    <p className="sm muted" style={{ margin: '14px 0 0' }}>
      While a request is open, the reviewer can read <b>the submitted version of that one memory</b> — nothing else in your personal memory. The read is logged in My
      activity. You can withdraw until a decision is made.
    </p>
  </aside>
);

export default MyRequests;
