import React, { createContext, useCallback, useContext, useEffect, useState } from 'react';
import { api } from '@/api/client';
import type { Me, Role } from '@/types';
import { buildHash } from '@/hooks/useHashRoute';
import { Dropdown, Icon } from './ui';

const REPO_URL = 'https://github.com/redhat-ai-americas/memory-hub';

/* ------------------------------------------------------------------ */
/* App-wide context: who is viewing, tenant, memory presence           */
/* ------------------------------------------------------------------ */

export interface AppState {
  me: Me | null;
  /** Viewer role; 'operator' until /api/me answers and in open development mode. */
  role: Role;
  tenant: string;
  /** null until the first count arrives. */
  memoryCount: number | null;
  /** True while the tenant has no memories yet (standby mode). */
  standby: boolean;
  reviewCount: number;
  refreshCounts: () => void;
}

const AppContext = createContext<AppState>({
  me: null,
  role: 'operator',
  tenant: '',
  memoryCount: null,
  standby: false,
  reviewCount: 0,
  refreshCounts: () => undefined,
});
export const useApp = () => useContext(AppContext);

const POLL_EMPTY_MS = 5000;
const POLL_IDLE_MS = 60000;

export const AppProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [me, setMe] = useState<Me | null>(null);
  const [memoryCount, setMemoryCount] = useState<number | null>(null);
  const [reviewCount, setReviewCount] = useState(0);
  const [token, setToken] = useState(0);

  useEffect(() => {
    api.me().then(setMe).catch(() => undefined);
  }, []);

  useEffect(() => {
    let cancelled = false;
    api
      .navCounts()
      .then((c) => {
        if (cancelled) return;
        setMemoryCount(c.memories);
        setReviewCount(c.review_queue);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [token]);

  // Poll every 5 s while waiting for the first write_memory, then slowly.
  useEffect(() => {
    const id = window.setInterval(() => setToken((t) => t + 1), memoryCount === 0 ? POLL_EMPTY_MS : POLL_IDLE_MS);
    return () => window.clearInterval(id);
  }, [memoryCount]);

  const refreshCounts = useCallback(() => setToken((t) => t + 1), []);
  return (
    <AppContext.Provider
      value={{
        me,
        role: me?.role ?? 'operator',
        tenant: me?.tenant_id ?? '',
        memoryCount,
        standby: memoryCount === 0,
        reviewCount,
        refreshCounts,
      }}
    >
      {children}
    </AppContext.Provider>
  );
};

/* ------------------------------------------------------------------ */
/* Navigation                                                          */
/* ------------------------------------------------------------------ */

export interface NavItem {
  path: string;
  label: string;
  badge?: 'review';
  /** Roles that see this item; all roles when omitted. */
  roles?: Role[];
  /** Label override per role. */
  labelFor?: Partial<Record<Role, string>>;
}

const ADMIN: Role[] = ['admin', 'operator'];
const USER: Role[] = ['user', 'operator'];

/**
 * Users work with memories; platform admins run the platform and see memory
 * metadata only. The open-mode operator (no sign-in configured) sees both.
 */
export const NAV: { group?: string; groupFor?: Partial<Record<Role, string>>; items: NavItem[] }[] = [
  { items: [{ path: '/overview', label: 'Overview', labelFor: { user: 'My overview', admin: 'Platform overview' } }] },
  {
    group: 'Memory',
    groupFor: { admin: 'Memory · metadata only', user: 'My memory' },
    items: [
      { path: '/memories', label: 'Memory browser', labelFor: { admin: 'Memory analytics' } },
      { path: '/entities', label: 'Entities', roles: USER },
      { path: '/search', label: 'Search playground', roles: USER },
      { path: '/projects', label: 'Projects & campaigns' },
    ],
  },
  {
    group: 'Governance',
    items: [
      { path: '/review', label: 'Review queue', badge: 'review', labelFor: { user: 'Review requests' } },
      { path: '/curation', label: 'Curation rules', roles: ADMIN },
      { path: '/audit', label: 'Audit & forensics', roles: ADMIN },
    ],
  },
  {
    group: 'Access',
    items: [
      { path: '/accounts', label: 'Accounts & sign-up', roles: ADMIN },
      { path: '/identities', label: 'Users & agents', roles: ADMIN },
      { path: '/clients', label: 'OAuth clients & keys', roles: ADMIN },
    ],
  },
  { group: 'Platform', items: [{ path: '/settings', label: 'Settings', roles: ADMIN }] },
];

export const canSee = (path: string, role: Role) =>
  NAV.some((s) => s.items.some((i) => i.path === path && (!i.roles || i.roles.includes(role))));

export const navFor = (role: Role) =>
  NAV.map((s) => ({
    group: s.groupFor?.[role] ?? s.group,
    items: s.items
      .filter((i) => !i.roles || i.roles.includes(role))
      .map((i) => ({ ...i, label: i.labelFor?.[role] ?? i.label })),
  })).filter((s) => s.items.length > 0);

export const ROLE_LABEL: Record<Role, string> = { admin: 'Platform admin', user: 'User', operator: 'Open mode' };

const NAV_PREF = 'memoryhub.navOpen';
const readNavPref = () => {
  try {
    return window.localStorage.getItem(NAV_PREF) !== 'false';
  } catch {
    return true;
  }
};

export const Shell: React.FC<{ activePath: string; children: React.ReactNode }> = ({ activePath, children }) => {
  const { me, role, tenant, reviewCount } = useApp();
  const narrow = () => window.innerWidth < 960;
  const [open, setOpen] = useState<boolean>(() => (narrow() ? false : readNavPref()));

  useEffect(() => {
    if (narrow()) setOpen(false);
  }, [activePath]);

  const toggle = () => {
    setOpen((o) => {
      const next = !o;
      if (!narrow()) {
        try {
          window.localStorage.setItem(NAV_PREF, String(next));
        } catch {
          /* storage unavailable */
        }
      }
      return next;
    });
  };

  const onLogout = async () => {
    try {
      await api.logout();
    } finally {
      window.location.hash = '#/login';
    }
  };

  return (
    <div>
      <header className="mast">
        <button type="button" className="icb" aria-label={open ? 'Hide navigation' : 'Show navigation'} aria-expanded={open} onClick={toggle}>
          <Icon.Menu />
        </button>
        <a className="brand" href={buildHash('/overview')}>
          <span className="mark">
            <Icon.Mark />
          </span>
          <span className="wm">
            MemoryHub<small>for OpenShift AI</small>
          </span>
        </a>
        {role !== 'operator' && (
          <span className={`role-pill ${role}`} title={role === 'admin' ? 'You see memory metadata only' : 'You see memories you are allowed to read'}>
            {ROLE_LABEL[role]}
          </span>
        )}
        {tenant && (
          <span className="ctx" title="Each dashboard deployment serves one tenant (MEMORYHUB_UI_TENANT_ID)">
            Tenant: {tenant}
          </span>
        )}
        <div className="mast-r">
          {role !== 'admin' && (
            <a className="icb" aria-label="Search memories" href={buildHash('/search')}>
              <Icon.Search />
            </a>
          )}
          <a className="icb" aria-label={`Review queue, ${reviewCount} waiting`} href={buildHash('/review')}>
            <Icon.Bell />
            {reviewCount > 0 && <span className="dot">{reviewCount > 99 ? '99+' : reviewCount}</span>}
          </a>
          <Dropdown
            align="right"
            toggle={(p) => (
              <button type="button" className="icb" aria-label="Help" {...p}>
                <Icon.Help />
              </button>
            )}
          >
            {() => (
              <>
                <a href={`${REPO_URL}#readme`} target="_blank" rel="noopener noreferrer">
                  Documentation
                </a>
                <a href={`${REPO_URL}/issues/new`} target="_blank" rel="noopener noreferrer">
                  Report an issue
                </a>
              </>
            )}
          </Dropdown>
          <Dropdown
            align="right"
            toggle={(p) => (
              <button type="button" className="who" aria-label="Account" {...p}>
                <span className="av">{me?.initials ?? '··'}</span>
                {me?.name ?? ''}
              </button>
            )}
          >
            {() => (
              <>
                <div className="mh">
                  {me ? `Signed in as ${me.username}` : 'Not signed in'}
                  <br />
                  <span className="muted sm">{ROLE_LABEL[role]}</span>
                </div>
                {(me?.source === 'api-key' || me?.source === 'account') && (
                  <button type="button" onClick={onLogout}>
                    Log out
                  </button>
                )}
                {me?.source === 'oauth-proxy' && <a href="/oauth/sign_out">Log out</a>}
              </>
            )}
          </Dropdown>
        </div>
      </header>
      <div className={`shell ${open ? 'nav-open' : 'nav-closed'}`}>
        <nav className="nav" aria-label="Primary">
          {navFor(role).map((section, i) => (
            <React.Fragment key={section.group ?? i}>
              {section.group && <div className="ng">{section.group}</div>}
              {section.items.map((item) => (
                <a key={item.path} href={buildHash(item.path)} className={activePath === item.path ? 'on' : ''} aria-current={activePath === item.path ? 'page' : undefined}>
                  {item.label}
                  {item.badge === 'review' && reviewCount > 0 && <span className="cnt">{reviewCount}</span>}
                </a>
              ))}
            </React.Fragment>
          ))}
        </nav>
        <main className="main">{children}</main>
      </div>
    </div>
  );
};

/* ------------------------------------------------------------------ */
/* Page header                                                         */
/* ------------------------------------------------------------------ */

export const PageHeader: React.FC<{
  title: React.ReactNode;
  crumbs?: { label: React.ReactNode; href?: string }[];
  description?: React.ReactNode;
  actions?: React.ReactNode;
  tabs?: React.ReactNode;
  long?: boolean;
  topAlign?: boolean;
  below?: React.ReactNode;
}> = ({ title, crumbs, description, actions, tabs, long, topAlign, below }) => (
  <div className={`ph ${tabs ? 'has-tabs' : ''}`}>
    <div className="crumbs">
      <a href={buildHash('/overview')}>MemoryHub</a>
      {(crumbs ?? [{ label: title }]).map((c, i) => (
        <React.Fragment key={i}>
          <span>/</span>
          {c.href ? <a href={c.href}>{c.label}</a> : <span>{c.label}</span>}
        </React.Fragment>
      ))}
    </div>
    <div className={`ph-row ${topAlign ? 'top' : ''}`}>
      <div style={{ minWidth: 0, flex: topAlign ? '1 1 520px' : undefined }}>
        <h1 className={long ? 'long' : ''}>{title}</h1>
        {description && <p className="desc">{description}</p>}
        {below}
      </div>
      {actions && <div className="ph-actions">{actions}</div>}
    </div>
    {tabs}
  </div>
);

export function Tabs<T extends string>({
  value,
  tabs,
  onChange,
  label,
  inCard,
}: {
  value: T;
  tabs: { value: T; label: string; count?: number | string | null }[];
  onChange: (v: T) => void;
  label: string;
  inCard?: boolean;
}) {
  return (
    <nav className={`tabs ${inCard ? 'in-card' : ''}`} aria-label={label} role="tablist">
      {tabs.map((t) => (
        <button
          key={t.value}
          type="button"
          role="tab"
          aria-selected={t.value === value}
          className={`tab ${t.value === value ? 'on' : ''}`}
          onClick={() => onChange(t.value)}
        >
          {t.label}
          {t.count !== undefined && t.count !== null && <span className="muted"> {typeof t.count === 'number' ? t.count.toLocaleString() : t.count}</span>}
        </button>
      ))}
    </nav>
  );
}
