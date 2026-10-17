import React, { useEffect, useState } from 'react';
import { api, AUTH_REQUIRED_EVENT } from '@/api/client';
import { AppProvider, NAV, Shell } from '@/components/Shell';
import { ToastProvider } from '@/components/ui';
import { navigate, useHashRoute, type HashRoute } from '@/hooks/useHashRoute';
import Audit from '@/pages/Audit';
import Clients from '@/pages/Clients';
import Curation from '@/pages/Curation';
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
  '/graph': { nav: '/graph', title: 'Memory graph', render: (r) => <Graph route={r} /> },
  '/search': { nav: '/search', title: 'Search playground', render: (r) => <Search route={r} /> },
  '/projects': { nav: '/projects', title: 'Projects & campaigns', render: (r) => <Projects route={r} /> },
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

const screenKey = (path: string) => {
  if (path.startsWith('/memory/')) return '/memory';
  return path;
};

const Routed: React.FC = () => {
  const route = useHashRoute();
  const [authRequired, setAuthRequired] = useState(false);

  useEffect(() => {
    api
      .authStatus()
      .then((s) => setAuthRequired(!s.authenticated))
      .catch(() => undefined);
    const onAuth = () => setAuthRequired(true);
    window.addEventListener(AUTH_REQUIRED_EVENT, onAuth);
    return () => window.removeEventListener(AUTH_REQUIRED_EVENT, onAuth);
  }, []);

  useEffect(() => {
    const legacy = LEGACY[route.path];
    if (legacy) navigate(legacy.path, legacy.params, { replace: true });
    else if (route.path === '/' || (!SCREENS[screenKey(route.path)] && route.path !== '/login')) {
      navigate('/overview', undefined, { replace: true });
    }
  }, [route.path]);

  const key = screenKey(route.path);
  const screen = SCREENS[key];

  useEffect(() => {
    const title = route.path === '/login' ? 'Log in' : screen?.title;
    document.title = title ? `MemoryHub · ${title}` : 'MemoryHub';
  }, [screen, route.path]);

  if (route.path === '/login' || authRequired) {
    return <Login onLoggedIn={() => setAuthRequired(false)} />;
  }
  if (!screen) return null;
  const known = NAV.some((s) => s.items.some((i) => i.path === screen.nav));
  return (
    <AppProvider>
      <Shell activePath={known ? screen.nav : ''}>
        {/* key resets page state when moving between screens */}
        <React.Fragment key={key}>{screen.render(route)}</React.Fragment>
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
