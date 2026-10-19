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
      ['Memory graph', 'graph', 'Memory graph'],
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

test.describe('memory graph', () => {
  test('focus picker, layout, depth and relationship filters are interactive', async ({ page }) => {
    await page.goto('/#/graph');
    await expect(page.getByRole('heading', { name: 'Memory graph', exact: true })).toBeVisible();
    await expect(page.getByRole('img', { name: /2 memories and 1 relationships/ })).toBeVisible();

    await page.getByRole('button', { name: /Focus:/ }).click();
    await expect(page.getByRole('dialog', { name: 'Focus memory' })).toBeVisible();
    await page.getByRole('dialog', { name: 'Focus memory' }).getByRole('button', { name: 'Close', exact: true }).click();
    await page.locator('svg g.node').first().click();
    await expect(page.getByRole('link', { name: 'Open memory' })).toHaveAttribute('href', new RegExp(memoryId));

    await page.getByRole('group', { name: 'Layout' }).getByRole('button', { name: 'Timeline' }).click();
    await expect(page.getByRole('group', { name: 'Layout' }).getByRole('button', { name: 'Timeline' })).toHaveAttribute('aria-pressed', 'true');

    await page.getByRole('button', { name: 'Depth' }).click();
    await page.getByRole('menuitemradio', { name: '3 hops' }).click();
    await expect(page).toHaveURL(/depth=3/);

    const related = page.getByRole('checkbox', { name: 'related_to' });
    await expect(related).toBeChecked();
    await related.uncheck();
    await expect(related).not.toBeChecked();

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
