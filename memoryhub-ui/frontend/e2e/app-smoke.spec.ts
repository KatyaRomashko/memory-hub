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
      ['Projects & campaigns', 'projects', 'Projects & campaigns'],
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
    await expect(page.getByRole('img', { name: /Memory operations in 28 6-hour buckets/ })).toBeVisible();

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
    await expect(page.getByRole('link', { name: 'MemoryHub UI' })).toHaveAttribute('href', /#\/memories\?entity=/);
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

    await page.getByRole('group', { name: 'Layout' }).getByRole('button', { name: 'Timeline' }).click();
    await expect(page.getByRole('group', { name: 'Layout' }).getByRole('button', { name: 'Timeline' })).toHaveAttribute('aria-pressed', 'true');
    await expect(page.getByText('Brighter = written more recently')).toBeVisible();

    const related = page.getByRole('checkbox', { name: 'related_to' });
    await expect(related).toBeChecked();
    await related.uncheck();
    await expect(related).not.toBeChecked();
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
