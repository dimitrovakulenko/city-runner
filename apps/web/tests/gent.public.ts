import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import type { ActivityDetail, MapResponse, ProgressResponse, ImportBatchResponse } from '../../mobile/src/api/generated';

type Route = { kind: string; filename: string; name: string; street_id: string; street_name: string; city_id: string; eligible_nodes: number; segments: Array<Array<[number, number]>> };
type Snapshot = { dataset_id: string; checksum: string; totals: { streets: number; nodes: number }; routes: Route[] };
const escape = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

test('public Gent streets: browser GPX and FIT import, original samples, real street coverage and empty default account', async ({ page }) => {
  const snapshot: Snapshot = JSON.parse(readFileSync(new URL('./.fixtures/gent.json', import.meta.url), 'utf8'));
  const alice = { Authorization: 'Bearer web-synthetic-alice' }; const bob = { Authorization: 'Bearer web-synthetic-bob' };
  const progress = async (headers = bob): Promise<ProgressResponse> => {
    const response = await page.request.get('/api/progress', { headers }); expect(response.ok()).toBe(true); return response.json();
  };
  const before = (await progress(alice)).datasets.find((dataset) => dataset.dataset_id === snapshot.dataset_id)!;
  expect(before.eligible_streets).toBe(3108); expect(before.eligible_nodes).toBe(51134);
  expect(before.completed_streets).toBe(0); expect(before.visited_node_count).toBe(0);
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Your city. A new perspective.' })).toBeVisible();
  await page.evaluate(() => sessionStorage.setItem('city-runner.session', 'web-synthetic-bob')); await page.reload();
  await expect(page.getByRole('button', { name: 'Sign out', exact: true })).toBeVisible();
  await expect(page.locator('.map-area')).toHaveAttribute('data-ready', 'true', { timeout: 20_000 });
  await page.getByRole('button', { name: 'Imports', exact: true }).click();
  const created = page.waitForResponse((response) => response.url().endsWith('/api/import-batches') && response.request().method() === 'POST' && response.ok());
  const chooser = page.waitForEvent('filechooser'); await page.getByRole('button', { name: 'Choose activity files' }).click();
  await (await chooser).setFiles(snapshot.routes.map((route) => `tests/.fixtures/${route.filename}`));
  const manifest: ImportBatchResponse = await (await created).json();
  await expect(page.getByText('2 imported', { exact: true })).toBeVisible({ timeout: 30_000 });
  await expect.poll(async () => (await progress()).datasets[0].completed_streets, { timeout: 20_000 }).toBeGreaterThanOrEqual(2);
  const after = (await progress()).datasets[0]; expect(after.state).toBe('ready'); expect(after.visited_node_count).toBeGreaterThan(0);
  const batch: ImportBatchResponse = await (await page.request.get(`/api/import-batches/${manifest.id}`, { headers: bob })).json();
  const imported = batch.items;
  try {
    for (const route of snapshot.routes) {
      const item = imported.find((item) => item.name === route.filename)!; expect(item.status).toBe('succeeded'); expect(item.activity_id).not.toBeNull();
      const detail: ActivityDetail = await (await page.request.get(`/api/activities/${item.activity_id}`, { headers: bob })).json();
      expect(detail.processed).toBe(true); expect(detail.date).toBe('2026-10-06'); expect(detail.unmapped_points).toBe(0);
      expect(detail.tracks.map((segment) => segment.length)).toEqual(route.segments.map((segment) => segment.length));
      let record = 0;
      detail.tracks.forEach((segment, index) => segment.forEach(([lon, lat], offset) => {
        expect(lon).toBeCloseTo(route.segments[index][offset][0], 6); expect(lat).toBeCloseTo(route.segments[index][offset][1], 6);
        expect(detail.timestamps[index][offset]).toBe(new Date(Date.UTC(2026, 9, 6, 8, 0, record++ * 5)).toISOString().replace('.000Z', 'Z'));
      }));
      await page.getByRole('button', { name: 'Cities & streets', exact: true }).click();
      await page.getByLabel('Region', { exact: true }).selectOption({ label: 'Gent' });
      await page.getByRole('button', { name: /^Gent / }).click();
      await page.getByLabel('Search streets').fill(route.street_name);
      await page.getByRole('button', { name: new RegExp(`^${escape(route.street_name)} `) }).click();
      await expect(page.getByText('GPS nodes visited', { exact: false })).toContainText(`${route.eligible_nodes} / ${route.eligible_nodes}`);
      await expect(page.getByText('0 original GPS nodes remain.', { exact: true })).toBeVisible();
      const renderedMap = page.waitForResponse(async (response) => {
        if (!response.url().includes('/api/map?') || !response.ok()) return false;
        const map: MapResponse = await response.json();
        return map.tracks.some((track) => track.activity_id === item.activity_id) && map.streets.some((street) => street.street_id === route.street_id && street.completed === true);
      }, { timeout: 20_000 });
      await page.getByRole('button', { name: new RegExp(`^${escape(route.name)} `) }).click();
      await expect(page.getByRole('heading', { name: route.name, exact: true })).toBeVisible();
      await renderedMap;
      await expect(page.getByText('Coverage processed.', { exact: false })).toBeVisible();
      await page.screenshot({ path: `test-results/public-gent-${route.kind}.png`, fullPage: true });
      await page.getByRole('button', { name: 'Cities & streets', exact: true }).click();
      await page.getByRole('button', { name: 'Back to streets' }).click();
      await page.getByRole('button', { name: 'Choose another city' }).click();
    }
    await page.reload(); await page.getByRole('button', { name: 'Imports', exact: true }).click();
    await expect(page.getByText('2 imported', { exact: true })).toBeVisible();
    expect((await progress(alice)).datasets[0].visited_node_count).toBe(0);
    const defaultActivities = await (await page.request.get('/api/activities', { headers: alice })).json(); expect(defaultActivities.total).toBe(0);
  } finally {
    for (const item of imported) if (item.activity_id) {
      const deleted = await page.request.delete(`/api/activities/${item.activity_id}`, { headers: bob }); expect(deleted.ok()).toBe(true);
    }
  }
  await expect.poll(async () => (await progress()).datasets[0].visited_node_count).toBe(0);
});
