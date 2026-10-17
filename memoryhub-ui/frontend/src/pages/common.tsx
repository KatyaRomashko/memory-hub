import React, { useState } from 'react';
import { api } from '@/api/client';
import { useApp } from '@/components/Shell';
import { Modal, useToast } from '@/components/ui';
import { buildHash } from '@/hooks/useHashRoute';
import type { AuditEvent } from '@/types';
import { errorMessage, SCOPE_ORDER, scopeMeta, truncate } from '@/utils/format';

export const memoryHref = (id: string) => buildHash(`/memory/${id}`);

export const MemoryLink: React.FC<{ id: string; children: React.ReactNode; style?: React.CSSProperties; className?: string }> = ({
  id,
  children,
  style,
  className,
}) => (
  <a href={memoryHref(id)} style={style} className={className}>
    {children}
  </a>
);

/** Human label and colour for an audit decision, as drawn in the design. */
export const decisionOf = (e: AuditEvent): { label: string; cls: string; note?: string } => {
  const meta = e.metadata || {};
  const reason = (meta.rule as string) || (meta.reason as string) || (meta.detail as string) || '';
  if (e.decision === 'denied') {
    if (e.event_type === 'memory.write' && reason) return { label: `Blocked · ${truncate(reason, 24)}`, cls: 'err', note: reason ? `rule ${reason}` : undefined };
    return { label: 'Denied', cls: 'err', note: reason || undefined };
  }
  if (e.event_type === 'memory.contradiction_reported') return { label: 'Logged', cls: 'muted' };
  if (meta.queued || e.event_type === 'review.promotion_proposed') return { label: 'Queued for review', cls: 'warn', note: 'queued for review' };
  if (e.event_type === 'review.reject') return { label: 'Rejected', cls: 'err' };
  return { label: 'Permitted', cls: 'ok' };
};

export const actorKind = (e: AuditEvent): string => {
  if (meta(e).via === 'dashboard') return 'human · dashboard';
  if (e.actor_type === 'service') return 'service agent';
  if (e.driver_id && e.driver_id !== e.actor_id) return 'user agent';
  if (e.actor_type === 'user') return 'user';
  return e.actor_id.includes('agent') || e.actor_id.includes('curator') ? 'service agent' : 'user';
};

const meta = (e: AuditEvent) => (e.metadata || {}) as Record<string, unknown>;

export const actorName = (e: AuditEvent) =>
  e.driver_id && e.driver_id !== e.actor_id ? `${e.actor_id} · ${e.driver_id}` : e.actor_id;

/** The "Memory" / "Target" cell of an audit row. */
export const EventTarget: React.FC<{ e: AuditEvent; max?: number }> = ({ e, max = 80 }) => {
  const m = meta(e);
  if (e.event_type === 'memory.search') {
    const q = (m.query as string) || 'search';
    const n = (m.result_count as number) ?? (Array.isArray(m.result_ids) ? (m.result_ids as unknown[]).length : null);
    return (
      <span>
        “{truncate(q, max)}”{n !== null && n !== undefined ? ` · ${n} results` : ''}
      </span>
    );
  }
  if (e.memory_id && e.memory_stub) return <MemoryLink id={e.memory_id}>{truncate(e.memory_stub, max)}</MemoryLink>;
  if (e.memory_id) return <MemoryLink id={e.memory_id}>{`mem ${e.memory_id.slice(0, 8)}…`}</MemoryLink>;
  if (typeof m.name === 'string') return <span>{m.name}</span>;
  return <span className="muted">—</span>;
};

/* ------------------------------------------------------------------ */
/* Write memory dialog (Overview, Memory browser)                      */
/* ------------------------------------------------------------------ */

export const WriteMemoryModal: React.FC<{ onClose: () => void; onSaved?: (id: string) => void }> = ({ onClose, onSaved }) => {
  const toast = useToast();
  const { me, refreshCounts } = useApp();
  const [content, setContent] = useState('');
  const [scope, setScope] = useState('user');
  const [owner, setOwner] = useState('');
  const [scopeId, setScopeId] = useState('');
  const [weight, setWeight] = useState(0.7);
  const [domains, setDomains] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const needsScopeId = ['project', 'role', 'campaign'].includes(scope);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await api.writeMemory({
        content: content.trim(),
        scope,
        weight,
        owner_id: owner.trim() || (needsScopeId ? scopeId.trim() : undefined),
        scope_id: needsScopeId ? scopeId.trim() : undefined,
        domains: domains.split(',').map((d) => d.trim()).filter(Boolean),
      });
      toast(scope === 'enterprise' ? 'Memory written and sent to the review queue for approval.' : 'Memory written.');
      refreshCounts();
      onSaved?.(res.id);
      onClose();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title="Write memory" onClose={onClose}>
      <form onSubmit={submit} style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        <div className="f">
          <label htmlFor="wm-content">Content</label>
          <textarea id="wm-content" className="in" required value={content} onChange={(e) => setContent(e.target.value)} />
          <span className="hint">Runs through the same curation rules as an agent’s write_memory call.</span>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
          <div className="f">
            <label htmlFor="wm-scope">Scope</label>
            <select id="wm-scope" className="in" value={scope} onChange={(e) => setScope(e.target.value)}>
              {SCOPE_ORDER.map((s) => (
                <option key={s} value={s}>
                  {scopeMeta(s).label}
                </option>
              ))}
            </select>
          </div>
          {needsScopeId ? (
            <div className="f">
              <label htmlFor="wm-sid">{scopeMeta(scope).label} ID</label>
              <input id="wm-sid" className="in" required value={scopeId} onChange={(e) => setScopeId(e.target.value)} placeholder={scope === 'project' ? 'memoryhub-ui' : ''} />
            </div>
          ) : (
            <div className="f">
              <label htmlFor="wm-owner">Owner</label>
              <input id="wm-owner" className="in" value={owner} onChange={(e) => setOwner(e.target.value)} placeholder={scope === 'user' ? me?.username : 'tenant'} />
            </div>
          )}
        </div>
        <div className="f">
          <label htmlFor="wm-weight">Weight · {weight.toFixed(2)}</label>
          <input id="wm-weight" type="range" min={0} max={100} value={Math.round(weight * 100)} onChange={(e) => setWeight(Number(e.target.value) / 100)} />
        </div>
        <div className="f">
          <label htmlFor="wm-domains">Domain tags</label>
          <input id="wm-domains" className="in" value={domains} onChange={(e) => setDomains(e.target.value)} placeholder="containers, tooling" />
        </div>
        {error && <div className="alert err-a">{error}</div>}
        <div className="modal-actions">
          <button type="submit" className="btn bp" disabled={busy || !content.trim()}>
            {busy ? 'Writing…' : 'Write memory'}
          </button>
          <button type="button" className="btn bg" onClick={onClose}>
            Cancel
          </button>
        </div>
      </form>
    </Modal>
  );
};

/** Bar chart of daily searches and writes, drawn like the design's SVG. */
export const ActivityChart: React.FC<{ data: { date: string; searches: number; writes: number }[] }> = ({ data }) => {
  const max = Math.max(1, ...data.map((d) => Math.max(d.searches, d.writes)));
  const step = niceStep(max / 4);
  const top = step * 4;
  const base = 198;
  const y = (v: number) => base - (v / top) * 178;
  const slot = 600 / Math.max(1, data.length);
  const label = (iso: string) => new Date(`${iso}T00:00:00`).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  const mid = Math.floor((data.length - 1) / 2);
  const total = data.reduce((a, d) => a + d.searches + d.writes, 0);
  return (
    <svg viewBox="0 0 640 220" role="img" aria-label={`Daily searches and writes for the last ${data.length} days, ${total} operations in total`} style={{ width: '100%', height: 'auto', display: 'block' }}>
      <g stroke="#e6e6e6" strokeWidth="1">
        {[4, 3, 2, 1].map((k) => (
          <line key={k} x1="40" y1={y(step * k)} x2="640" y2={y(step * k)} />
        ))}
      </g>
      <g fontSize="11" fill="#4d4d4d" fontFamily="Red Hat Text">
        {[4, 3, 2, 1].map((k) => (
          <text key={k} x="0" y={y(step * k) + 4}>
            {fmtTick(step * k)}
          </text>
        ))}
        {data.length > 0 && (
          <>
            <text x={52} y="214">{label(data[0].date)}</text>
            <text x={52 + mid * slot} y="214">{label(data[mid].date)}</text>
            <text x={52 + (data.length - 1) * slot - 12} y="214">{label(data[data.length - 1].date)}</text>
          </>
        )}
      </g>
      {data.map((d, i) => {
        const x = 52 + i * slot;
        return (
          <g key={d.date}>
            <title>{`${label(d.date)}: ${d.searches.toLocaleString()} searches, ${d.writes.toLocaleString()} writes & updates`}</title>
            {d.searches > 0 && <rect x={x} y={y(d.searches)} width="16" height={base - y(d.searches)} rx="3" fill="#0066cc" />}
            {d.writes > 0 && <rect x={x + 18} y={y(d.writes)} width="10" height={base - y(d.writes)} rx="2" fill="#92c5f9" />}
          </g>
        );
      })}
    </svg>
  );
};

function niceStep(raw: number): number {
  if (raw <= 1) return 1;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const n = raw / mag;
  const nice = n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10;
  return nice * mag;
}
const fmtTick = (v: number) => (v >= 1000 ? `${Math.round(v / 100) / 10}k`.replace('.0k', 'k') : String(Math.round(v)));

/* ------------------------------------------------------------------ */
/* Memory picker (relationships, graph focus)                          */
/* ------------------------------------------------------------------ */

export const MemoryPicker: React.FC<{
  onPick: (m: { id: string; stub: string; scope: string }) => void;
  exclude?: string[];
  placeholder?: string;
  autoFocus?: boolean;
}> = ({ onPick, exclude = [], placeholder = 'Search memories by text', autoFocus }) => {
  const [q, setQ] = useState('');
  const [items, setItems] = useState<{ id: string; stub: string; scope: string }[]>([]);
  const [busy, setBusy] = useState(false);
  React.useEffect(() => {
    let cancelled = false;
    const t = window.setTimeout(() => {
      setBusy(true);
      api
        .browse({ q: q.trim() || undefined, mode: 'semantic', sort: q.trim() ? 'relevance' : 'updated', per_page: 8 })
        .then((r) => !cancelled && setItems(r.items.filter((i) => !exclude.includes(i.id))))
        .catch(() => !cancelled && setItems([]))
        .finally(() => !cancelled && setBusy(false));
    }, 250);
    return () => {
      cancelled = true;
      window.clearTimeout(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q]);
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <input className="in" value={q} onChange={(e) => setQ(e.target.value)} placeholder={placeholder} aria-label={placeholder} autoFocus={autoFocus} />
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4, maxHeight: 260, overflow: 'auto' }}>
        {busy && items.length === 0 && <span className="sm muted">Searching…</span>}
        {!busy && items.length === 0 && <span className="sm muted">No matching memories.</span>}
        {items.map((m) => (
          <button key={m.id} type="button" className="item" style={{ padding: '8px 12px', display: 'flex', gap: 8, alignItems: 'center' }} onClick={() => onPick(m)}>
            <span className={`lbl ${scopeMeta(m.scope).cls}`}>{scopeMeta(m.scope).label}</span>
            <span className="ellipsis" style={{ flex: 1 }}>{m.stub}</span>
          </button>
        ))}
      </div>
    </div>
  );
};

export const REL_TYPES = ['related_to', 'derived_from', 'supersedes', 'conflicts_with'] as const;

/** Create a relationship from one or more memories to a picked target. */
export const RelationshipModal: React.FC<{ sourceIds: string[]; onClose: () => void; onDone: () => void }> = ({ sourceIds, onClose, onDone }) => {
  const toast = useToast();
  const [type, setType] = useState<string>('related_to');
  const [target, setTarget] = useState<{ id: string; stub: string; scope: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    if (!target) return;
    setBusy(true);
    setError(null);
    try {
      if (sourceIds.length === 1) {
        await api.createRelationship({ source_id: sourceIds[0], target_id: target.id, relationship_type: type });
      } else {
        const res = await api.bulk({ action: 'relate', ids: sourceIds, target_id: target.id, relationship_type: type });
        if (res.errors.length) throw new Error(res.errors.map((e) => e.detail).join('; '));
      }
      toast(`Relationship ${type} created.`);
      onDone();
      onClose();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      title={sourceIds.length > 1 ? `Add relationship from ${sourceIds.length} memories` : 'Add relationship'}
      onClose={onClose}
      actions={
        <>
          <button type="button" className="btn bp" disabled={!target || busy} onClick={submit}>
            {busy ? 'Creating…' : 'Create relationship'}
          </button>
          <button type="button" className="btn bg" onClick={onClose}>
            Cancel
          </button>
        </>
      }
    >
      <div className="f">
        <label htmlFor="rel-type">Relationship</label>
        <select id="rel-type" className="in" value={type} onChange={(e) => setType(e.target.value)}>
          {REL_TYPES.map((t) => (
            <option key={t}>{t}</option>
          ))}
        </select>
      </div>
      <div className="f">
        <label>Target memory</label>
        {target ? (
          <div className="br" style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <span className={`lbl ${scopeMeta(target.scope).cls}`}>{scopeMeta(target.scope).label}</span>
            <span style={{ flex: 1 }}>{target.stub}</span>
            <button type="button" className="btn bl sm" onClick={() => setTarget(null)}>
              Change
            </button>
          </div>
        ) : (
          <MemoryPicker onPick={setTarget} exclude={sourceIds} autoFocus />
        )}
      </div>
      {error && <div className="alert err-a">{error}</div>}
    </Modal>
  );
};

export const ConfirmModal: React.FC<{
  title: string;
  children: React.ReactNode;
  confirmLabel: string;
  danger?: boolean;
  onConfirm: () => Promise<void> | void;
  onClose: () => void;
}> = ({ title, children, confirmLabel, danger, onConfirm, onClose }) => {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <Modal
      title={title}
      onClose={onClose}
      actions={
        <>
          <button
            type="button"
            className={`btn ${danger ? 'bd' : 'bp'}`}
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              setError(null);
              try {
                await onConfirm();
                onClose();
              } catch (err) {
                setError(errorMessage(err));
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? 'Working…' : confirmLabel}
          </button>
          <button type="button" className="btn bg" onClick={onClose}>
            Cancel
          </button>
        </>
      }
    >
      <div style={{ fontSize: 14, lineHeight: 1.5 }}>{children}</div>
      {error && <div className="alert err-a">{error}</div>}
    </Modal>
  );
};
