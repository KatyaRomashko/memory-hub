import React, { useEffect, useMemo, useState } from 'react';
import { api } from '@/api/client';
import { PageHeader, Tabs } from '@/components/Shell';
import { Empty, ErrorState, ScopeLabel, SkLines, Switch, useToast } from '@/components/ui';
import { useApi } from '@/hooks/useApi';
import { buildHash, navigate } from '@/hooks/useHashRoute';
import type { DreamInsight, DreamKind, DreamReport, DreamSettings, DreamsResponse } from '@/types';
import { errorMessage, fmtInt, fmtRelative, fmtWeight, shortId } from '@/utils/format';
import { WriteMemoryModal } from './common';

/**
 * Dreaming — while you are away, MemoryHub reviews your memories the way sleep
 * consolidates the day: it spots duplicates, missing links, memories nobody
 * uses any more, contradictions and themes worth one consolidated memory.
 * Nothing changes without your OK, except the safe kinds you let run alone.
 */

const KIND: Record<DreamKind, { label: string; verb: string; cls: string; icon: string; blurb: string }> = {
  duplicates: { label: 'Duplicates', verb: 'Merge', cls: 'org', icon: 'M8 4h10v10H8zM4 8h2v12h12v-2', blurb: 'Near-identical memories: keep the newer one, fade the older.' },
  connections: { label: 'Connections', verb: 'Link them', cls: 'blue', icon: 'M9 15l6-6M10 6l1-1a4 4 0 016 6l-1 1M14 18l-1 1a4 4 0 01-6-6l1-1', blurb: 'Related memories without a link, so agents retrieve them together.' },
  fading: { label: 'Fading', verb: 'Lower weight', cls: 'g', icon: 'M12 3v18M5 10l7 7 7-7', blurb: 'Weighty memories nobody read or changed for 60 days.' },
  contradictions: { label: 'Contradictions', verb: 'Review', cls: 'red', icon: 'M12 3l9 16H3zM12 10v4M12 17h.01', blurb: 'Memories agents keep reporting as wrong.' },
  themes: { label: 'Themes', verb: 'Write summary', cls: 'pur', icon: 'M4 6h16M4 12h10M4 18h7', blurb: 'Many recent memories on one topic — consolidate them into one.' },
};
const KINDS = Object.keys(KIND) as DreamKind[];

const KindIcon: React.FC<{ kind: DreamKind }> = ({ kind }) => (
  <span className={`dream-ic ${KIND[kind].cls}`} aria-hidden="true">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d={KIND[kind].icon} />
    </svg>
  </span>
);

const nextRun = (s: DreamSettings): string => {
  if (s.schedule === 'off') return 'Off — dream only when you ask';
  const [h, m] = s.window.split(':').map(Number);
  const next = new Date();
  next.setHours(h, m, 0, 0);
  if (next <= new Date()) next.setDate(next.getDate() + 1);
  if (s.schedule === 'weekly') while (next.getDay() !== 0) next.setDate(next.getDate() + 1);
  const soon = next.getTime() - Date.now() < 24 * 3600_000;
  const day = s.schedule === 'nightly' || soon ? (h < 6 ? 'Tonight' : 'Today') : next.toLocaleDateString(undefined, { weekday: 'long' });
  return `${day} at ${s.window}`;
};

/** The most recent scheduled time that has already passed. */
const lastDue = (s: DreamSettings): Date | null => {
  if (s.schedule === 'off') return null;
  const [h, m] = s.window.split(':').map(Number);
  const due = new Date();
  due.setHours(h, m, 0, 0);
  if (due > new Date()) due.setDate(due.getDate() - 1);
  if (s.schedule === 'weekly') while (due.getDay() !== 0) due.setDate(due.getDate() - 1);
  return due;
};

const Dreaming: React.FC = () => {
  const toast = useToast();
  const { data, error, loading, reload, setData } = useApi<DreamsResponse>(() => api.dreams(), []);
  const [kind, setKind] = useState<'all' | DreamKind>('all');
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState<{ insight: DreamInsight; text: string } | null>(null);
  const report = data?.last ?? null;

  // Scheduled dreams: there is no background worker yet, so a dream that was
  // due while you were away runs the next time you open this page.
  const [caughtUp, setCaughtUp] = useState(false);
  useEffect(() => {
    if (!data || caughtUp || busy) return;
    setCaughtUp(true);
    const due = lastDue(data.settings);
    if (due && (!data.last || new Date(data.last.started_at) < due)) void dreamNow('scheduled');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  const open = useMemo(() => (report?.insights ?? []).filter((i) => i.status === 'proposed'), [report]);
  const shown = (report?.insights ?? []).filter((i) => kind === 'all' || i.kind === kind);

  const dreamNow = async (trigger: 'manual' | 'scheduled' = 'manual') => {
    setBusy(true);
    try {
      const res = await api.dreamNow(trigger);
      toast(`Dream finished: ${fmtInt(res.reviewed)} memories reviewed, ${res.insights.length} insights.`);
      reload();
    } catch (e) {
      toast(errorMessage(e), 'err');
    } finally {
      setBusy(false);
    }
  };

  const patchInsight = (r: DreamReport, next: DreamInsight) => {
    if (!data) return;
    setData({ ...data, last: { ...r, insights: r.insights.map((i) => (i.id === next.id ? next : i)) } });
  };

  const decide = async (i: DreamInsight, decision: 'accept' | 'dismiss') => {
    if (!report) return;
    if (decision === 'accept' && i.action.type === 'consolidate') {
      setDraft({ insight: i, text: String(i.action.draft ?? '') });
      return;
    }
    try {
      const next = await api.decideInsight(report.id, i.id, decision);
      patchInsight(report, next);
      toast(decision === 'accept' ? next.result ?? 'Done.' : 'Dismissed.');
      if (decision === 'accept' && i.action.type === 'review') navigate(`/memory/${String(i.action.memory)}`, { tab: 'contradictions' });
    } catch (e) {
      toast(errorMessage(e), 'err');
    }
  };

  const saveSettings = async (patch: Partial<DreamSettings>) => {
    if (!data) return;
    try {
      const settings = await api.saveDreamSettings(patch);
      setData({ ...data, settings });
    } catch (e) {
      toast(errorMessage(e), 'err');
    }
  };

  return (
    <>
      <PageHeader
        title="Dreaming"
        description="While you are away, MemoryHub reviews your memories the way sleep consolidates a day: duplicates, missing links, memories nobody uses, contradictions and themes. Nothing changes without your OK."
        actions={
          <button type="button" className="btn bp" disabled={busy || !data} onClick={() => dreamNow()}>
            <MoonIcon />
            {busy ? 'Dreaming…' : 'Dream now'}
          </button>
        }
        tabs={
          report ? (
            <Tabs
              label="Insight kind"
              value={kind}
              onChange={setKind}
              tabs={[
                { value: 'all', label: 'All', count: report.insights.length },
                ...KINDS.map((k) => ({ value: k, label: KIND[k].label, count: report.counts[k] ?? 0 })),
              ]}
            />
          ) : undefined
        }
      />
      <div className="body">
        {error && !data ? (
          <ErrorState error={error} onRetry={reload} />
        ) : loading && !data ? (
          <div className="card">
            <SkLines lines={10} />
          </div>
        ) : (
          <div className="row-wrap">
            <div className="col-main">
              <section className="card dream-hero">
                <div className="dream-sky" aria-hidden="true">
                  {[12, 30, 54, 71, 88, 40, 64].map((x, i) => (
                    <i key={i} style={{ left: `${x}%`, top: `${[22, 60, 30, 70, 40, 15, 52][i]}%` }} />
                  ))}
                </div>
                <div className="dream-hero-in">
                  <div>
                    <div className="sm" style={{ color: '#c7c7c7' }}>
                      Next dream
                    </div>
                    <div className="num-lg" style={{ color: '#fff' }}>
                      {data ? nextRun(data.settings) : '—'}
                    </div>
                  </div>
                  <div>
                    <div className="sm" style={{ color: '#c7c7c7' }}>
                      Last dream
                    </div>
                    <div className="num-lg" style={{ color: '#fff' }}>
                      {report ? fmtRelative(report.started_at) : 'Never'}
                    </div>
                    {report && (
                      <div className="sm" style={{ color: '#c7c7c7' }}>
                        {fmtInt(report.reviewed)} memories reviewed in {(report.duration_ms / 1000).toFixed(1)} s
                      </div>
                    )}
                  </div>
                  <div>
                    <div className="sm" style={{ color: '#c7c7c7' }}>
                      Waiting for you
                    </div>
                    <div className="num-lg" style={{ color: '#fff' }}>
                      {open.length}
                    </div>
                  </div>
                </div>
              </section>

              {!report ? (
                <section className="card">
                  <Empty
                    title="No dreams yet"
                    action={
                      <button type="button" className="btn bp" disabled={busy} onClick={() => dreamNow()}>
                        Dream now
                      </button>
                    }
                  >
                    Your first dream reviews the memories you can read and proposes housekeeping. It takes a few seconds and changes nothing on its own.
                  </Empty>
                </section>
              ) : shown.length === 0 ? (
                <section className="card">
                  <Empty title={kind === 'all' ? 'A quiet night' : `No ${KIND[kind as DreamKind].label.toLowerCase()} this time`}>
                    {kind === 'all' ? 'Nothing needed attention. Your memories look tidy.' : KIND[kind as DreamKind].blurb}
                  </Empty>
                </section>
              ) : (
                shown.map((i) => <InsightCard key={i.id} insight={i} onDecide={decide} />)
              )}
            </div>

            <aside className="col-side">
              {data && (
                <section className="card">
                  <h3>Schedule</h3>
                  <div className="seg full" role="group" aria-label="Dream schedule" style={{ marginTop: 14 }}>
                    {(['nightly', 'weekly', 'off'] as const).map((s) => (
                      <button key={s} type="button" className={data.settings.schedule === s ? 'on' : ''} aria-pressed={data.settings.schedule === s} onClick={() => saveSettings({ schedule: s })}>
                        {s === 'nightly' ? 'Nightly' : s === 'weekly' ? 'Weekly' : 'Off'}
                      </button>
                    ))}
                  </div>
                  {data.settings.schedule !== 'off' && (
                    <div className="f" style={{ marginTop: 14 }}>
                      <label htmlFor="dw">Dream at (your time)</label>
                      <input id="dw" className="in auto" type="time" value={data.settings.window} onChange={(e) => e.target.value && saveSettings({ window: e.target.value })} />
                      <span className="hint">The dream runs at this time — or, if you were away, the next time you open Dreaming.</span>
                    </div>
                  )}

                  <hr className="sep" style={{ margin: '18px 0' }} />
                  <p className="fh">What to look for</p>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                    {KINDS.map((k) => (
                      <div className="swrow" key={k}>
                        <span style={{ display: 'flex', gap: 10, alignItems: 'flex-start' }}>
                          <KindIcon kind={k} />
                          <span>
                            <span style={{ display: 'block' }}>{KIND[k].label}</span>
                            <span className="hint">{KIND[k].blurb}</span>
                          </span>
                        </span>
                        <Switch on={data.settings.kinds[k]} onChange={(on) => saveSettings({ kinds: { ...data.settings.kinds, [k]: on } })} label={KIND[k].label} />
                      </div>
                    ))}
                  </div>

                  <hr className="sep" style={{ margin: '18px 0' }} />
                  <p className="fh">Let dreams act on their own</p>
                  <p className="hint" style={{ margin: '-4px 0 10px' }}>
                    Only reversible changes. Merges, reviews and summaries always wait for you.
                  </p>
                  {(['connections', 'fading'] as const).map((k) => (
                    <div className="swrow" key={k} style={{ marginBottom: 10 }}>
                      <span>{k === 'connections' ? 'Add missing links' : 'Lower the weight of unused memories'}</span>
                      <Switch on={data.settings.auto[k]} onChange={(on) => saveSettings({ auto: { ...data.settings.auto, [k]: on } })} label={k} />
                    </div>
                  ))}
                </section>
              )}

              {data && data.history.length > 0 && (
                <section className="card">
                  <h3>Dream journal</h3>
                  <div style={{ display: 'flex', flexDirection: 'column', marginTop: 10 }}>
                    {data.history.map((h) => (
                      <div key={h.id} className="hrow">
                        <span>
                          <b className="sm">{new Date(h.started_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}</b>
                          <span className="hint" style={{ display: 'block' }}>
                            {fmtInt(h.reviewed)} reviewed · {h.trigger}
                          </span>
                        </span>
                        <span className="sm" style={{ textAlign: 'right' }}>
                          {h.total} insights
                          <span className="hint" style={{ display: 'block' }}>
                            {h.accepted} accepted · {h.dismissed} dismissed
                          </span>
                        </span>
                      </div>
                    ))}
                  </div>
                </section>
              )}
            </aside>
          </div>
        )}
      </div>
      {draft && report && (
        <WriteMemoryModal
          title="Write the consolidated memory"
          initialContent={draft.text}
          onClose={() => setDraft(null)}
          onSaved={async () => {
            const i = draft.insight;
            setDraft(null);
            try {
              patchInsight(report, await api.decideInsight(report.id, i.id, 'accept'));
            } catch {
              /* the memory is written either way */
            }
          }}
        />
      )}
    </>
  );
};

const InsightCard: React.FC<{ insight: DreamInsight; onDecide: (i: DreamInsight, d: 'accept' | 'dismiss') => void }> = ({ insight: i, onDecide }) => {
  const k = KIND[i.kind];
  return (
    <section className={`card dream-card ${i.status}`} aria-label={`${k.label}: ${i.title}`}>
      <div style={{ display: 'flex', gap: 12, alignItems: 'flex-start' }}>
        <KindIcon kind={i.kind} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <span className={`lbl ${k.cls}`}>{k.label}</span>
            {i.confidence !== null && <span className="sm muted">{Math.round(i.confidence * 100)}% similar</span>}
            {i.auto && <span className="lbl g">done automatically</span>}
          </div>
          <h3 style={{ marginTop: 8 }}>{i.title}</h3>
          <p className="sm muted" style={{ margin: '4px 0 12px' }}>
            {i.detail}
          </p>
          <div className={`dream-mems ${i.memories.length === 2 ? 'pair' : ''}`}>
            {i.memories.map((m, idx) => (
              <React.Fragment key={m.id}>
                {idx === 1 && i.memories.length === 2 && (
                  <span className="dream-join" aria-hidden="true">
                    {i.kind === 'duplicates' ? '≈' : '↔'}
                  </span>
                )}
                <a className="dream-mem" href={buildHash(`/memory/${m.id}`)}>
                  <span style={{ display: 'flex', gap: 6, alignItems: 'center', marginBottom: 4 }}>
                    <ScopeLabel scope={m.scope} />
                    <span className="mono muted" style={{ fontSize: 11 }}>
                      {shortId(m.id)} · v{m.version} · w {fmtWeight(m.weight)}
                    </span>
                  </span>
                  <span className="dream-stub">{m.stub}</span>
                  {i.kind === 'duplicates' && <span className="hint">{idx === 0 ? 'keep (newer)' : 'fade (older)'}</span>}
                </a>
              </React.Fragment>
            ))}
          </div>
        </div>
      </div>
      <div className="dream-actions">
        {i.status === 'proposed' ? (
          <>
            <button type="button" className="btn bp sm" onClick={() => onDecide(i, 'accept')}>
              {k.verb}
            </button>
            <button type="button" className="btn bl sm" onClick={() => onDecide(i, 'dismiss')}>
              Dismiss
            </button>
          </>
        ) : (
          <span className={`sm ${i.status === 'accepted' ? 'ok' : 'muted'}`}>{i.status === 'accepted' ? `✓ ${i.result ?? 'Done'}` : 'Dismissed'}</span>
        )}
      </div>
    </section>
  );
};

const MoonIcon: React.FC = () => (
  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
    <path d="M20 14.5A8 8 0 119.5 4a6.5 6.5 0 0010.5 10.5z" />
  </svg>
);

export default Dreaming;
