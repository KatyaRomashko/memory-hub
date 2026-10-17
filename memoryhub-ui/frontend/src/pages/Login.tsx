import React, { useEffect, useState } from 'react';
import { api } from '@/api/client';
import { Icon } from '@/components/ui';
import { navigate } from '@/hooks/useHashRoute';
import type { AuthStatus } from '@/types';
import { errorMessage } from '@/utils/format';

/**
 * Screen 13. In production the OpenShift OAuth proxy authenticates before the
 * page loads, so "Log in with OpenShift" simply (re)starts that flow. The
 * API-key form is only enabled when MEMORYHUB_UI_DEV_API_KEY is set.
 */
const Login: React.FC<{ onLoggedIn: () => void }> = ({ onLoggedIn }) => {
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [key, setKey] = useState('');
  const [user, setUser] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.authStatus().then(setStatus).catch(() => setStatus(null));
  }, []);

  const proceed = () => {
    onLoggedIn();
    navigate('/overview');
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(key, user.trim() || undefined);
      proceed();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const openshift = () => {
    if (status?.mode === 'oauth-proxy' || status?.mode === 'open') proceed();
    else window.location.href = '/oauth/start?rd=/';
  };

  return (
    <div style={{ minHeight: '100vh', position: 'relative', overflow: 'hidden', display: 'flex', flexDirection: 'column', color: '#fff', background: '#151515' }}>
      <svg aria-hidden="true" viewBox="0 0 1440 900" preserveAspectRatio="xMidYMid slice" style={{ position: 'absolute', inset: 0, width: '100%', height: '100%' }}>
        <g fill="none" stroke="#2a2a2a" strokeWidth="1.5">
          {[120, 240, 360, 480].map((r) => (
            <circle key={r} cx="1180" cy="300" r={r} />
          ))}
        </g>
        <g stroke="#3a3a3a" strokeWidth="1.5">
          <line x1="1040" y1="180" x2="1300" y2="420" />
          <line x1="1300" y1="180" x2="980" y2="520" />
          <line x1="900" y1="300" x2="1440" y2="300" />
        </g>
        <g fill="#ee0000">
          <circle cx="1040" cy="180" r="6" />
          <circle cx="1300" cy="420" r="6" />
        </g>
        <g fill="#4d4d4d">
          <circle cx="1300" cy="180" r="5" />
          <circle cx="980" cy="520" r="5" />
          <circle cx="1180" cy="300" r="7" />
        </g>
      </svg>
      <header style={{ position: 'relative', display: 'flex', alignItems: 'center', gap: 10, padding: '28px 48px' }}>
        <span style={{ width: 34, height: 34, borderRadius: 9, background: '#ee0000', display: 'grid', placeItems: 'center' }}>
          <Icon.Mark size={20} />
        </span>
        <span style={{ fontFamily: 'var(--mh-font-display)', fontWeight: 600, fontSize: 20 }}>MemoryHub</span>
        <span style={{ color: '#c7c7c7', fontSize: 14 }}>for OpenShift AI</span>
      </header>
      <div style={{ position: 'relative', flex: 1, display: 'flex', flexWrap: 'wrap', gap: 48, alignItems: 'center', padding: '24px 48px 64px', maxWidth: 1280 }}>
        <form onSubmit={submit} style={{ flex: '0 1 440px', background: '#fff', color: '#151515', borderRadius: 20, padding: '40px 40px 32px', display: 'flex', flexDirection: 'column', gap: 20 }}>
          <div>
            <h1 style={{ fontFamily: 'var(--mh-font-display)', fontSize: 28, fontWeight: 500, margin: '0 0 6px' }}>Log in to your account</h1>
            <p style={{ margin: 0, color: '#4d4d4d', fontSize: 14 }}>Use your OpenShift identity. Your access to memories follows your cluster roles.</p>
          </div>
          <button type="button" className="btn bp block" style={{ fontSize: 15, padding: '10px 20px' }} onClick={openshift}>
            Log in with OpenShift
          </button>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, color: '#4d4d4d', fontSize: 13 }}>
            <span style={{ flex: 1, borderTop: '1px solid #d2d2d2' }} />
            or, for development clusters
            <span style={{ flex: 1, borderTop: '1px solid #d2d2d2' }} />
          </div>
          <div className="f">
            <label htmlFor="k">API key</label>
            <input
              id="k"
              className="in"
              type="password"
              placeholder="mh-dev-…"
              style={{ padding: '10px 12px' }}
              value={key}
              disabled={status ? !status.api_key_login : false}
              onChange={(e) => setKey(e.target.value)}
              autoComplete="current-password"
            />
          </div>
          {status?.api_key_login && (
            <div className="f">
              <label htmlFor="u">Your name (shown in the audit log)</label>
              <input id="u" className="in" style={{ padding: '10px 12px' }} value={user} onChange={(e) => setUser(e.target.value)} placeholder="dev-operator" autoComplete="username" />
            </div>
          )}
          {error && <div className="alert err-a">{error}</div>}
          <button type="submit" className="btn bg block" style={{ fontSize: 15, padding: '10px 20px' }} disabled={busy || !key || (status ? !status.api_key_login : false)}>
            {busy ? 'Checking…' : 'Continue with API key'}
          </button>
          <p style={{ margin: 0, fontSize: 13, color: '#4d4d4d', lineHeight: 1.5 }}>
            {status && !status.api_key_login
              ? 'API-key login is disabled on this deployment. '
              : 'API-key sessions are for single-operator setups. Production deployments should use OAuth 2.1. '}
            <a href="https://github.com/redhat-ai-americas/memory-hub#readme" target="_blank" rel="noopener noreferrer">
              Learn about authentication
            </a>
          </p>
        </form>
        <div style={{ flex: '1 1 420px', maxWidth: 560 }}>
          <h2 style={{ fontFamily: 'var(--mh-font-display)', fontSize: 44, fontWeight: 500, lineHeight: 1.12, margin: '0 0 18px', letterSpacing: '-.02em', color: '#fff' }}>
            Governed memory for every agent in your cluster.
          </h2>
          <p style={{ color: '#c7c7c7', fontSize: 17, lineHeight: 1.55, margin: '0 0 28px' }}>
            Shared, versioned and audited memory over MCP — with six-tier scopes, contradiction handling and a forensic trail that answers what an agent knew and why.
          </p>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14, fontSize: 15 }}>
            {['Scope isolation enforced in SQL, not in prompts', 'Human approval for enterprise policy memories', 'Air-gap ready with on-cluster embedding models'].map((t) => (
              <div key={t} style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
                <span style={{ width: 8, height: 8, borderRadius: 99, background: '#ee0000' }} />
                {t}
              </div>
            ))}
          </div>
        </div>
      </div>
      <footer style={{ position: 'relative', padding: '20px 48px', fontSize: 13, color: '#a3a3a3', display: 'flex', gap: 20, flexWrap: 'wrap' }}>
        <span>MemoryHub{status?.cluster_domain ? ` · cluster ${status.cluster_domain}` : ''}</span>
        <a href="https://github.com/redhat-ai-americas/memory-hub#readme" style={{ color: '#c7c7c7' }} target="_blank" rel="noopener noreferrer">
          Help
        </a>
      </footer>
    </div>
  );
};

export default Login;
