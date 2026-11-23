import React, { useState } from 'react';
import { api } from '@/api/client';
import { Empty, ErrorState, Icon, Modal, ScopeLabel, SkTable, useToast } from '@/components/ui';
import { useApi } from '@/hooks/useApi';
import type { MyRequest, MyRequestKind, ProjectsResponse, ReviewQueueResponse, ReviewRequestDetail } from '@/types';
import { errorMessage, fmtMonthDay, shortId } from '@/utils/format';
import { MemoryPicker } from './common';

/** Building blocks for requests, shared by My requests and the reviewers' queue (#719). */

export const REQUEST_KIND: Record<MyRequestKind, string> = {
  promotion: 'Promotion',
  shared_knowledge: 'Shared knowledge',
};

/** The scope a request starts from and the one it asks for, as two labels. */
export const ScopeStep: React.FC<{ scope?: string | null; label?: string | null }> = ({ scope, label }) =>
  scope ? <ScopeLabel scope={scope}>{label || undefined}</ScopeLabel> : null;

export const ScopeChain: React.FC<{ r: Pick<MyRequest, 'from_scope' | 'from_label' | 'to_scope' | 'to_label'> }> = ({ r }) => (
  <span className="chain">
    <ScopeStep scope={r.from_scope} label={r.from_label} />
    {r.from_scope && r.to_scope && (
      <span className="muted" aria-label="to">
        {' '}
        →{' '}
      </span>
    )}
    <ScopeStep scope={r.to_scope} label={r.to_label} />
  </span>
);

/** Where an open request is waiting, or how it ended. */
export const RequestStatus: React.FC<{ r: MyRequest; short?: boolean }> = ({ r, short }) => {
  if (r.state === 'withdrawn') return <span className="lbl g">Withdrawn</span>;
  if (r.state === 'decided')
    return r.outcome === 'rejected' ? (
      <span className="lbl red">
        <Icon.X /> Rejected
      </span>
    ) : (
      <span className="lbl grn">
        <Icon.Check /> Approved
      </span>
    );
  return (
    <span className="lbl ylw pill-pending">
      <Icon.Clock />
      {short ? 'Pending' : r.waiting_for === 'project_owner' ? 'Waiting for a project admin' : 'Waiting for review'}
    </span>
  );
};

export const requestIds = (r: MyRequest) => (
  <span className="mono hint">
    {r.id}
    {r.memory_id ? ` · ${shortId(r.memory_id)} v${r.memory_version}` : ''}
  </span>
);

/** Submit a new request. The reviewer sees the submitted version of that one memory only. */
export const NewRequestModal: React.FC<{ onClose: () => void; onCreated: (r: MyRequest) => void; initialKind?: MyRequestKind }> = ({
  onClose,
  onCreated,
  initialKind = 'promotion',
}) => {
  const toast = useToast();
  const [kind, setKind] = useState<MyRequestKind>(initialKind);
  const [memory, setMemory] = useState<{ id: string; stub: string; scope: string } | null>(null);
  const [target, setTarget] = useState<'project' | 'organizational'>('organizational');
  const [project, setProject] = useState('');
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const projects = useApi<ProjectsResponse>(() => api.projects(true), []);
  const mine = (projects.data?.items ?? []).filter((p) => p.my_role);
  // Personal → project or organizational; project → organizational (the BFF enforces the same).
  const projectTarget = memory?.scope === 'user';
  const writable = !memory || memory.scope === 'user' || memory.scope === 'project';
  const effectiveTarget = projectTarget ? target : 'organizational';
  const ready = Boolean(memory) && writable && (effectiveTarget !== 'project' || Boolean(project));

  const submit = async () => {
    if (!memory) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await api.createMyRequest({
        kind,
        memory_id: memory.id,
        target_scope: effectiveTarget,
        project: effectiveTarget === 'project' ? project : undefined,
        reason: reason.trim() || undefined,
      });
      toast(`Request ${r.id} submitted. You can follow it in My requests.`);
      onCreated(r);
      onClose();
    } catch (e) {
      setErr(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title="New request"
      onClose={onClose}
      actions={
        <>
          <button type="button" className="btn bp" disabled={!ready || busy} onClick={submit}>
            {busy ? 'Submitting…' : 'Submit request'}
          </button>
          <button type="button" className="btn bg" onClick={onClose}>
            Cancel
          </button>
        </>
      }
    >
      <div className="form-stack">
        <div>
          <label htmlFor="nr-kind" className="flabel">
            What do you need?
          </label>
          <select id="nr-kind" className="in" value={kind} onChange={(e) => setKind(e.target.value as MyRequestKind)}>
            <option value="promotion">Promote a memory to a wider scope</option>
            <option value="shared_knowledge">Submit a memory as shared knowledge</option>
          </select>
        </div>
        {memory ? (
          <div>
            <span className="flabel">Memory</span>
            <div className="chip">
              <ScopeLabel scope={memory.scope} />
              <span className="ellipsis" style={{ maxWidth: 320 }}>
                {memory.stub}
              </span>
              <button type="button" aria-label="Choose another memory" onClick={() => setMemory(null)}>
                ✕
              </button>
            </div>
            {!writable && <p className="hint" style={{ color: "#a30000" }}>You can only submit your personal memories and your projects' memories.</p>}
          </div>
        ) : (
          <div>
            <span className="flabel">Memory</span>
            <MemoryPicker onPick={setMemory} placeholder="Search your memories" />
          </div>
        )}
        <div>
          <label htmlFor="nr-target" className="flabel">
            Target scope
          </label>
          <select
            id="nr-target"
            className="in"
            value={effectiveTarget}
            disabled={!projectTarget}
            onChange={(e) => setTarget(e.target.value as 'project' | 'organizational')}
          >
            {projectTarget && <option value="project">Project</option>}
            <option value="organizational">Organizational</option>
          </select>
          {memory?.scope === 'project' && <p className="hint">A project memory can only move up to Organizational.</p>}
        </div>
        {effectiveTarget === 'project' && (
          <div>
            <label htmlFor="nr-project" className="flabel">
              Project
            </label>
            <select id="nr-project" className="in" value={project} onChange={(e) => setProject(e.target.value)}>
              <option value="">Choose one of your projects</option>
              {mine.map((p) => (
                <option key={p.name} value={p.name}>
                  {p.name}
                </option>
              ))}
            </select>
          </div>
        )}
        <div>
          <label htmlFor="nr-reason" className="flabel">
            Why (shown to the reviewer)
          </label>
          <textarea id="nr-reason" className="in" rows={3} value={reason} onChange={(e) => setReason(e.target.value)} />
        </div>
        <p className="hint" style={{ margin: 0 }}>
          A project admin decides project targets; a platform admin decides organizational ones. While the request is open the reviewer can read the submitted
          version of that one memory — nothing else in your personal memory — and the read shows in My activity.
        </p>
        {err && <div className="alert err-a">{err}</div>}
      </div>
    </Modal>
  );
};

/**
 * The reviewer's queue: open requests this person may decide (GET /api/review/requests).
 * A row carries no content; opening one reads the submitted snapshot, which the
 * requester sees as a read in My activity.
 */
export const RequestQueue: React.FC<{ onChanged?: () => void }> = ({ onChanged }) => {
  const toast = useToast();
  const { data, error, loading, reload } = useApi<ReviewQueueResponse>(() => api.reviewRequests(), [], 30000);
  const [openId, setOpenId] = useState<string | null>(null);
  const items = data?.items ?? [];

  return (
    <section className="card">
      <div className="ch">
        <h2 data-h="3">Requests to decide</h2>
        <span className="sm muted">{items.length} open</span>
      </div>
      {error && !data ? (
        <ErrorState error={error} onRetry={reload} />
      ) : loading && !data ? (
        <SkTable rows={3} cols={5} />
      ) : items.length === 0 ? (
        <Empty compact title="Nothing to decide">
          Promotion and shared-knowledge requests you may decide appear here. Withdrawn requests leave the queue.
        </Empty>
      ) : (
        <div className="scroll">
          <table className="tbl" style={{ minWidth: 720 }}>
            <thead>
              <tr>
                <th>Request</th>
                <th>Type</th>
                <th>Scope</th>
                <th>Submitted</th>
                <th>
                  <span className="vh">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {items.map((r) => (
                <tr key={r.id}>
                  <td className="mono sm">{r.id}</td>
                  <td className="sm">{REQUEST_KIND[r.kind]}</td>
                  <td>
                    <ScopeChain r={r} />
                  </td>
                  <td className="sm">
                    {fmtMonthDay(r.submitted_at)}
                    <div className="hint">by {r.requester}</div>
                  </td>
                  <td>
                    <button type="button" className="btn bl sm" onClick={() => setOpenId(r.id)}>
                      Review
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {openId && (
        <DecideModal
          id={openId}
          onClose={() => setOpenId(null)}
          onDecided={(outcome) => {
            toast(outcome === 'approved' ? 'Approved. The memory is published at the target scope.' : 'Rejected. The requester sees your reason.');
            reload();
            onChanged?.();
          }}
        />
      )}
    </section>
  );
};

const DecideModal: React.FC<{ id: string; onClose: () => void; onDecided: (outcome: 'approved' | 'rejected') => void }> = ({ id, onClose, onDecided }) => {
  const { data, error } = useApi<ReviewRequestDetail>(() => api.reviewRequest(id), [id]);
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const decide = async (outcome: 'approved' | 'rejected') => {
    setBusy(true);
    setErr(null);
    try {
      await api.decideRequest(id, { outcome, reason: reason.trim() || undefined });
      onDecided(outcome);
      onClose();
    } catch (e) {
      setErr(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      title={`Request ${id}`}
      onClose={onClose}
      actions={
        <>
          <button type="button" className="btn bp" disabled={!data || busy} onClick={() => decide('approved')}>
            Approve
          </button>
          <button type="button" className="btn bl" disabled={!data || busy || !reason.trim()} onClick={() => decide('rejected')}>
            Reject
          </button>
          <button type="button" className="btn bg" onClick={onClose}>
            Cancel
          </button>
        </>
      }
    >
      {error && !data ? (
        <ErrorState error={error} title="This request is no longer open" />
      ) : !data ? (
        <SkTable rows={2} cols={2} />
      ) : (
        <div className="form-stack">
          <div className="sm">
            {REQUEST_KIND[data.kind]} from <b>{data.requester}</b> · <ScopeChain r={data} /> · submitted {fmtMonthDay(data.submitted_at)}
          </div>
          <div>
            <span className="flabel">Submitted version (v{data.memory_version})</span>
            <div className="card" style={{ whiteSpace: 'pre-wrap', margin: 0 }}>
              {data.content}
            </div>
          </div>
          {data.reason && (
            <div>
              <span className="flabel">Why</span>
              <p style={{ margin: 0 }}>{data.reason}</p>
            </div>
          )}
          <div>
            <label htmlFor="dr-reason" className="flabel">
              Your reason (required to reject, shown to the requester)
            </label>
            <textarea id="dr-reason" className="in" rows={2} value={reason} onChange={(e) => setReason(e.target.value)} />
          </div>
          <p className="hint" style={{ margin: 0 }}>
            Approving publishes exactly this version at the target scope, linked to the source. If the memory changed since it was submitted, approval is refused and the
            requester can submit it again.
          </p>
          {err && <div className="alert err-a">{err}</div>}
        </div>
      )}
    </Modal>
  );
};
