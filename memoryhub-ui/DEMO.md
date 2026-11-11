# MemoryHub standalone UI demo

This branch renders the three MemoryHub interface roles with synthetic examples. The role selector in the account menu is a presentation control; it is not authentication or authorization. Every action changes in-memory frontend data only and resets on refresh. The app never contacts a MemoryHub backend, database, auth service, agent, API, or MCP endpoint.

## Local build and preview

```bash
cd memoryhub-ui/frontend
npm ci --no-audit --no-fund
npm run build
npm run preview -- --host 127.0.0.1
# Open http://127.0.0.1:4173/#/overview
```

Run the browser checks from the same directory:

```bash
npx playwright test e2e/demo.spec.ts --reporter=line --workers=1
```

The frontend needs no environment variables. The included `server.mjs` serves the production build on port 8080 with `/healthz` for OpenShift probes and a Content Security Policy that blocks network connections from the app.

## QA cluster preview

Authenticate to the target OpenShift cluster first, using your own credentials. From the repository root on branch `memoryhub-ui`:

```bash
MEMORYHUB_UI_NAMESPACE=memoryhub-ui memoryhub-ui/deploy/build.sh
MEMORYHUB_UI_NAMESPACE=memoryhub-ui memoryhub-ui/deploy/deploy.sh
oc get route memoryhub-ui -n memoryhub-ui -o jsonpath='https://{.spec.host}/'
```

The expected route is `https://memoryhub-ui-memoryhub-ui.apps.ocp.crz5h.sandbox1099.opentlc.com/`. The build context contains only the frontend source, `server.mjs`, and the Containerfile. The Deployment has one static Node container and no backend service, OAuth proxy, secrets, or database settings.

The scripts make cluster changes, so run them only when you are ready to deploy. This branch does not deploy itself.
