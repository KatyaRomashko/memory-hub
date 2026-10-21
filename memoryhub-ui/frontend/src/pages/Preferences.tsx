import React, { useEffect, useState } from 'react';
import { api } from '@/api/client';
import { PageHeader, ROLE_LABEL } from '@/components/Shell';
import { ErrorState, SkLines, Switch, useToast } from '@/components/ui';
import { useApi } from '@/hooks/useApi';
import { buildHash } from '@/hooks/useHashRoute';
import type { MyPrefs, MyPrefsResponse } from '@/types';
import { errorMessage, fmtInt } from '@/utils/format';

/** Preferences & privacy: the user's own defaults, sharing choices and data. */
const Preferences: React.FC = () => {
  const toast = useToast();
  const { data, error, loading, reload, setData } = useApi<MyPrefsResponse>(() => api.myPrefs(), []);
  const [p, setP] = useState<MyPrefs | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (data) setP(structuredClone(data.prefs));
  }, [data]);

  const dirty = data && p && JSON.stringify(p) !== JSON.stringify(data.prefs);
  const save = async () => {
    if (!p || !data) return;
    setBusy(true);
    try {
      const res = await api.saveMyPrefs(p);
      setData({ ...data, prefs: res.prefs });
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
        description="Your defaults for new memories, who may build on your memories, and your data. Platform admins never see what your memories say."
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
                  <h3>Writing memories</h3>
                  <p className="sm muted" style={{ margin: '6px 0 14px' }}>
                    Used when you write a memory in the dashboard. Agents choose their own scope and weight.
                  </p>
                  <div className="fg" style={{ marginTop: 0 }}>
                    <div className="f">
                      <label htmlFor="ds">Default scope</label>
                      <select id="ds" className="in" value={p.write.default_scope} onChange={(e) => setP({ ...p, write: { ...p.write, default_scope: e.target.value } })}>
                        <option value="user">User — only you</option>
                        <option value="project">Project</option>
                        <option value="campaign">Campaign</option>
                        <option value="role">Role</option>
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

                <section className="card">
                  <h3>Privacy</h3>
                  <div className="row">
                    <span>
                      <b className="sm">Let others propose promoting my memories</b>
                      <span className="hint" style={{ display: 'block' }}>
                        When off, only you can propose moving one of your user memories to a project or organization scope.
                      </span>
                    </span>
                    <Switch
                      on={p.privacy.allow_promotion_by_others}
                      onChange={(on) => setP({ ...p, privacy: { ...p.privacy, allow_promotion_by_others: on } })}
                      label="Let others propose promoting my memories"
                    />
                  </div>
                  <div className="row">
                    <span>
                      <b className="sm">Show me in team pickers</b>
                      <span className="hint" style={{ display: 'block' }}>Lets teammates find you when they add people to a team or project.</span>
                    </span>
                    <Switch
                      on={p.privacy.show_in_team_directory}
                      onChange={(on) => setP({ ...p, privacy: { ...p.privacy, show_in_team_directory: on } })}
                      label="Show me in team pickers"
                    />
                  </div>
                  <div className="row">
                    <span>
                      <b className="sm">Admin access to my content</b>
                      <span className="hint" style={{ display: 'block' }}>
                        Platform admins see counts, scopes and activity only. Break-glass access to a named memory needs approval and is always shown in{' '}
                        <a href={buildHash('/reads')}>Who read my memories</a>.
                      </span>
                    </span>
                    <span className="lbl g">Always off</span>
                  </div>
                </section>

                <section className="card">
                  <h3>Dreaming</h3>
                  <div className="f" style={{ marginTop: 12, maxWidth: 320 }}>
                    <label htmlFor="fd">Suggest fading a memory after</label>
                    <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                      <input
                        id="fd"
                        className="in auto"
                        type="number"
                        min={14}
                        max={365}
                        value={p.dreaming_fade_days}
                        onChange={(e) => setP({ ...p, dreaming_fade_days: Number(e.target.value) })}
                        style={{ width: 100 }}
                      />
                      <span className="sm">days without use</span>
                    </div>
                  </div>
                  <p className="sm muted" style={{ margin: '12px 0 0' }}>
                    Schedule and automatic actions live on the <a href={buildHash('/dreaming')}>Dreaming</a> page.
                  </p>
                </section>

                <section className="card">
                  <h3>Your data</h3>
                  <p className="sm muted" style={{ margin: '6px 0 14px' }}>
                    {fmtInt(a.own_memories)} user memories belong to you and your agents.
                  </p>
                  <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                    <a className="btn bs" href={api.exportMineUrl()} download>
                      Download my memories (JSONL)
                    </a>
                    <a className="btn bg" href={buildHash('/memories', { scope: 'user' })}>
                      Browse my memories
                    </a>
                  </div>
                </section>
              </div>

              <aside className="col-side">
                <section className="card">
                  <h3>Account</h3>
                  <dl className="dl" style={{ marginTop: 14 }}>
                    <dt>Username</dt>
                    <dd className="mono">{a.username}</dd>
                    {a.email && (
                      <>
                        <dt>Email</dt>
                        <dd>{a.email}</dd>
                      </>
                    )}
                    <dt>Role</dt>
                    <dd>{ROLE_LABEL[a.role]}</dd>
                    <dt>Signed in via</dt>
                    <dd>{a.source === 'oauth-proxy' ? 'OpenShift' : a.source === 'account' ? 'MemoryHub account' : a.source === 'api-key' ? 'API key (dev)' : 'Open mode'}</dd>
                  </dl>
                </section>
                <section className="card">
                  <h3>What you can read</h3>
                  <dl className="dl" style={{ marginTop: 14 }}>
                    <dt>Agents</dt>
                    <dd>{a.agents.length ? a.agents.join(', ') : <a href={buildHash('/identities')}>Link an agent</a>}</dd>
                    <dt>Projects</dt>
                    <dd>{a.projects.length ? a.projects.join(', ') : '—'}</dd>
                    <dt>Roles</dt>
                    <dd>{a.roles.length ? a.roles.join(', ') : '—'}</dd>
                  </dl>
                  <p className="hint" style={{ marginBottom: 0 }}>
                    Plus every organizational and enterprise memory. Teams on <a href={buildHash('/projects', { kind: 'teams' })}>Projects &amp; teams</a> add projects.
                  </p>
                </section>
              </aside>
            </div>
          )
        )}
      </div>
    </>
  );
};

export default Preferences;
