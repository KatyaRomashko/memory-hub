import React, { useState } from 'react';
import { api } from '@/api/client';
import { PageHeader, Tabs, useApp } from '@/components/Shell';
import { Empty, ErrorState, Icon, SkLines, useToast } from '@/components/ui';
import { useApi } from '@/hooks/useApi';
import { buildHash, navigate, type HashRoute } from '@/hooks/useHashRoute';
import type { CampaignRow, ProjectDetail, ProjectRole, ProjectRoleInfo, ProjectRow, TeamRow } from '@/types';
import { errorMessage, fmtInt, fmtRelative } from '@/utils/format';
import { ConfirmModal } from './common';
import { FocusHistory } from './Projects';

/**
 * Projects & teams for a user (#720): the projects, teams and campaigns they
 * belong to and their role in each. Roles are the ones the backend enforces.
 * A user joins open projects themselves and leaves projects and teams; a
 * project admin enrolls their project in campaigns or takes it out.
 */

const ROLE_LABEL: Record<ProjectRole, string> = { owner: 'Owner', manager: 'Project Manager', contributor: 'Contributor' };
const roleLabel = (r: ProjectRole | null | undefined) => (r ? ROLE_LABEL[r] : 'Not a member');

type Kind = 'projects' | 'teams' | 'campaigns';

const MyProjects: React.FC<{ route: HashRoute }> = ({ route }) => {
  const { me } = useApp();
  const raw = route.params.get('kind');
  const kind: Kind = raw === 'teams' || raw === 'campaigns' ? raw : 'projects';
  const projects = useApi(() => api.projects(false), []);
  const teams = useApi(() => api.teams(), []);
  const campaigns = useApi(() => api.campaigns(), []);
  const roles = useApi(() => api.projectRoles(), []);

  const rows = projects.data?.items ?? [];
  const myTeams = teams.data?.items ?? [];
  const myCampaigns = campaigns.data?.items ?? [];

  const setParam = (changes: Record<string, string | undefined>) => {
    const next = new URLSearchParams(route.params);
    Object.entries(changes).forEach(([k, v]) => (v ? next.set(k, v) : next.delete(k)));
    navigate('/projects', next, { replace: true });
  };
  const selProject = rows.find((p) => p.name === route.params.get('project')) ?? rows[0] ?? null;
  const selTeam = myTeams.find((t) => t.id === route.params.get('team')) ?? myTeams[0] ?? null;
  const selCampaign = myCampaigns.find((c) => c.id === route.params.get('campaign')) ?? myCampaigns[0] ?? null;
  const memberOf = rows.filter((p) => p.my_role).length;

  return (
    <>
      <PageHeader
        title="Projects & teams"
        description="Projects, teams and campaigns you belong to, and open projects you can join. What you can change depends on your role in each one, not on your account role."
        tabs={
          <Tabs
            label="Kind"
            value={kind}
            onChange={(k) => navigate('/projects', k === 'projects' ? undefined : { kind: k }, { replace: true })}
            tabs={[
              { value: 'projects', label: 'Projects', count: projects.data ? memberOf : null },
              { value: 'teams', label: 'Teams', count: teams.data ? myTeams.length : null },
              { value: 'campaigns', label: 'Campaigns', count: campaigns.data ? myCampaigns.filter((c) => c.my_projects.length > 0).length : null },
            ]}
          />
        }
      />
      <div className="body">
        {kind === 'projects' ? (
          <Split
            loading={projects.loading && !projects.data}
            error={projects.error && !projects.data ? projects.error : null}
            reload={projects.reload}
            empty={rows.length === 0}
            emptyTitle="You are not in any project yet"
            emptyText="Projects are created by platform admins, who add their members. Open projects show up here for you to join."
            label="Your projects"
            list={rows.map((p) => ({
              key: p.name,
              title: p.name,
              sub: `${p.my_role ? roleLabel(p.my_project_role) : p.invite_only ? 'Invite only' : 'Open · you can join'} · ${fmtInt(p.members)} members · ${fmtInt(p.memories)} memories`,
              on: p.name === selProject?.name,
              go: () => setParam({ project: p.name }),
            }))}
            detail={
              selProject ? (
                <ProjectView
                  key={selProject.name}
                  row={selProject}
                  roles={roles.data?.items ?? []}
                  onChanged={(left) => {
                    if (left) setParam({ project: undefined });
                    projects.reload();
                    campaigns.reload();
                  }}
                />
              ) : null
            }
          />
        ) : kind === 'teams' ? (
          <Split
            loading={teams.loading && !teams.data}
            error={teams.error && !teams.data ? teams.error : null}
            reload={teams.reload}
            empty={myTeams.length === 0}
            emptyTitle="You are not in a team"
            emptyText="Teams are groups of people set up by a platform admin; a team lead adds members."
            label="Your teams"
            list={myTeams.map((t) => ({
              key: t.id,
              title: t.name,
              sub: `${t.members.find((m) => m.user_id === me?.username)?.role === 'lead' ? 'Lead' : 'Member'} · ${t.members.length} people · ${t.projects.length} projects`,
              on: t.id === selTeam?.id,
              go: () => setParam({ team: t.id }),
            }))}
            detail={
              selTeam ? (
                <TeamView
                  key={selTeam.id}
                  team={selTeam}
                  self={me?.username}
                  onLeft={() => {
                    setParam({ team: undefined });
                    teams.reload();
                  }}
                />
              ) : null
            }
          />
        ) : (
          <Split
            loading={campaigns.loading && !campaigns.data}
            error={campaigns.error && !campaigns.data ? campaigns.error : null}
            reload={campaigns.reload}
            empty={myCampaigns.length === 0}
            emptyTitle="None of your projects is in a campaign"
            emptyText="Campaigns group projects under one policy and memory lifetime for a bounded initiative. A project admin enrolls the project."
            label="Campaigns"
            list={myCampaigns.map((c) => ({
              key: c.id,
              title: c.name,
              sub: `${c.status} · ${c.projects.length} projects · ${fmtInt(c.memories)} memories${c.my_projects.length ? '' : ' · you may enroll'}`,
              on: c.id === selCampaign?.id,
              go: () => setParam({ campaign: c.id }),
            }))}
            detail={
              selCampaign ? (
                <CampaignView
                  key={selCampaign.id}
                  campaign={selCampaign}
                  onChanged={() => {
                    campaigns.reload();
                    projects.reload();
                  }}
                />
              ) : null
            }
          />
        )}
      </div>
    </>
  );
};

/** A narrow list on the left, the selected record on the right. */
const Split: React.FC<{
  loading: boolean;
  error: Error | null;
  reload: () => void;
  empty: boolean;
  emptyTitle: string;
  emptyText: string;
  label: string;
  list: { key: string; title: string; sub: string; on: boolean; go: () => void }[];
  detail: React.ReactNode;
}> = ({ loading, error, reload, empty, emptyTitle, emptyText, label, list, detail }) => {
  if (error)
    return (
      <div className="card">
        <ErrorState error={error} onRetry={reload} />
      </div>
    );
  if (loading)
    return (
      <div className="card">
        <SkLines lines={6} />
      </div>
    );
  if (empty)
    return (
      <div className="card">
        <Empty title={emptyTitle}>{emptyText}</Empty>
      </div>
    );
  return (
    <div className="row-wrap">
      <nav className="card plist" aria-label={label}>
        {list.map((i) => (
          <button key={i.key} type="button" className={i.on ? 'on' : ''} aria-current={i.on ? 'true' : undefined} onClick={i.go}>
            <span className="b6">{i.title}</span>
            <span className="hint">{i.sub}</span>
          </button>
        ))}
      </nav>
      <div className="col-main" style={{ flex: '999 1 620px' }}>
        {detail}
      </div>
    </div>
  );
};

const Stat: React.FC<{ label: string; value: string; hint?: string }> = ({ label, value, hint }) => (
  <div>
    <div className="sm muted">{label}</div>
    <div className="kpi">{value}</div>
    {hint && <div className="hint">{hint}</div>}
  </div>
);

const count = (n: number | null | undefined) => (n === null || n === undefined ? '—' : fmtInt(n));

const ProjectView: React.FC<{ row: ProjectRow; roles: ProjectRoleInfo[]; onChanged: (left: boolean) => void }> = ({ row, roles, onChanged }) => {
  const toast = useToast();
  const { me } = useApp();
  const { data, error, loading, reload } = useApi<ProjectDetail>(() => api.project(row.name), [row.name]);
  const [leaving, setLeaving] = useState(false);
  const [busy, setBusy] = useState(false);
  if (error && !data)
    return (
      <div className="card">
        <ErrorState error={error} onRetry={reload} compact />
      </div>
    );
  if (loading && !data)
    return (
      <div className="card">
        <SkLines lines={10} />
      </div>
    );
  if (!data) return null;

  const myRole = data.my_project_role ?? null;
  const isMember = Boolean(data.my_role);
  const managers = data.managers ?? [];
  const myRoleInfo = roles.find((r) => r.role === myRole);

  const join = async () => {
    setBusy(true);
    try {
      await api.joinProject(data.name);
      toast(`You joined ${data.name} as a Contributor.`);
      reload();
      onChanged(false);
    } catch (e) {
      toast(errorMessage(e), 'err');
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <section className="card">
        <div className="ch" style={{ alignItems: 'flex-start' }}>
          <div>
            <h2 className="card-title">{data.name}</h2>
            <div className="sm muted">
              Owner {data.owner}
              {managers.length > 0 && ` · Project Manager ${managers.join(', ')}`}
            </div>
          </div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <span className="lbl blue lg">{isMember ? `Your role: ${roleLabel(myRole)}` : data.invite_only ? 'Invite only' : 'Open project'}</span>
            {data.can_join && (
              <button type="button" className="btn bp sm" disabled={busy} onClick={join}>
                Join project
              </button>
            )}
            <a className="btn bs sm" href={buildHash('/memories', { project: data.name })}>
              Browse memories
            </a>
          </div>
        </div>
        {data.description && (
          <p className="sm muted" style={{ margin: '0 0 12px' }}>
            {data.description}
          </p>
        )}
        <div className="stat4">
          <Stat label="Memories" value={fmtInt(data.memories)} hint={data.last_write ? `last write ${fmtRelative(data.last_write).toLowerCase()}` : undefined} />
          <Stat label="Written by you" value={count(data.written_by_me)} hint={isMember && data.written_by_my_agents ? `${fmtInt(data.written_by_my_agents)} by your agents` : undefined} />
          <Stat label="Open contradictions" value={count(data.open_contradictions)} />
          <Stat label="Quota" value={data.quota ? `${Math.round((data.quota.used / data.quota.limit) * 100)}%` : '—'} hint={data.quota ? undefined : 'no quota set'} />
        </div>
      </section>

      <div className="grid-eq">
        <section className="card">
          <div className="ch">
            <h2 data-h="3">Members</h2>
            <span className="sm muted">Read only</span>
          </div>
          {data.members_list.length === 0 ? (
            <p className="sm muted">{isMember ? 'No members yet.' : 'Only members see who else is in the project.'}</p>
          ) : (
            data.members_list.map((m) => (
              <div key={m.user_id} className="evrow" style={{ justifyContent: 'space-between' }}>
                <span className="sm">
                  {m.user_id}
                  {m.user_id === me?.username && ' (you)'}
                </span>
                <span className={`lbl ${m.project_role === 'contributor' ? 'g' : 'blue'}`}>{roleLabel(m.project_role ?? null)}</span>
              </div>
            ))
          )}
        </section>
        <section className="card">
          <div className="ch">
            <h2 data-h="3">{myRoleInfo ? `What a ${myRoleInfo.label} can do` : 'Roles in a project'}</h2>
          </div>
          {myRoleInfo ? (
            <ul className="sm" style={{ margin: 0, paddingLeft: 18 }}>
              {myRoleInfo.permissions.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          ) : (
            <dl className="kv">
              {roles.map((r) => (
                <React.Fragment key={r.role}>
                  <dt>{r.label}</dt>
                  <dd>{r.permissions.join(', ')}</dd>
                </React.Fragment>
              ))}
            </dl>
          )}
          <p className="hint" style={{ margin: '10px 0 0' }}>
            Agents read and write this project when they pass project_id={data.name}; which memories an agent loads at session start is set in that agent’s own configuration.
          </p>
        </section>
      </div>

      {data.campaigns.length > 0 && (
        <section className="card">
          <div className="ch">
            <h2 data-h="3">Campaigns</h2>
            <a className="sm" href={buildHash('/projects', { kind: 'campaigns' })}>
              All campaigns
            </a>
          </div>
          {data.campaigns.map((c) => (
            <div key={c.id} className="evrow" style={{ justifyContent: 'space-between' }}>
              <a className="sm" href={buildHash('/projects', { kind: 'campaigns', campaign: c.id })}>
                {c.name}
              </a>
              <span className={`lbl ${c.status === 'active' ? 'grn' : 'g'}`}>{c.status}</span>
            </div>
          ))}
        </section>
      )}

      <FocusHistory data={data} />

      {isMember && (
        <section className="card">
          <div className="ch">
            <h2 data-h="3">Membership</h2>
            <span className="lbl g lg">{roleLabel(myRole)}</span>
          </div>
          <p className="sm" style={{ margin: 0, display: 'flex', gap: 6, alignItems: 'center' }}>
            <Icon.Lock /> Members are added and removed by a platform admin who administers this project.
          </p>
          <button type="button" className="btn bl" style={{ marginTop: 6, paddingLeft: 0 }} onClick={() => setLeaving(true)}>
            Leave project…
          </button>
        </section>
      )}

      {leaving && me && (
        <ConfirmModal
          title={`Leave ${data.name}?`}
          confirmLabel="Leave project"
          danger
          onClose={() => setLeaving(false)}
          onConfirm={async () => {
            await api.removeMember(data.name, me.username);
            toast(`You left ${data.name}. You no longer read or write its memories from the dashboard.`);
            onChanged(true);
          }}
        >
          You lose read and write access to <b>{data.name}</b> project memories. Agents that are members of the project in their own right keep theirs. Memories you wrote stay in the project.
          {data.invite_only ? ' A platform admin can add you again.' : ' You can join again at any time.'}
        </ConfirmModal>
      )}
    </>
  );
};

const TeamView: React.FC<{ team: TeamRow; self?: string; onLeft: () => void }> = ({ team, self, onLeft }) => {
  const toast = useToast();
  const [leaving, setLeaving] = useState(false);
  const mine = team.members.find((m) => m.user_id === self);
  return (
    <>
      <section className="card">
        <div className="ch" style={{ alignItems: 'flex-start' }}>
          <div>
            <h2 className="card-title">{team.name}</h2>
            <div className="sm muted">
              {team.description || 'No description'} · created by {team.created_by}
            </div>
          </div>
          {mine && <span className="lbl blue lg">You are {mine.role === 'lead' ? 'the lead' : 'a member'}</span>}
        </div>
      </section>
      <div className="grid-eq">
        <section className="card">
          <div className="ch">
            <h2 data-h="3">People</h2>
            <span className="sm muted">Read only</span>
          </div>
          {team.members.map((m) => (
            <div key={m.user_id} className="evrow" style={{ justifyContent: 'space-between' }}>
              <span className="sm">
                {m.user_id}
                {m.user_id === self && ' (you)'}
              </span>
              <span className="lbl g">{m.role === 'lead' ? 'Lead' : 'Member'}</span>
            </div>
          ))}
        </section>
        <section className="card">
          <div className="ch">
            <h2 data-h="3">Projects this team works on</h2>
          </div>
          {team.projects.length === 0 && <p className="sm muted">None yet.</p>}
          {team.projects.map((p) => (
            <div key={p} className="evrow">
              <a className="sm" href={buildHash('/projects', { project: p })}>
                {p}
              </a>
            </div>
          ))}
          <p className="hint" style={{ margin: '10px 0 0' }}>
            A linked project is for orientation: project access still comes from project membership.
          </p>
        </section>
      </div>
      <section className="card">
        <p className="sm muted" style={{ margin: 0, display: 'flex', gap: 6, alignItems: 'center' }}>
          <Icon.Lock /> Team members and linked projects are managed by the team lead or a platform admin.
        </p>
        {mine && (
          <button type="button" className="btn bl" style={{ marginTop: 6, paddingLeft: 0 }} onClick={() => setLeaving(true)}>
            Leave team…
          </button>
        )}
      </section>
      {leaving && self && (
        <ConfirmModal
          title={`Leave ${team.name}?`}
          confirmLabel="Leave team"
          danger
          onClose={() => setLeaving(false)}
          onConfirm={async () => {
            await api.leaveTeam(team.id, self);
            toast(`You left ${team.name}.`);
            onLeft();
          }}
        >
          You leave the team. Your project memberships do not change. A team keeps at least one lead, so its last lead cannot leave.
        </ConfirmModal>
      )}
    </>
  );
};

const CampaignView: React.FC<{ campaign: CampaignRow; onChanged: () => void }> = ({ campaign, onChanged }) => {
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);
  const act = async (project: string, enroll: boolean) => {
    setBusy(project);
    try {
      if (enroll) await api.enrollCampaign(campaign.id, project);
      else await api.leaveCampaign(campaign.id, project);
      toast(enroll ? `${project} joined ${campaign.name}.` : `${project} left ${campaign.name}.`);
      onChanged();
    } catch (e) {
      toast(errorMessage(e), 'err');
    } finally {
      setBusy(null);
    }
  };
  return (
    <>
      <section className="card">
        <div className="ch" style={{ alignItems: 'flex-start' }}>
          <div>
            <h2 className="card-title">{campaign.name}</h2>
            <div className="sm muted">{campaign.description || 'No description'}</div>
          </div>
          <span className={`lbl ${campaign.status === 'active' ? 'grn' : 'g'}`}>{campaign.status}</span>
        </div>
        <div className="stat4">
          <Stat label="Projects" value={fmtInt(campaign.projects.length)} />
          <Stat label="Memories" value={fmtInt(campaign.memories)} />
          <Stat label="Default lifetime" value={campaign.default_ttl_days ? `${campaign.default_ttl_days} d` : '—'} />
        </div>
      </section>
      <section className="card">
        <div className="ch">
          <h2 data-h="3">Enrolled projects</h2>
        </div>
        {campaign.projects.length === 0 && <p className="sm muted">No project is enrolled yet.</p>}
        {campaign.projects.map((p) => (
          <div key={p} className="evrow" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
            <a className="sm" href={buildHash('/projects', { project: p })}>
              {p}
            </a>
            <span style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              {campaign.my_projects.includes(p) && <span className="lbl blue">You are a member</span>}
              {campaign.can_leave.includes(p) && (
                <button type="button" className="btn bl sm" disabled={busy === p} onClick={() => act(p, false)}>
                  Take out
                </button>
              )}
            </span>
          </div>
        ))}
      </section>
      {campaign.can_enroll.length > 0 && (
        <section className="card">
          <div className="ch">
            <h2 data-h="3">Enroll a project you manage</h2>
          </div>
          {campaign.can_enroll.map((p) => (
            <div key={p} className="evrow" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
              <span className="sm">{p}</span>
              <button type="button" className="btn bs sm" disabled={busy === p} onClick={() => act(p, true)}>
                Enroll
              </button>
            </div>
          ))}
          <p className="hint" style={{ margin: '10px 0 0' }}>
            Memories written to the campaign get its default lifetime. Taking a project out keeps the campaign’s memories with the campaign.
          </p>
        </section>
      )}
    </>
  );
};

export default MyProjects;
