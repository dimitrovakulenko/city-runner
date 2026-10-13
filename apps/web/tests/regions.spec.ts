import { test, expect } from '@playwright/test';
import type { DatasetPage, MapResponse } from '../../mobile/src/api/generated';

const headers = { Authorization: 'Bearer web-synthetic-regions' };
async function signIn(page: import('@playwright/test').Page, token: string) {
  await page.goto('/');
  await page.evaluate((value) => sessionStorage.setItem('city-runner.session', value), token);
  await page.reload();
  await expect(page.getByRole('button', { name: 'Sign out', exact: true })).toBeVisible();
  await expect(page.locator('.map-area')).toHaveAttribute('data-ready', 'true');
}

test('a newly imported region matches existing history and paged discovery focuses cities beyond twenty regions', async ({ page }) => {
  test.setTimeout(120_000);
  const points = Array.from({ length: 5 }, (_, index) => `<trkpt lat="51.044" lon="${4.116 + index * .004}"><time>2026-10-13T07:00:0${index}Z</time></trkpt>`).join('');
  const bytes = `<gpx version="1.1"><trk><name>Before the region existed</name><type>running</type><trkseg>${points}</trkseg></trk></gpx>`;
  const uploaded = await page.request.post('/api/uploads', { headers, multipart: { file: { name: 'before-region.gpx', mimeType: 'application/gpx+xml', buffer: Buffer.from(bytes) } } });
  expect(uploaded.status()).toBe(202);
  const upload = await uploaded.json();
  await expect.poll(async () => (await (await page.request.get(`/api/uploads/${upload.id}`, { headers })).json()).status).toBe('succeeded');
  await expect.poll(async () => (await (await page.request.get('/api/progress', { headers })).json()).unmapped_points).toBe(5);
  const status = await (await page.request.get(`/api/uploads/${upload.id}`, { headers })).json();
  const original = await (await page.request.get(`/api/activities/${status.activity_id}`, { headers })).json();

  const created = await page.request.post('http://127.0.0.1:8004/__test__/regions/create');
  expect(created.ok()).toBe(true);
  const task = await created.json();
  await expect.poll(async () => (await (await page.request.get(`http://127.0.0.1:8004/__test__/regions/tasks/${task.id}`)).json()).status, { timeout: 30_000 }).toBe('succeeded');
  await expect.poll(async () => {
    const response: DatasetPage = await (await page.request.get('/api/datasets?q=Z%20Added', { headers })).json();
    return response.items[0]?.state;
  }, { timeout: 30_000 }).toBe('ready');
  const after = await (await page.request.get(`/api/activities/${status.activity_id}`, { headers })).json();
  expect(after.tracks).toEqual(original.tracks); expect(after.timestamps).toEqual(original.timestamps);
  const progress = await (await page.request.get('/api/progress', { headers })).json();
  expect(progress.unmapped_points).toBe(0);
  expect(progress.datasets.find((item: { region: string }) => item.region === 'Demo Region').visited_node_count).toBe(0);
  const demo = progress.datasets.find((item: { region: string }) => item.region === 'Demo Region');
  const added = progress.datasets.find((item: { region: string }) => item.region === 'Z Added Region');
  expect(added.visited_node_count).toBe(5); expect(added.completed_streets).toBe(1);
  const other = await (await page.request.get(`/api/datasets/${added.dataset_id}`, { headers: { Authorization: 'Bearer web-synthetic-region-replacement' } })).json();
  expect(other.visited_node_count).toBe(0);

  expect((await page.request.post('http://127.0.0.1:8004/__test__/regions/catalog')).ok()).toBe(true);
  let view: MapResponse | null = null;
  page.on('response', async (response) => { if (response.ok() && response.url().includes('/api/map?')) { try { view = await response.json(); } catch { /* Superseded response body. */ } } });
  await signIn(page, 'web-synthetic-regions');
  await page.getByRole('button', { name: 'Cities & streets', exact: true }).click();
  await page.getByRole('button', { name: 'Load more regions', exact: true }).click();
  const catalog: DatasetPage = await (await page.request.get('/api/datasets?q=ZZ%20Catalog%2020', { headers })).json();
  await page.getByLabel('Region', { exact: true }).selectOption(catalog.items[0].dataset_id);
  await expect(page.getByRole('button', { name: /Catalog City/ })).toBeVisible();
  await page.getByRole('button', { name: /Catalog City/ }).click();
  await expect(page.getByRole('heading', { name: 'Catalog City', exact: true })).toBeVisible();
  await expect.poll(() => view && (view.bbox[0] + view.bbox[2]) / 2, { timeout: 15_000 }).toBeGreaterThan(3.80);
  const selected = await page.getByLabel('Region', { exact: true }).inputValue();
  await page.getByRole('button', { name: 'Refresh your data', exact: true }).click();
  await expect(page.getByLabel('Region', { exact: true })).toHaveValue(selected);
  await page.getByLabel('Search regions', { exact: true }).fill('Z Added');
  await expect(page.getByLabel('Region', { exact: true }).locator('option')).toHaveCount(3);
  await expect(page.getByLabel('Region', { exact: true })).toHaveValue(selected);
  await page.getByLabel('Region', { exact: true }).selectOption({ label: 'Z Added Region' });
  await page.getByRole('button', { name: /Additional City/ }).click();
  await expect(page.getByRole('heading', { name: 'Additional City', exact: true })).toBeVisible();
  await expect.poll(() => view && (view.bbox[0] + view.bbox[2]) / 2).toBeGreaterThan(4.1);
  await page.getByLabel('Search regions', { exact: true }).fill('Demo');
  await page.getByLabel('Region', { exact: true }).selectOption(demo.dataset_id);
  await page.getByRole('button', { name: /Demo City/ }).click();
  await expect.poll(() => view && (view.bbox[0] + view.bbox[2]) / 2).toBeLessThan(3.8);
  await page.getByLabel('Search regions', { exact: true }).fill('Z Added');
  await page.getByLabel('Region', { exact: true }).selectOption({ label: 'Z Added Region' });
  await expect.poll(() => view && (view.bbox[0] + view.bbox[2]) / 2).toBeGreaterThan(4.1);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: 'test-results/web-regions.png', fullPage: true });
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  expect((await page.request.post('http://127.0.0.1:8004/__test__/regions/cleanup')).ok()).toBe(true);
});

test('catalog retry preserves selection and late catalog responses cannot restore a signed-out account', async ({ page }) => {
  await signIn(page, 'web-synthetic-region-race');
  await page.getByRole('button', { name: 'Cities & streets', exact: true }).click();
  await page.getByLabel('Region', { exact: true }).selectOption({ label: 'Demo Region' });
  await expect(page.getByRole('button', { name: /Demo City/ })).toBeVisible();
  const selected = await page.getByLabel('Region', { exact: true }).inputValue();
  let fail = true;
  await page.route('**/api/datasets?**', async (route) => {
    if (fail) { fail = false; await route.fulfill({ status: 503, json: { detail: 'Temporary region outage' } }); }
    else await route.continue();
  });
  await page.getByRole('button', { name: 'Refresh your data', exact: true }).click();
  await expect(page.getByRole('alert').filter({ has: page.getByRole('button', { name: 'Retry', exact: true }) })).toBeVisible();
  await page.getByRole('alert').filter({ has: page.getByRole('button', { name: 'Retry', exact: true }) }).getByRole('button', { name: 'Retry', exact: true }).click();
  await expect(page.getByRole('alert').filter({ has: page.getByRole('button', { name: 'Retry', exact: true }) })).toHaveCount(0);
  await expect(page.getByLabel('Region', { exact: true })).toHaveValue(selected);
  await page.unroute('**/api/datasets?**');
  let release!: () => void; const gate = new Promise<void>((resolve) => { release = resolve; }); let held = false;
  await page.route('**/api/datasets?**', async (route) => { const response = await route.fetch(); held = true; await gate; try { await route.fulfill({ response }); } catch { /* Request aborted by logout. */ } });
  await page.getByRole('button', { name: 'Refresh your data', exact: true }).click();
  await expect.poll(() => held).toBe(true);
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  release();
  await expect(page.getByLabel('Region', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: /Demo City/ })).toHaveCount(0);
  await page.unroute('**/api/datasets?**');
  await signIn(page, 'web-synthetic-region-replacement');
  await expect(page.locator('.progress-foot')).toContainText('0 /');
});
