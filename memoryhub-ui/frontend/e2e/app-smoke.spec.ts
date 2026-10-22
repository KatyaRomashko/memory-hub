import { expect, test } from '@playwright/test';
import { memoryId, mockDashboardApi } from './fixtures';

test.beforeEach(async ({ page }) => {
  await mockDashboardApi(page);
});

test.describe('primary navigation', () => {
  test('opens every primary destination', async ({ page }) => {
    const pageErrors: Error[] = [];
    page.on('pageerror', (error) => pageErrors.push(error));

    await page.goto('/#/overview');
    await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible();

    const destinations = [
      ['Memory browser', 'memories', 'Memory browser'],
      ['Entities', 'entities', 'Entities'],
      ['Search playground', 'search', 'Search playground'],
      ['Projects & teams', 'projects', 'Projects & teams'],
      ['Review queue', 'review', 'Review queue'],
      ['Curation rules', 'curation', 'Curation rules'],
      ['Audit & forensics', 'audit', 'Audit & forensics'],
      ['Users & agents', 'identities', 'Users & agents'],
      ['OAuth clients & keys', 'clients', 'OAuth clients & API keys'],
      ['Settings', 'settings', 'Settings'],
    ] as const;

    for (const [linkName, path, heading] of destinations) {
      await page.getByRole('link', { name: linkName, exact: true }).click();
      await expect(page).toHaveURL(new RegExp(`#/${path}$`));
      await expect(page.getByRole('heading', { name: heading, exact: true })).toBeVisible();
    }

    expect(pageErrors, pageErrors.map((error) => error.stack || error.message).join('\n')).toEqual([]);
  });
});

test.describe('shell controls', () => {
  test('toggles navigation and opens utility menus', async ({ page }) => {
    await page.goto('/#/overview');

    const navToggle = page.getByRole('button', { name: 'Hide navigation' });
    await navToggle.click();
    await expect(page.getByRole('button', { name: 'Show navigation' })).toBeVisible();
    await page.getByRole('button', { name: 'Show navigation' }).click();
    await expect(page.getByRole('button', { name: 'Hide navigation' })).toBeVisible();

    await page.getByRole('button', { name: 'Help' }).click();
    await expect(page.getByRole('menu')).toContainText('Documentation');
    await page.keyboard.press('Escape');
    await expect(page.getByRole('menu')).toBeHidden();

    await page.getByRole('button', { name: 'Account' }).click();
    await expect(page.getByRole('menu')).toContainText('Signed in as admin');
  });

  test('global search and review shortcuts navigate to the expected screens', async ({ page }) => {
    await page.goto('/#/overview');
    await page.getByRole('link', { name: 'Search memories' }).click();
    await expect(page).toHaveURL(/#\/search$/);
    await expect(page.getByRole('heading', { name: 'Search playground', exact: true })).toBeVisible();

    await page.getByRole('link', { name: 'Review queue', exact: true }).click();
    await expect(page).toHaveURL(/#\/review$/);
    await expect(page.getByRole('heading', { name: 'Review queue', exact: true })).toBeVisible();
  });
});

test.describe('overview activity', () => {
  test('charts memory operations and loads each activity range', async ({ page }) => {
    await page.goto('/#/overview');
    await expect(page.getByRole('heading', { name: 'Memory operations', exact: true })).toBeVisible();
    await expect(page.getByRole('img', { name: /Memory operations in 7 daily buckets/ })).toBeVisible();

    const rangeGroup = page.getByRole('group', { name: 'Memory operation range' });
    await expect(rangeGroup.getByRole('button')).toHaveCount(6);
    for (const label of ['All time', '1h', '12h', '1d', '7d', '30d']) {
      await expect(rangeGroup.getByRole('button', { name: label, exact: true })).toBeVisible();
    }

    const overviewRequest = page.waitForRequest((request) => request.url().includes('/api/overview?range=1d'));
    await rangeGroup.getByRole('button', { name: '1d', exact: true }).click();
    await overviewRequest;
    await expect(page.getByText('Last 24 hours', { exact: true })).toBeVisible();
    await expect(page.getByRole('img', { name: /24 hourly buckets/ })).toBeVisible();

    const refreshRequest = page.waitForRequest((request) => request.url().includes('/api/overview?range=1d'));
    await page.getByRole('button', { name: 'Refresh memory operations' }).click();
    await refreshRequest;
  });

  test('KPI tiles link to their screens', async ({ page }) => {
    await page.goto('/#/overview');
    await page.getByRole('link', { name: /Agents connected/ }).click();
    await expect(page).toHaveURL(/#\/identities$/);
  });

  test('renders the category mix as a pie with a legend', async ({ page }) => {
    await page.goto('/#/overview');
    await expect(page.getByRole('heading', { name: 'Memory categories', exact: true })).toBeVisible();
    await expect(page.getByRole('img', { name: /^Memory categories:/ })).toBeVisible();
    await expect(page.getByRole('link', { name: /tooling/ })).toBeVisible();
    await expect(page.getByRole('link', { name: /containers/ })).toBeVisible();
  });
});

test.describe('entities', () => {
  test('browses extracted entities and links them to their memories', async ({ page }) => {
    await page.goto('/#/entities');
    await expect(page.getByRole('heading', { name: 'Entities', exact: true })).toBeVisible();
    await expect(page.getByRole('tab', { name: /All/ })).toBeVisible();
    await expect(page.getByRole('link', { name: 'MemoryHub UI' })).toHaveAttribute('href', /#\/entity\//);
    await expect(page.getByText('memoryhub-ui', { exact: true })).toBeVisible();
    await page.getByLabel('Search entities').fill('MemoryHub');
    await page.getByRole('button', { name: 'Apply filters' }).click();
    await expect(page).toHaveURL(/#\/entities\?q=MemoryHub/);
  });
});

test.describe('memory graph', () => {
  test('table/graph toggle, focus, layouts, depth and relationship filters', async ({ page }) => {
    await page.goto('/#/memories');
    await page.getByRole('group', { name: 'View as' }).getByRole('button', { name: 'Graph' }).click();
    await expect(page).toHaveURL(/view=graph/);
    // Graph view swaps the table filters for graph settings and hides the state tabs.
    await expect(page.getByRole('heading', { name: 'Graph settings' })).toBeVisible();
    await expect(page.getByRole('tab', { name: /All versions/ })).toHaveCount(0);
    await expect(page.getByRole('img', { name: /2 memories and 1 relationships/ })).toBeVisible();

    await page.locator('svg g.node').first().click();
    await expect(page.getByRole('link', { name: 'Open memory' })).toHaveAttribute('href', new RegExp(memoryId));
    await page.getByRole('button', { name: 'Show only its connections' }).click();
    await expect(page).toHaveURL(/gfocus=memory/);

    await page.getByRole('group', { name: 'Depth' }).getByRole('button', { name: '3 hops' }).click();
    await expect(page).toHaveURL(/depth=3/);

    // Time is its own view now, not a graph layout.
    await expect(page.getByRole('group', { name: 'Layout' }).getByRole('button', { name: 'Timeline' })).toHaveCount(0);

    const related = page.getByRole('checkbox', { name: 'related_to' });
    await expect(related).toBeChecked();
    await related.uncheck();
    await expect(related).not.toBeChecked();
  });

  test('graph says when it is truncated and offers a bigger limit', async ({ page }) => {
    await page.route('**/api/graph/all**', (route) =>
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ nodes: [], edges: [], total: 900, truncated: true, left_out: 600, max_nodes: 300, limit_ceiling: 1500, prefer: 'connected', orphan_count: 0, hidden_links: 12, metadata_only: false }),
      }),
    );
    await page.goto('/#/memories?view=graph');
    await expect(page.getByText(/Showing 0 of 900 memories/)).toBeVisible();
    await page.getByRole('button', { name: 'Draw up to 600' }).click();
    await expect(page).toHaveURL(/glimit=600/);
  });

  test('old graph links land in the browser graph view', async ({ page }) => {
    await page.goto(`/#/graph?focus=${memoryId}`);
    await expect(page).toHaveURL(/#\/memories\?view=graph&gfocus=memory&focus=/);
  });
});

test.describe('write and credentials actions', () => {
  test('writes a memory through the modal', async ({ page }) => {
    await page.goto('/#/memories');
    await expect(page.getByRole('heading', { name: 'Memory browser', exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Write memory' }).click();
    const dialog = page.getByRole('dialog', { name: 'Write memory' });
    await expect(dialog).toBeVisible();
    await dialog.getByLabel('Content').fill('A test memory created by the browser smoke suite.');
    await dialog.getByRole('button', { name: 'Write memory', exact: true }).click();
    await expect(dialog).toBeHidden();
    await expect(page.getByText('Memory written.')).toBeVisible();
  });

  test('opens the OAuth client form and submits it', async ({ page }) => {
    await page.goto('/#/clients');
    await expect(page.getByRole('heading', { name: 'OAuth clients & API keys', exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Create OAuth client' }).click();
    await expect(page.getByRole('dialog', { name: 'Create OAuth client' })).toBeVisible();
    await page.getByLabel('client_id').fill('browser-test-agent');
    await page.getByLabel('Display name').fill('Browser test agent');
    await page.getByRole('button', { name: 'Create', exact: true }).click();
    const credentials = page.getByRole('dialog', { name: 'Copy these credentials now' });
    await expect(credentials).toBeVisible();
    await expect(credentials.getByText('browser-test-agent', { exact: true })).toBeVisible();
  });
});

test.describe('memory workspace', () => {
  test('timeline view keeps filters and selection across views', async ({ page }) => {
    await page.goto('/#/memories?scope=project');
    await page.getByRole('group', { name: 'View as' }).getByRole('button', { name: 'Timeline' }).click();
    await expect(page).toHaveURL(/view=timeline/);
    await expect(page).toHaveURL(/scope=project/);
    await expect(page.getByRole('heading', { name: 'Timeline settings' })).toBeVisible();
    await expect(page.getByText('Filters from the table:')).toBeVisible();
    await page.locator('svg g.node').first().click();
    await expect(page).toHaveURL(/sel=/);
    await page.getByRole('group', { name: 'View as' }).getByRole('button', { name: 'Graph' }).click();
    await expect(page).toHaveURL(/sel=/);
  });

  test('old timeline layout links open the timeline view', async ({ page }) => {
    await page.goto('/#/memories?view=graph&layout=timeline');
    await expect(page).toHaveURL(/view=timeline/);
  });

  test('knowledge page cites its memories', async ({ page }) => {
    await page.goto('/#/knowledge');
    await page.getByRole('link', { name: /What memory knows about Admin User/ }).click();
    await expect(page).toHaveURL(/#\/knowledge\/me/);
    await expect(page.getByRole('heading', { name: 'In short' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Tooling' })).toBeVisible();
    await expect(page.getByText('+1 similar folded')).toBeVisible();
    await expect(page.locator('a.cite').first()).toHaveAttribute('href', new RegExp(memoryId));
  });

  test('entity page shows memories and offers rename and merge', async ({ page }) => {
    await page.goto('/#/entities');
    await page.getByRole('link', { name: 'MemoryHub UI' }).click();
    await expect(page.getByRole('heading', { name: /MemoryHub UI/ })).toBeVisible();
    await expect(page.getByText('Also known as ui.')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Merge into…' })).toBeVisible();
    await page.getByRole('button', { name: 'Rename · aliases' }).click();
    await expect(page.getByRole('textbox', { name: 'Aliases' })).toHaveValue('ui');
  });

  test('deleted memories can be restored from the table', async ({ page }) => {
    let restored = false;
    await page.route('**/api/memories/bulk', async (route) => {
      restored = JSON.parse(route.request().postData() || '{}').action === 'restore';
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ done: [memoryId], errors: [] }) });
    });
    await page.goto('/#/memories?tab=deleted');
    await page.getByRole('checkbox', { name: 'Select memory' }).first().check();
    await page.getByRole('button', { name: 'Restore', exact: true }).click();
    await expect.poll(() => restored).toBe(true);
  });

  test('notifications open from the bell', async ({ page }) => {
    await page.goto('/#/overview');
    await page.getByRole('button', { name: /Notifications, 1 new/ }).click();
    await expect(page.getByText('Other agents used your memories 2 times today')).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Getting started' })).toBeVisible();
  });
});

test.describe('quality, packs, documents, operations', () => {
  test('content tabs appear in the menu once they have content', async ({ page }) => {
    await page.goto('/#/overview');
    const nav = page.getByRole('navigation');
    await expect(nav.getByRole('link', { name: 'Documents', exact: true })).toBeVisible();
    await expect(nav.getByRole('link', { name: 'Context packs', exact: true })).toBeVisible();
    await expect(nav.getByRole('link', { name: 'Operations', exact: true })).toBeVisible();
  });

  test('content tabs stay hidden while empty', async ({ page }) => {
    await page.route('**/api/nav-features', (route) => route.fulfill({ contentType: 'application/json', body: '{}' }));
    await page.goto('/#/overview');
    const nav = page.getByRole('navigation');
    await expect(nav.getByRole('link', { name: 'Memory browser', exact: true })).toBeVisible();
    await expect(nav.getByRole('link', { name: 'Documents', exact: true })).toHaveCount(0);
    await expect(nav.getByRole('link', { name: 'Context packs', exact: true })).toHaveCount(0);
    await expect(nav.getByRole('link', { name: 'Operations', exact: true })).toHaveCount(0);
  });

  test('retrieval checks show recall and why a memory is missing', async ({ page }) => {
    await page.goto('/#/quality');
    await expect(page.getByText('50% · 1/2')).toBeVisible();
    await expect(page.getByText('below weight threshold')).toBeVisible();
    await page.getByRole('tab', { name: 'My search health' }).click();
    await expect(page.getByRole('heading', { name: 'Where matches are cut' })).toBeVisible();
  });

  test('context pack renders a preview within its budget', async ({ page }) => {
    await page.goto('/#/packs');
    await page.getByRole('link', { name: /Local dev pack/ }).click();
    await expect(page).toHaveURL(/#\/packs\/p1/);
    await expect(page.getByText('12 of 2,000 tokens · 1 memories')).toBeVisible();
    await expect(page.getByText(/get_context_pack/)).toBeVisible();
  });

  test('document passages show their memories and can be selected', async ({ page }) => {
    await page.goto('/#/documents/d1');
    await expect(page.getByRole('heading', { name: 'Dev handbook' })).toBeVisible();
    await page.getByRole('checkbox', { name: 'Passage 2' }).check();
    await expect(page.getByRole('button', { name: 'Write 1 memory' })).toBeEnabled();
  });

  test('operations lists jobs and offers a retry', async ({ page }) => {
    await page.goto('/#/operations');
    await expect(page.getByText('database timeout')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Retry' })).toBeVisible();
  });
});
