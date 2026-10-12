import { test, expect, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';

const headers = (owner: string) => ({ Authorization: `Bearer web-synthetic-${owner}` });
const gpx = (name: string, timed = true) => Buffer.from(`<gpx version="1.1"><trk><name>${name}</name><type>running</type>
  <trkseg><trkpt lon="3.716" lat="51.044">${timed ? '<time>2026-10-07T08:00:00Z</time>' : ''}</trkpt><trkpt lon="3.717" lat="51.044">${timed ? '<time>2026-10-07T08:01:00Z</time>' : ''}</trkpt></trkseg>
  <trkseg><trkpt lon="3.730" lat="51.056">${timed ? '<time>2026-10-07T08:10:00Z</time>' : ''}</trkpt><trkpt lon="3.731" lat="51.056">${timed ? '<time>2026-10-07T08:11:00Z</time>' : ''}</trkpt></trkseg>
</trk></gpx>`);
async function upload(page: Page, owner: string, name: string, buffer: Buffer) {
  const response = await page.request.post('/api/uploads', { headers: headers(owner), multipart: {
    file: { name, mimeType: name.endsWith('.fit') ? 'application/octet-stream' : 'application/gpx+xml', buffer },
  } });
  expect(response.status()).toBe(202);
  const source = await response.json();
  await expect.poll(async () => (await (await page.request.get(`/api/uploads/${source.id}`, { headers: headers(owner) })).json()).status).toBe('succeeded');
  const status = await (await page.request.get(`/api/uploads/${source.id}`, { headers: headers(owner) })).json();
  return { source, activity: await (await page.request.get(`/api/activities/${status.activity_id}`, { headers: headers(owner) })).json() };
}
async function signIn(page: Page, owner: string) {
  await page.goto('/');
  await page.evaluate((owner) => sessionStorage.setItem('city-runner.session', `web-synthetic-${owner}`), owner);
  await page.reload();
  await page.getByRole('button', { name: 'Activities', exact: true }).click();
}

test('GPX and paused FIT expose matching recorded statistics with honest unknown timing', async ({ page }) => {
  const recorded = await upload(page, 'stats', 'statistics.gpx', gpx('Segment statistics'));
  const fit = await upload(page, 'stats', 'statistics.fit', readFileSync('tests/.fixtures/statistics.fit'));
  const untimed = await upload(page, 'stats', 'untimed.gpx', gpx('Untimed statistics', false));
  expect(recorded.activity.duration_s).toBe(120);
  expect(fit.activity.duration_s).toBe(120);
  expect(recorded.activity.distance_m).toBeGreaterThan(130);
  expect(recorded.activity.distance_m).toBeLessThan(150);
  expect(fit.activity.distance_m).toBeCloseTo(recorded.activity.distance_m, 1);
  expect(fit.activity.pace_s_per_km).toBeCloseTo(recorded.activity.pace_s_per_km, 0);
  expect(untimed.activity.distance_m).toBeCloseTo(recorded.activity.distance_m, 5);
  expect(untimed.activity.duration_s).toBeNull(); expect(untimed.activity.pace_s_per_km).toBeNull();
  const duplicate = await upload(page, 'stats', 'statistics.gpx', gpx('Segment statistics'));
  expect(duplicate.source.duplicate).toBe(true); expect(duplicate.activity.id).toBe(recorded.activity.id);
  expect((await page.request.get(`/api/activities/${recorded.activity.id}`, { headers: headers('view-other') })).status()).toBe(404);
  await signIn(page, 'stats');
  await expect(page.locator('.activity-row')).toHaveCount(3);
  const row = page.locator('.activity-row').filter({ hasText: 'Segment statistics' });
  await expect(row).toContainText('0.14 km'); await expect(row).toContainText('2:00');
  await row.click();
  const metrics = page.getByRole('region', { name: 'Recorded activity statistics' });
  await expect(metrics).toContainText('GPS distance'); await expect(metrics).toContainText('Recorded time');
  await expect(metrics).toContainText('2:00'); await expect(metrics).toContainText('gaps');
  await page.locator('.activity-row').filter({ hasText: 'Untimed statistics' }).click();
  await expect(metrics.locator('[data-stat="distance"]')).toContainText('0.14 km');
  await expect(metrics.locator('[data-stat="time"]')).toContainText('Unknown');
  await expect(metrics.locator('[data-stat="pace"]')).toContainText('Unknown');
  const controls = page.getByRole('region', { name: 'Activity and map filters' });
  await controls.locator('summary').click(); await controls.getByLabel('File source').selectOption('fit');
  await controls.getByRole('button', { name: 'Apply filters' }).click();
  await expect(page.locator('.activity-row')).toHaveCount(1);
  await expect(page.locator('.activity-row')).toContainText('2:00');
  await page.locator('.activity-row').click(); await expect(metrics).toContainText('2:00');
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: 'test-results/web-statistics.png', fullPage: true });
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await expect(metrics).toHaveCount(0);
});

test('real legacy backfill refreshes selected statistics without reimport or changing filters', async ({ page }) => {
  const imported = await upload(page, 'stats-backfill', 'legacy.gpx', gpx('Legacy statistics'));
  const legacy = await page.request.post(`http://127.0.0.1:8004/__test__/statistics/legacy/${imported.activity.id}`);
  expect(legacy.status()).toBe(200);
  await signIn(page, 'stats-backfill');
  await page.locator('.activity-row').filter({ hasText: 'Legacy statistics' }).click();
  const metrics = page.getByRole('region', { name: 'Recorded activity statistics' });
  await expect(metrics.locator('[data-stat="distance"]')).toContainText('Unknown');
  const controls = page.getByRole('region', { name: 'Activity and map filters' });
  await controls.locator('summary').click(); await controls.getByLabel('File source').selectOption('gpx');
  await controls.getByRole('button', { name: 'Apply filters' }).click();
  await page.locator('.activity-row').filter({ hasText: 'Legacy statistics' }).click();
  expect((await page.request.post('http://127.0.0.1:8004/__test__/statistics/backfill')).status()).toBe(200);
  await expect(metrics.locator('[data-stat="distance"]')).toContainText('0.14 km', { timeout: 15_000 });
  await expect(metrics.locator('[data-stat="time"]')).toContainText('2:00');
  await expect(controls.locator('.filter-summary')).toContainText('GPX');
  await expect(page.locator('.activity-row')).toHaveCount(1);
  const repeated = await page.request.post('http://127.0.0.1:8004/__test__/statistics/backfill');
  expect((await repeated.json()).processed).toBe(0);
  expect((await (await page.request.get('/api/activities', { headers: headers('stats-backfill') })).json()).total).toBe(1);
});
