import React, { useEffect, useState } from 'react';
import { api } from '@/api/client';
import { PageHeader, ROLE_LABEL, useApp } from '@/components/Shell';
import { ErrorState, SkLines, Switch, useToast } from '@/components/ui';
import { useApi } from '@/hooks/useApi';
import { buildHash } from '@/hooks/useHashRoute';
import type { BehaviorProfile, MyPrefs, MyPrefsResponse } from '@/types';
import { errorMessage, fmtInt } from '@/utils/format';
import { ConfirmModal } from './common';

/**
 * Preferences & privacy (#723): the person's defaults, what their agents may
 * remember, Dreaming, and their data. GET/PATCH /api/me/prefs; MCP applies the
 * default weight and never-store topics to the person's agents.
 */
const Preferences: React.FC = () => {
  const toast = useToast();
  const { role } = useApp();
  const { data, error, loading, reload, setData } = useApi<MyPrefsResponse>(() => api.myPrefs(), []);
  const [p, setP] = useState<MyPrefs | null>(null);
  const [busy, setBusy] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [confirm, setConfirm] = useState('');
  useEffect(() => {
    if (data) setP(structuredClone(data.prefs));
  }, [data]);

  const dirty = data && p && JSON.stringify(p) !== JSON.stringify(data.prefs);
  const save = async () => {
    if (!p || !data) return;
    setBusy(true);
    try {
      const behaviorChanged = JSON.stringify(profileBody(p.behavior)) !== JSON.stringify(profileBody(data.prefs.behavior));
      const res = await api.saveMyPrefs({
        write: p.write,
        dreaming: p.dreaming,
        ...(behaviorChanged ? { behavior: profileBody(p.behavior) } : {}),
      });
      setData(res);
      toast('Preferences saved.');
    } catch (e) {
      toast(errorMessage(e), 'err');
    } finally {
      setBusy(false);
    }
  };

  const a = data?.account;
  return (
    <>
      <PageHeader
        title="Preferences & privacy"
        description="Your defaults for new memories, what your agents may remember, Dreaming, and your data. Platform admins never read your personal memories."
        actions={
          <button type="button" className="btn bp" disabled={!dirty || busy} onClick={save}>
            {busy ? 'Saving…' : 'Save changes'}
          </button>
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
          p &&
          a && (
            <div className="row-wrap">
              <div className="col-main">
                <section className="card">
                  <h2 data-h="3">Writing memories</h2>
                  <p className="sm muted" style={{ margin: '6px 0 14px' }}>
                    The scope and weight a new memory starts with in the dashboard. Your agents get your default weight when they write without one.
                  </p>
                  <div className="fg" style={{ marginTop: 0 }}>
                    <div className="f">
                      <label htmlFor="ds">Default scope</label>
                      <select
                        id="ds"
                        className="in"
                        value={p.write.default_scope}
                        onChange={(e) => setP({ ...p, write: { ...p.write, default_scope: e.target.value as MyPrefs['write']['default_scope'] } })}
                      >
                        {role === 'user' && <option value="user">Personal — only you</option>}
                        <option value="project">Project</option>
                      </select>
                    </div>
                    <div className="f">
                      <label htmlFor="dw">Default weight · {p.write.default_weight.toFixed(2)}</label>
                      <input
                        id="dw"
                        type="range"
                        min={0}
                        max={100}
                        value={Math.round(p.write.default_weight * 100)}
                        onChange={(e) => setP({ ...p, write: { ...p.write, default_weight: Number(e.target.value) / 100 } })}
                      />
                      <span className="hint">At 0.80 or more, agents get the full text instead of a stub.</span>
                    </div>
                  </div>
                </section>

                <BehaviorSection p={p} setP={setP} profile={data!.profile} saved={data!.prefs.behavior} />

                <section className="card">
                  <h2 data-h="3">Dreaming</h2>
                  <div className="row">
                    <span>
                      <b className="sm">Extract memories from my conversations</b>
                      <span className="hint" style={{ display: 'block' }}>
                        When off, Dreaming skips your conversation threads and your agents' threads.
                      </span>
                    </span>
                    <Switch
                      on={p.dreaming.enabled}
                      onChange={(on) => setP({ ...p, dreaming: { ...p.dreaming, enabled: on } })}
                      label="Extract memories from my conversations"
                    />
                  </div>
                  <div className="f" style={{ marginTop: 12, maxWidth: 320 }}>
                    <label htmlFor="fd">Suggest fading a memory after</label>
                    <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                      <input
                        id="fd"
                        className="in auto"
                        type="number"
                        min={14}
                        max={365}
                        value={p.dreaming.fade_days}
                        onChange={(e) => setP({ ...p, dreaming: { ...p.dreaming, fade_days: Number(e.target.value) } })}
                        style={{ width: 100 }}
                      />
                      <span className="sm">days without use</span>
                    </div>
                  </div>
                </section>

                <section className="card">
                  <h2 data-h="3">Privacy</h2>
                  <div className="row">
                    <span>
                      <b className="sm">Admin access to my personal memories</b>
                      <span className="hint" style={{ display: 'block' }}>
                        Platform admins never see your personal memories. A reviewer reads one only when you submit it in{' '}
                        <a href={buildHash('/review')}>My requests</a>, and that read is logged in <a href={buildHash('/audit')}>My activity</a>.
                      </span>
                    </span>
                    <span className="lbl g">Always off</span>
                  </div>
                </section>

                {role === 'user' && (
                  <section className="card">
                    <h2 data-h="3">Your data</h2>
                    <p className="sm muted" style={{ margin: '6px 0 14px' }}>
                      {fmtInt(a.own_memories)} personal memories belong to you and your agents.
                    </p>
                    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                      <a className="btn bs" href={api.exportMineUrl()} download>
                        Download my memories (JSONL)
                      </a>
                      <a className="btn bg" href={buildHash('/memories', { scope: 'user' })}>
                        Browse my memories
                      </a>
                      <button type="button" className="btn bl" disabled={a.own_memories === 0} onClick={() => setDeleting(true)}>
                        Delete my personal memories
                      </button>
                    </div>
                  </section>
                )}
              </div>

              <aside className="col-side">
                <section className="card">
                  <h2 data-h="3">Account</h2>
                  <dl className="dl" style={{ marginTop: 14 }}>
                    <dt>Username</dt>
                    <dd className="mono">{a.username}</dd>
                    <dt>Role</dt>
                    <dd>{ROLE_LABEL[a.role]}</dd>
                    <dt>Signed in via</dt>
                    <dd>{a.source === 'oauth-proxy' ? 'OpenShift' : a.source === 'local' ? 'Local development' : a.source}</dd>
                  </dl>
                </section>
                <section className="card">
                  <h2 data-h="3">Where your preferences apply</h2>
                  <ul className="sm" style={{ margin: '12px 0 0', paddingLeft: 18 }}>
                    {data!.profile.applied_by.map((x) => (
                      <li key={x}>{x}</li>
                    ))}
                  </ul>
                </section>
                <section className="card">
                  <h2 data-h="3">What you can read</h2>
                  <dl className="dl" style={{ marginTop: 14 }}>
                    <dt>Agents</dt>
                    <dd>{a.agents.length ? a.agents.join(', ') : <a href={buildHash('/identities')}>Link an agent</a>}</dd>
                    <dt>Projects</dt>
                    <dd>{a.projects.length ? a.projects.join(', ') : '—'}</dd>
                    <dt>Roles</dt>
                    <dd>{a.roles.length ? a.roles.join(', ') : '—'}</dd>
                  </dl>
                </section>
              </aside>
            </div>
          )
        )}
      </div>
      {deleting && a && (
        <ConfirmModal
          title="Delete your personal memories?"
          confirmLabel="Delete"
          danger
          confirmDisabled={confirm !== a.username}
          onClose={() => {
            setDeleting(false);
            setConfirm('');
          }}
          onConfirm={async () => {
            const res = await api.deleteMyData(confirm);
            toast(`${fmtInt(res.deleted)} memory versions deleted.`);
            reload();
          }}
        >
          <p style={{ marginTop: 0 }}>
            Every version of the {fmtInt(a.own_memories)} personal memories that belong to you and your agents is deleted. Project memories stay. Download them first if you
            want a copy.
          </p>
          <label htmlFor="del-confirm" className="flabel">
            Type <b className="mono">{a.username}</b> to confirm
          </label>
          <input id="del-confirm" className="in" value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="off" />
        </ConfirmModal>
      )}
    </>
  );
};

const CATEGORY: Record<string, string> = {
  preferences: 'Preferences — how you like things done',
  project_decisions: 'Project decisions and their reasons',
  procedures: 'Procedures and how-tos',
  facts: 'Reusable facts',
};
const EXCLUDE: Record<string, string> = {
  credentials: 'Credentials and secrets',
  personal_data: 'Personal data',
  health: 'Health information',
  customer_data: 'Customer data',
};
const CHOICE: Record<string, Record<string, string>> = {
  confirmation: { always: 'Always ask me first', sensitive: 'Ask when it may be sensitive', never: 'Never ask' },
  cadence: { every_turn: 'After every turn', session_end: 'At the end of a session', explicit: 'Only when I ask' },
  update: { supersede: 'Replace the old memory', branch: 'Keep both as versions', ask: 'Ask me' },
};
const CHOICE_LABEL: Record<string, string> = {
  confirmation: 'Confirm before saving',
  cadence: 'When to look for memories',
  update: 'When something changes',
};
const TEMPLATE: Record<string, { title: string; body: string }> = {
  balanced: { title: 'Balanced', body: 'Preferences, decisions, procedures and facts; asks for anything sensitive.' },
  conservative: { title: 'Conservative', body: 'Only preferences and decisions, only when you ask, always confirmed.' },
  proactive: { title: 'Proactive', body: 'Looks after every turn and saves without asking. Credentials are still excluded.' },
};

export const profileBody = (b: BehaviorProfile) => {
  const { revision: _r, updated_at: _u, schema: _s, ...rest } = b;
  return rest;
};

/** Memory-behavior profile (R1-018/019). Stored and versioned; agent hosts do not apply it yet. */
const BehaviorSection: React.FC<{
  p: MyPrefs;
  setP: (p: MyPrefs) => void;
  profile: MyPrefsResponse['profile'];
  saved: BehaviorProfile;
}> = ({ p, setP, profile, saved }) => {
  const b = p.behavior;
  const [custom, setCustom] = useState('');
  const set = (patch: Partial<BehaviorProfile>, keepTemplate = false) => setP({ ...p, behavior: { ...b, ...patch, template: keepTemplate ? b.template : 'custom' } });
  const applyTemplate = (name: string) => {
    const t = profile.templates[name];
    setP({ ...p, behavior: { ...b, ...profileBody(t), custom_exclude: b.custom_exclude, template: t.template } });
  };
  const toggle = (key: 'include' | 'exclude', value: string) =>
    set({ [key]: b[key].includes(value) ? b[key].filter((x) => x !== value) : [...b[key], value] } as Partial<BehaviorProfile>);
  const previous = p.behavior_history?.[0];
  const download = () => {
    const blob = new Blob([JSON.stringify(b, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `memoryhub-profile-r${b.revision}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
  };
  const changed = JSON.stringify(profileBody(b)) !== JSON.stringify(profileBody(saved));
  return (
    <section className="card" aria-labelledby="mb-h">
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'baseline', flexWrap: 'wrap' }}>
        <h2 data-h="3" id="mb-h">What agents remember</h2>
        <span className="hint">
          Profile revision {saved.revision}
          {saved.updated_at ? ` · ${new Date(saved.updated_at).toLocaleDateString()}` : ' · default'}
          {changed ? ' · unsaved changes' : ''}
        </span>
      </div>
      <div className="alert warn-a" style={{ margin: '10px 0 14px' }}>
        <span className="sm">
          Saved with your account as <span className="code">{profile.schema}</span>. MemoryHub enforces <b>Never store</b> topics you add below for your agents' writes; what
          to look for, when and whether to confirm are for agent hosts, which do not read the profile yet. Platform curation rules always apply on top.
        </span>
      </div>
      <div className="tplpick" role="radiogroup" aria-label="Profile">
        {Object.entries(TEMPLATE).map(([id, t]) => (
          <label key={id} className={b.template === id ? 'on' : ''}>
            <input type="radio" name="tpl" checked={b.template === id} onChange={() => applyTemplate(id)} />
            <span>
              <b>{t.title}</b>
              <span className="hint" style={{ display: 'block' }}>{t.body}</span>
            </span>
          </label>
        ))}
      </div>
      {b.template === 'custom' && <p className="hint">Custom — changed from a template.</p>}

      <h4 style={{ margin: '18px 0 8px', fontSize: 14, fontWeight: 600 }}>Look for</h4>
      <div className="checkgrid">
        {profile.categories.map((c) => (
          <label key={c}>
            <input type="checkbox" checked={b.include.includes(c)} onChange={() => toggle('include', c)} />
            {CATEGORY[c] ?? c}
          </label>
        ))}
      </div>
      <p className="hint">Looking for something does not force saving it. Agent hosts apply these choices; MemoryHub stores them.</p>

      <h4 style={{ margin: '18px 0 8px', fontSize: 14, fontWeight: 600 }}>Never store</h4>
      <p className="hint" style={{ marginTop: 0 }}>
        Categories guide agent hosts. Topics you add are enforced: MemoryHub refuses an agent's write that mentions one, and the refusal shows in My activity.
      </p>
      <div className="checkgrid">
        {profile.excludes.map((c) => (
          <label key={c}>
            <input type="checkbox" checked={b.exclude.includes(c)} onChange={() => toggle('exclude', c)} />
            {EXCLUDE[c] ?? c}
          </label>
        ))}
      </div>
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 10 }}>
        {b.custom_exclude.map((x) => (
          <span key={x} className="chip">
            {x}
            <button type="button" aria-label={`Remove ${x}`} onClick={() => set({ custom_exclude: b.custom_exclude.filter((y) => y !== x) }, true)}>
              ×
            </button>
          </span>
        ))}
      </div>
      <form
        style={{ display: 'flex', gap: 8, marginTop: 8, maxWidth: 420 }}
        onSubmit={(e) => {
          e.preventDefault();
          const v = custom.trim();
          if (v && !b.custom_exclude.includes(v) && b.custom_exclude.length < 20) set({ custom_exclude: [...b.custom_exclude, v] }, true);
          setCustom('');
        }}
      >
        <input className="in" placeholder="Add a topic, e.g. salary" maxLength={200} value={custom} onChange={(e) => setCustom(e.target.value)} aria-label="Add a topic to never store" />
        <button type="submit" className="btn bs" disabled={!custom.trim()}>
          Add
        </button>
      </form>

      <div className="fg" style={{ marginTop: 18 }}>
        {(Object.keys(CHOICE) as ('confirmation' | 'cadence' | 'update')[]).map((k) => (
          <div className="f" key={k}>
            <label htmlFor={`mb-${k}`}>{CHOICE_LABEL[k]}</label>
            <select id={`mb-${k}`} className="in" value={b[k]} onChange={(e) => set({ [k]: e.target.value } as Partial<BehaviorProfile>)}>
              {profile.choices[k].map((v) => (
                <option key={v} value={v}>
                  {CHOICE[k][v] ?? v}
                </option>
              ))}
            </select>
          </div>
        ))}
      </div>

      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 16 }}>
        <button type="button" className="btn bg sm" onClick={download}>
          Download profile (JSON)
        </button>
        {previous && (
          <button type="button" className="btn bg sm" onClick={() => setP({ ...p, behavior: { ...b, ...profileBody(previous) } })}>
            Revert to revision {previous.revision}
          </button>
        )}
      </div>
    </section>
  );
};

export default Preferences;
