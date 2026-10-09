import { test, expect, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';

const headers = (account: string) => ({ Authorization: `Bearer web-synthetic-${account}` });
async function signIn(page: Page, account: string) {
  await page.goto('/');
  await page.evaluate((token) => sessionStorage.setItem('city-runner.session', token), `web-synthetic-${account}`);
  await page.reload();
  await expect(page.getByRole('button', { name: 'Sign out', exact: true })).toBeVisible();
}
async function upload(page: Page, account: string) {
  const response = await page.request.post('/api/uploads', { headers: headers(account), multipart: {
    file: { name: 'browser.gpx', mimeType: 'application/gpx+xml', buffer: readFileSync('tests/.fixtures/browser.gpx') },
  } });
  expect(response.status()).toBe(202);
  return await response.json() as { id: string; duplicate: boolean };
}
async function ready(page: Page, account: string, source: string) {
  await expect.poll(async () => (await (await page.request.get(`/api/uploads/${source}`, { headers: headers(account) })).json()).status).toBe('succeeded');
  await expect.poll(async () => (await (await page.request.get('/api/progress', { headers: headers(account) })).json()).state).toBe('ready');
}

test('lightweight polling notices another client without resetting filters, selected detail or planner drafts', async ({ page }) => {
  let unchanged = 0; let activityReads = 0;
  page.on('request', (request) => { if (new URL(request.url()).pathname === '/api/activities') activityReads++; });
  page.on('response', (response) => { if (new URL(response.url()).pathname === '/api/sync/status' && response.status() === 204) unchanged++; });
  await signIn(page, 'sync');
  await page.getByRole('button', { name: 'Activities', exact: true }).click();
  await expect.poll(() => unchanged, { timeout: 15_000 }).toBeGreaterThan(0);
  const baselineReads = activityReads; const baselinePolls = unchanged;
  await expect.poll(() => unchanged, { timeout: 15_000 }).toBeGreaterThan(baselinePolls + 1);
  expect(activityReads).toBe(baselineReads);
  const controls = page.getByRole('region', { name: 'Activity and map filters' });
  await controls.locator('summary').click();
  await controls.getByLabel('File source').selectOption('gpx');
  await controls.getByLabel('Map coverage').selectOption('filtered');
  await controls.getByRole('button', { name: 'Apply filters' }).click();

  const accepted = await upload(page, 'sync');
  await ready(page, 'sync', accepted.id);
  await expect(page.locator('.activity-row')).toHaveCount(1, { timeout: 15_000 });
  await expect(controls.locator('.filter-summary')).toContainText('GPX');
  await expect(page.locator('.map-heading')).toContainText('Selected activities’ GPS coverage');
  await page.locator('.activity-row').click();
  await expect(page.getByRole('heading', { name: 'Browser GPX [synthetic]' })).toBeVisible();
  await page.getByRole('button', { name: 'Routes', exact: true }).click();
  await page.getByLabel('Route name').fill('Keep my unsaved route');
  const duplicate = await upload(page, 'sync');
  expect(duplicate.duplicate).toBe(true);
  expect(duplicate.id).toBe(accepted.id);

  // A real manual correction changes coverage from another client.
  const progress = await (await page.request.get('/api/progress', { headers: headers('sync') })).json();
  const dataset = progress.datasets[0].dataset_id;
  const cities = await (await page.request.get(`/api/cities?dataset_id=${dataset}`, { headers: headers('sync') })).json();
  const streets = await (await page.request.get(`/api/cities/${cities.items[0].id}/streets?dataset_id=${dataset}`, { headers: headers('sync') })).json();
  const changed = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/sync/status' && response.status() === 200);
  expect((await page.request.put(`/api/streets/${streets.items[0].id}/manual-completion?dataset_id=${dataset}`, {
    headers: headers('sync'), data: { reason: 'Synthetic remote correction' },
  })).status()).toBe(204);
  await changed;
  await expect(page.getByLabel('Route name')).toHaveValue('Keep my unsaved route');
  await page.getByRole('button', { name: 'Activities', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Browser GPX [synthetic]' })).toBeVisible();
  const activity = await (await page.request.get(`/api/uploads/${accepted.id}`, { headers: headers('sync') })).json();
  expect((await page.request.delete(`/api/activities/${activity.activity_id}`, { headers: headers('sync') })).status()).toBe(204);
  await expect(page.locator('.activity-row')).toHaveCount(0, { timeout: 15_000 });
  await expect(page.getByRole('heading', { name: 'Browser GPX [synthetic]' })).toHaveCount(0);
});

test('sources show real history and failed matching retry without claiming cloud connection', async ({ page, context }) => {
  const accepted = await upload(page, 'sync-failure');
  await ready(page, 'sync-failure', accepted.id);
  const before = await (await page.request.get('/api/sync/status', { headers: headers('sync-failure') })).json();
  expect(before.activity_count).toBe(1);
  expect(before.files.gpx.imported).toBe(1);
  expect(before.coverage.ready).toBe(1);
  expect(before.providers.every((provider: { available: boolean }) => !provider.available)).toBe(true);
  expect((await page.request.post(`http://127.0.0.1:8004/__test__/sync/fail-coverage/${accepted.id}`)).status()).toBe(200);
  await signIn(page, 'sync-failure');
  await page.getByRole('button', { name: 'Sources & sync', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Sources & sync', exact: true })).toBeVisible();
  await expect(page.getByRole('region', { name: 'File import status' })).toContainText('1 activity');
  await expect(page.getByRole('button', { name: 'Retry coverage', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: /^(Connect|Reconnect) (Garmin|Strava)$/ })).toHaveCount(0);
  await expect(page.getByRole('region', { name: 'Cloud connections' })).toContainText('Unavailable');
  await context.setOffline(true);
  await expect(page.getByRole('button', { name: 'Retry coverage', exact: true })).toBeDisabled();
  await expect(page.getByRole('region', { name: 'File import status' })).toContainText('last loaded');
  await context.setOffline(false);
  await expect(page.getByRole('button', { name: 'Retry coverage', exact: true })).toBeEnabled();
  const retried = page.waitForResponse((response) => response.url().endsWith(`/api/uploads/${accepted.id}/retry-coverage`));
  await page.getByRole('button', { name: 'Retry coverage', exact: true }).click();
  expect((await retried).status()).toBe(204);
  await expect(page.getByRole('button', { name: 'Retry coverage', exact: true })).toHaveCount(0, { timeout: 15_000 });
  const after = await (await page.request.get('/api/sync/status', { headers: headers('sync-failure') })).json();
  expect(after.coverage.ready).toBe(1);
  expect(after.last_import_at).toBe(before.last_import_at);
  expect(after.files.gpx.imported).toBe(1);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: 'test-results/web-sources.png', fullPage: true });
});

test('hidden polling pauses and a late status response cannot repopulate a replacement account', async ({ page }) => {
  const source = await upload(page, 'sync-race');
  await ready(page, 'sync-race', source.id);
  await signIn(page, 'sync-race');
  await page.getByRole('button', { name: 'Sources & sync', exact: true }).click();
  await expect(page.getByRole('region', { name: 'File import status' })).toContainText('1 activity');
  await page.evaluate(() => { Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' }); document.dispatchEvent(new Event('visibilitychange')); });
  let starts = 0;
  page.on('request', (request) => { if (new URL(request.url()).pathname === '/api/sync/status') starts++; });
  await page.waitForTimeout(3000);
  expect(starts).toBe(0);
  let held = false; let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  await page.route('**/api/sync/status*', async (route) => {
    if (route.request().headers().authorization !== 'Bearer web-synthetic-sync-race') return route.continue();
    const response = await route.fetch(); held = true; await gate; await route.fulfill({ response });
  });
  await page.evaluate(() => { Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' }); document.dispatchEvent(new Event('visibilitychange')); });
  await expect.poll(() => held).toBe(true);
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Start exploring' })).toBeVisible();
  await page.evaluate(() => sessionStorage.setItem('city-runner.session', 'web-synthetic-view-other'));
  release();
  await page.reload();
  await page.getByRole('button', { name: 'Sources & sync', exact: true }).click();
  await expect(page.getByRole('region', { name: 'File import status' })).toContainText('0 activities');
  await expect(page.getByRole('button', { name: 'Retry coverage', exact: true })).toHaveCount(0);
  expect(await page.evaluate(() => sessionStorage.getItem('city-runner.session'))).toBe('web-synthetic-view-other');
});
