import React, { useEffect, useState } from 'react';
import { api, AUTH_REQUIRED_EVENT } from '@/api/client';
import { AppProvider, canSee, NAV, Shell, useApp } from '@/components/Shell';
import { Empty } from '@/components/ui';
import { ToastProvider } from '@/components/ui';
import { navigate, useHashRoute, type HashRoute } from '@/hooks/useHashRoute';
import Accounts from '@/pages/Accounts';
import Audit from '@/pages/Audit';
import Preferences from '@/pages/Preferences';
import WhoRead from '@/pages/WhoRead';
import Dreaming from '@/pages/Dreaming';
import Clients from '@/pages/Clients';
import Curation from '@/pages/Curation';
import Entities from '@/pages/Entities';
import EntityDetail from '@/pages/EntityDetail';
import Knowledge from '@/pages/Knowledge';
import Documents from '@/pages/Documents';
import Operations from '@/pages/Operations';
import Packs from '@/pages/Packs';
import Quality from '@/pages/Quality';
import Graph from '@/pages/Graph';
import Identities from '@/pages/Identities';
import Login from '@/pages/Login';
import Memories from '@/pages/Memories';
import MemoryDetail from '@/pages/MemoryDetail';
import Overview from '@/pages/Overview';
import Projects from '@/pages/Projects';
import Review from '@/pages/Review';
import Search from '@/pages/Search';
import Settings from '@/pages/Settings';

interface Screen {
  nav: string;
  title: string;
  render: (route: HashRoute) => React.ReactNode;
}

const SCREENS: Record<string, Screen> = {
  '/overview': { nav: '/overview', title: 'Overview', render: () => <Overview /> },
  '/memories': { nav: '/memories', title: 'Memory browser', render: (r) => <Memories route={r} /> },
  '/memory': { nav: '/memories', title: 'Memory detail', render: (r) => <MemoryDetail route={r} /> },
  '/graph': { nav: '/memories', title: 'Memory graph', render: (r) => <Graph route={r} /> },
  '/accounts': { nav: '/accounts', title: 'Accounts & sign-up', render: () => <Accounts /> },
  '/entities': { nav: '/entities', title: 'Entities', render: (r) => <Entities route={r} /> },
  '/entity': { nav: '/entities', title: 'Entity', render: (r) => <EntityDetail route={r} /> },
  '/knowledge': { nav: '/knowledge', title: 'Knowledge', render: (r) => <Knowledge route={r} /> },
  '/documents': { nav: '/documents', title: 'Documents', render: (r) => <Documents route={r} /> },
  '/packs': { nav: '/packs', title: 'Context packs', render: (r) => <Packs route={r} /> },
  '/quality': { nav: '/quality', title: 'Retrieval quality', render: (r) => <Quality route={r} /> },
  '/operations': { nav: '/operations', title: 'Operations', render: () => <Operations /> },
  '/search': { nav: '/search', title: 'Search playground', render: (r) => <Search route={r} /> },
  '/projects': { nav: '/projects', title: 'Projects & teams', render: (r) => <Projects route={r} /> },
  '/dreaming': { nav: '/dreaming', title: 'Dreaming', render: () => <Dreaming /> },
  '/reads': { nav: '/reads', title: 'Who read my memories', render: (r) => <WhoRead route={r} /> },
  '/preferences': { nav: '/preferences', title: 'Preferences & privacy', render: () => <Preferences /> },
  '/review': { nav: '/review', title: 'Review queue', render: (r) => <Review route={r} /> },
  '/curation': { nav: '/curation', title: 'Curation rules', render: (r) => <Curation route={r} /> },
  '/audit': { nav: '/audit', title: 'Audit & forensics', render: (r) => <Audit route={r} /> },
  '/identities': { nav: '/identities', title: 'Users & agents', render: (r) => <Identities route={r} /> },
  '/clients': { nav: '/clients', title: 'OAuth clients & keys', render: (r) => <Clients route={r} /> },
  '/settings': { nav: '/settings', title: 'Settings', render: () => <Settings /> },
};

// Old bookmarks from the previous dashboard keep working.
const LEGACY: Record<string, { path: string; params?: Record<string, string> }> = {
  '/status': { path: '/overview' },
  '/contradictions': { path: '/review', params: { tab: 'contradictions' } },
  '/curation-rules': { path: '/curation' },
  '/rules': { path: '/curation' },
  '/users': { path: '/identities' },
};

const AUTH_PATHS = ['/login', '/register', '/setup'];

/** Screens a role may not open show a short explanation instead. */
const Guarded: React.FC<{ nav: string; children: React.ReactNode }> = ({ nav, children }) => {
  const { me, role } = useApp();
  const listed = NAV.some((s) => s.items.some((i) => i.path === nav));
  if (!me || !listed || canSee(nav, role)) return <>{children}</>;
  return (
    <div className="body">
      <div className="card">
        <Empty title={role === 'admin' ? 'Not available to platform admins' : 'Only platform admins can open this'}>
          {role === 'admin'
            ? 'This screen shows what memories say. Memory content is confidential: admins see metadata only, in Memory analytics.'
            : 'Platform settings, identities, keys, rules and the audit log are managed by platform admins.'}
        </Empty>
      </div>
    </div>
  );
};

const screenKey = (path: string) => {
  if (path.startsWith('/memory/')) return '/memory';
  if (path.startsWith('/entity/')) return '/entity';
  if (path.startsWith('/knowledge/')) return '/knowledge';
  if (path.startsWith('/documents/')) return '/documents';
  if (path.startsWith('/packs/')) return '/packs';
  return path;
};

const Routed: React.FC = () => {
  const route = useHashRoute();
  const [authRequired, setAuthRequired] = useState(false);

  useEffect(() => {
    api
      .authStatus()
      .then((s) => setAuthRequired(!s.authenticated || s.setup_required))
      .catch(() => undefined);
    const onAuth = () => setAuthRequired(true);
    window.addEventListener(AUTH_REQUIRED_EVENT, onAuth);
    return () => window.removeEventListener(AUTH_REQUIRED_EVENT, onAuth);
  }, []);

  useEffect(() => {
    const legacy = LEGACY[route.path];
    if (legacy) navigate(legacy.path, legacy.params, { replace: true });
    else if (route.path === '/' || (!SCREENS[screenKey(route.path)] && !AUTH_PATHS.includes(route.path))) {
      navigate('/overview', undefined, { replace: true });
    }
  }, [route.path]);

  const key = screenKey(route.path);
  const screen = SCREENS[key];

  useEffect(() => {
    const title =
      route.path === '/login' ? 'Log in' : route.path === '/register' ? 'Create account' : route.path === '/setup' ? 'Set up' : screen?.title;
    document.title = title ? `MemoryHub · ${title}` : 'MemoryHub';
  }, [screen, route.path]);

  if (AUTH_PATHS.includes(route.path) || authRequired) {
    const view = route.path === '/register' ? 'register' : route.path === '/setup' ? 'setup' : 'login';
    return (
      <Login
        view={view}
        invite={route.params.get('invite') ?? undefined}
        signedOut={route.params.get('signed_out') === '1'}
        onLoggedIn={() => setAuthRequired(false)}
      />
    );
  }
  if (!screen) return null;
  const known = NAV.some((s) => s.items.some((i) => i.path === screen.nav));
  return (
    <AppProvider>
      <Shell activePath={known ? screen.nav : ''}>
        {/* key resets page state when moving between screens */}
        <React.Fragment key={key}>
          <Guarded nav={screen.nav}>{screen.render(route)}</Guarded>
        </React.Fragment>
      </Shell>
    </AppProvider>
  );
};

const App: React.FC = () => (
  <ToastProvider>
    <Routed />
  </ToastProvider>
);

export default App;
