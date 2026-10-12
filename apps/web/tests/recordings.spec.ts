import { test, expect, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';

const headers = { Authorization: 'Bearer web-synthetic-recordings' };
async function importRecordings(page: Page, formats: Array<'gpx' | 'fit'>, requestHeaders = headers) {
  const response = await page.request.post('/api/import-batches', { headers: requestHeaders, data: {
    request_id: randomUUID(), files: formats.map((format) => ({ name: `equivalent.${format}`, format })),
  } });
  expect(response.status()).toBe(201);
  const batch = await response.json();
  for (let index = 0; index < formats.length; index++) {
    const format = formats[index];
    const uploaded = await page.request.post(`/api/import-batches/${batch.id}/items/${batch.items[index].id}/upload`, {
      headers: requestHeaders, multipart: { file: { name: `equivalent.${format}`, mimeType: 'application/octet-stream',
        buffer: readFileSync(`tests/.fixtures/equivalent.${format}`) } },
    });
    expect(uploaded.status()).toBe(202);
    await expect.poll(async () => {
      const detail = await (await page.request.get(`/api/import-batches/${batch.id}`, { headers: requestHeaders })).json();
      return detail.items[index].status;
    }).toBe('succeeded');
  }
  const detail = await (await page.request.get(`/api/import-batches/${batch.id}`, { headers: requestHeaders })).json();
  const activityId = detail.items[0].activity_id;
  await expect.poll(async () => (await (await page.request.get(`/api/activities/${activityId}`, { headers: requestHeaders })).json()).processed).toBe(true);
  return { batch: detail, activityId };
}
async function visited(page: Page) {
  const response = await (await page.request.get('/api/progress', { headers })).json();
  return response.datasets[0]?.visited_node_count;
}

test('equivalent GPX/FIT retain separate imports and source removal preserves the workout and coverage', async ({ page }) => {
  await page.goto('/');
  const first = await importRecordings(page, ['gpx', 'fit']);
  expect(first.batch.items[0].activity_id).toBe(first.batch.items[1].activity_id);
  expect(first.batch.items.every((item: { duplicate: boolean }) => !item.duplicate)).toBe(true);
  expect((await (await page.request.get('/api/activities', { headers })).json()).total).toBe(1);
  await expect.poll(() => visited(page)).toBe(4);
  const before = await (await page.request.get(`/api/activities/${first.activityId}`, { headers })).json();
  expect(before.tracks).toHaveLength(2);
  expect((await page.request.get(`/api/activities/${first.activityId}/sources`, {
    headers: { Authorization: 'Bearer web-synthetic-view-other' },
  })).status()).toBe(404);
  await page.evaluate(() => sessionStorage.setItem('city-runner.session', 'web-synthetic-recordings'));
  await page.reload();
  await page.getByRole('button', { name: 'Activities', exact: true }).click();
  await expect(page.locator('.activity-row')).toHaveCount(1);
  await page.locator('.activity-row').click();
  const sources = page.getByRole('region', { name: 'Imported files' });
  await expect(sources).toContainText('2 imported files');
  await expect(sources).toContainText('GPX'); await expect(sources).toContainText('FIT');
  await sources.getByRole('button', { name: /Remove FIT import/ }).click();
  const remove = page.getByRole('dialog', { name: 'Remove this import?' });
  await expect(remove).toContainText('Other imports keep this activity');
  await remove.getByRole('button', { name: 'Keep import', exact: true }).click();
  await expect(sources).toContainText('2 imported files');
  await sources.getByRole('button', { name: /Remove FIT import/ }).click();
  await remove.getByRole('button', { name: 'Remove import', exact: true }).click();
  await expect(sources).toContainText('1 imported file');
  await expect(page.locator('.activity-row')).toHaveCount(1);
  const after = await (await page.request.get(`/api/activities/${first.activityId}`, { headers })).json();
  expect(after.tracks).toEqual(before.tracks); expect(after.timestamps).toEqual(before.timestamps);
  await expect.poll(() => visited(page)).toBe(4);
  const remaining = await (await page.request.get(`/api/import-batches/${first.batch.id}`, { headers })).json();
  expect(remaining.items.map((item: { status: string }) => item.status)).toEqual(['succeeded', 'deleted']);
  const reattached = await importRecordings(page, ['fit']);
  expect(reattached.activityId).toBe(first.activityId);
  await page.getByRole('button', { name: 'Refresh your data' }).click();
  await expect(sources).toContainText('2 imported files');
  await page.getByRole('button', { name: 'Delete activity', exact: true }).click();
  const whole = page.getByRole('dialog', { name: 'Delete this activity?' });
  await expect(whole).toContainText('all its imported files');
  await whole.getByRole('button', { name: 'Delete activity', exact: true }).click();
  await expect(page.locator('.activity-row')).toHaveCount(0);
  await expect(sources).toHaveCount(0);
  await expect.poll(() => visited(page)).toBe(0);
  const deleted = await (await page.request.get(`/api/import-batches/${first.batch.id}`, { headers })).json();
  expect(deleted.counts.deleted).toBe(2);
  const fresh = await importRecordings(page, ['gpx', 'fit']);
  expect(fresh.activityId).not.toBe(first.activityId);
  expect(fresh.batch.items[1].activity_id).toBe(fresh.activityId);
  expect((await (await page.request.get(`/api/import-batches/${first.batch.id}`, { headers })).json()).counts.deleted).toBe(2);
  await expect.poll(() => visited(page)).toBe(4);
  await page.reload();
  await page.getByRole('button', { name: 'Activities', exact: true }).click();
  await page.locator('.activity-row').click();
  await expect(sources).toContainText('2 imported files');
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: 'test-results/web-recording-sources.png', fullPage: true });
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await expect(sources).toHaveCount(0);
});

test('a source removed by another client invalidates its open removal confirmation', async ({ page }) => {
  const owner = { Authorization: 'Bearer web-synthetic-recording-race' };
  await page.goto('/');
  const imported = await importRecordings(page, ['gpx', 'fit'], owner);
  await page.evaluate(() => sessionStorage.setItem('city-runner.session', 'web-synthetic-recording-race'));
  await page.reload();
  await page.getByRole('button', { name: 'Activities', exact: true }).click();
  await page.locator('.activity-row').click();
  const sources = page.getByRole('region', { name: 'Imported files' });
  await expect(sources).toContainText('2 imported files');
  await sources.getByRole('button', { name: /Remove FIT import/ }).click();
  const confirmation = page.getByRole('dialog', { name: 'Remove this import?' });
  await expect(confirmation).toBeVisible();
  const fit = imported.batch.items[1].source_id;
  expect((await page.request.delete(`/api/activities/${imported.activityId}/sources/${fit}`, { headers: owner })).status()).toBe(204);
  await expect(confirmation).toHaveCount(0, { timeout: 15_000 });
  await expect(sources).toContainText('1 imported file');
  await expect(page.locator('.activity-row')).toHaveCount(1);
});
