import { test, expect, type Page } from '@playwright/test';
import { BlobReader, BlobWriter, ZipWriter } from '@zip.js/zip.js';
import { readFileSync, mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const token = (owner: string) => `web-synthetic-${owner}`;
const headers = (owner: string) => ({ Authorization: `Bearer ${token(owner)}` });
const gpx = (index: number) => `<gpx><trk><name>History run ${index}</name><type>running</type><trkseg><trkpt lon="3.72" lat="51.05"><time>${new Date(Date.UTC(2026, 9, 13, 8, index * 2)).toISOString()}</time></trkpt><trkpt lon="3.7201" lat="51.05"><time>${new Date(Date.UTC(2026, 9, 13, 8, index * 2, 30)).toISOString()}</time></trkpt></trkseg></trk></gpx>`;
async function signIn(page: Page, owner: string) {
  await page.goto('/'); await page.evaluate((value) => sessionStorage.setItem('city-runner.session', value), token(owner));
  await page.reload(); await page.getByRole('button', { name: 'Imports', exact: true }).click();
}
async function makeZip() {
  const writer = new ZipWriter(new BlobWriter(), { useWebWorkers: false });
  for (let index = 0; index < 42; index++) await writer.add(`activities/run-${String(index).padStart(3, '0')}.gpx`, new BlobReader(new Blob([index === 41 ? '<gpx><broken>' : gpx(index)])), { level: 6 });
  await writer.add('activities/Activity.FIT', new BlobReader(new Blob([new Uint8Array(readFileSync('tests/.fixtures/equivalent.fit'))])), { level: 6 });
  for (let copy = 1; copy <= 2; copy++) await writer.add(`copy-${copy}/run-005.gpx`, new BlobReader(new Blob([gpx(5)])), { level: 6 });
  await writer.add('profile.json', new BlobReader(new Blob(['{"synthetic":true}'])));
  return Buffer.from(await (await writer.close()).arrayBuffer());
}
async function chooseZip(page: Page, button: string, buffer: Buffer) {
  const chooser = page.waitForEvent('filechooser');
  await page.getByRole('button', { name: button, exact: true }).click();
  await (await chooser).setFiles({ name: 'synthetic-history.zip', mimeType: 'application/zip', buffer });
}
async function history(page: Page, owner: string) {
  return (await (await page.request.get('/api/import-histories', { headers: headers(owner) })).json()).items[0];
}

test('ZIP history crosses batch boundaries, stops/reloads/reselects, and never reimports deleted items', async ({ page }) => {
  test.setTimeout(90_000);
  const owner = 'history-zip', archive = await makeZip();
  await signIn(page, owner);
  let release!: () => void; const gate = new Promise<void>((resolve) => { release = resolve; });
  let accepted = 0; let held = false;
  let resumeRelease!: () => void; const resumedGate = new Promise<void>((resolve) => { resumeRelease = resolve; });
  let resumedHeld = false;
  await page.route('**/api/import-batches/*/items/*/upload', async (route) => {
    const response = await route.fetch();
    if (response.status() === 202 && ++accepted === 20) { held = true; await gate; }
    if (response.status() === 202 && accepted === 21) { resumedHeld = true; await resumedGate; }
    await route.fulfill({ response });
  });
  await chooseZip(page, 'Import ZIP', archive);
  const card = page.getByRole('article', { name: 'History import synthetic-history.zip' });
  await expect(card).toContainText('45 files');
  await expect.poll(() => held, { timeout: 30_000 }).toBe(true);
  await card.getByRole('button', { name: 'Stop uploading', exact: true }).click();
  await expect(card).toContainText('Uploading paused'); release();
  await expect.poll(async () => (await history(page, owner)).counts.awaiting_upload).toBe(25);
  await expect.poll(async () => (await history(page, owner)).counts.succeeded).toBe(20);
  const before = await history(page, owner);
  expect(before.batch_ids).toHaveLength(3);
  expect((await page.request.get(`/api/import-histories/${before.id}`, { headers: headers('view-other') })).status()).toBe(404);
  const batches = await (await page.request.get(`/api/import-histories/${before.id}/batches`, { headers: headers(owner) })).json();
  expect(batches.items.map((batch: { items: unknown[] }) => batch.items.length)).toEqual([20, 20, 5]);
  const first = batches.items[0].items[0];
  const original = await (await page.request.get(`/api/activities/${first.activity_id}`, { headers: headers(owner) })).json();
  expect(original.tracks).toEqual([[[3.72, 51.05], [3.7201, 51.05]]]);
  expect(original.timestamps).toEqual([['2026-10-13T08:00:00.000Z', '2026-10-13T08:00:30.000Z']]);
  expect((await page.request.delete(`/api/activities/${first.activity_id}`, { headers: headers(owner) })).status()).toBe(204);
  await page.reload(); await page.getByRole('button', { name: 'Imports', exact: true }).click();
  await expect(card).toContainText('1 deleted');
  await expect(card).toContainText('25 waiting');
  await expect(card).toContainText('Uploading paused');
  await chooseZip(page, 'Select ZIP again', archive);
  await expect(card.getByRole('button', { name: 'Select ZIP again' })).toBeEnabled();
  expect((await history(page, owner)).counts.awaiting_upload).toBe(25);
  await card.getByRole('button', { name: 'Resume uploading', exact: true }).click();
  await expect.poll(() => resumedHeld).toBe(true);
  await expect(card.getByRole('button', { name: 'Stop uploading', exact: true })).toBeEnabled();
  await card.getByRole('button', { name: 'Stop uploading', exact: true }).click();
  await expect(card).toContainText('Uploading paused'); resumeRelease();
  await expect.poll(async () => (await history(page, owner)).counts.awaiting_upload).toBe(24);
  await expect(card.getByRole('button', { name: 'Resume uploading', exact: true })).toBeEnabled();
  await card.getByRole('button', { name: 'Resume uploading', exact: true }).click();
  await expect.poll(async () => (await history(page, owner)).counts.awaiting_upload, { timeout: 30_000 }).toBe(0);
  await expect.poll(async () => (await history(page, owner)).counts.failed, { timeout: 30_000 }).toBe(1);
  await expect.poll(async () => (await history(page, owner)).counts.succeeded).toBe(43);
  const after = await history(page, owner); expect(after.duplicate_count).toBe(2); expect(after.counts.deleted).toBe(1);
  expect((await (await page.request.get('/api/activities', { headers: headers(owner) })).json()).total).toBe(41);
  await card.getByRole('button', { name: 'Show files', exact: true }).click();
  await expect(card).toContainText('Files 1–20 of 45');
  await card.getByRole('button', { name: 'Next files' }).click(); await card.getByRole('button', { name: 'Next files' }).click();
  await expect(card).toContainText('Activity.FIT'); await expect(card).toContainText('Choose a corrected file');
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: 'test-results/web-history-import.png', fullPage: true });
});

test('folder selection imports more than20files without paths and rejects bad archives without manifests', async ({ page }) => {
  const owner = 'history-folder'; await signIn(page, owner);
  await chooseZip(page, 'Import ZIP', Buffer.from('not a zip'));
  await expect(page.getByRole('alert').filter({ hasText: 'corrupt' })).toBeVisible();
  expect((await (await page.request.get('/api/import-histories', { headers: headers(owner) })).json()).total).toBe(0);
  const directory = mkdtempSync(join(tmpdir(), 'city-runner-history-folder-'));
  try {
    mkdirSync(join(directory, 'activities'));
    for (let index = 0; index < 23; index++) writeFileSync(join(directory, 'activities', `run-${index}.gpx`), gpx(index + 100));
    writeFileSync(join(directory, 'private-profile.json'), '{}');
    writeFileSync(join(directory, 'backup.zip'), 'unrelated nested ZIP');
    let failed = false;
    await page.route('**/api/import-batches/*/items/*/upload', async (route) => {
      if (!failed) { failed = true; return route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"Synthetic upload unavailable"}' }); }
      await route.continue();
    });
    const chooser = page.waitForEvent('filechooser'); await page.getByRole('button', { name: 'Choose folder', exact: true }).click(); await (await chooser).setFiles(directory);
    const card = page.getByRole('article').filter({ has: page.getByRole('button', { name: 'Retry uploading', exact: true }) });
    await expect(card.getByRole('button', { name: 'Retry uploading', exact: true })).toBeVisible();
    expect((await history(page, owner)).counts.awaiting_upload).toBe(23);
    await card.getByRole('button', { name: 'Retry uploading', exact: true }).click();
    await expect.poll(async () => (await history(page, owner))?.counts.awaiting_upload, { timeout: 30_000 }).toBe(0);
    const imported = await history(page, owner); expect(imported.file_count).toBe(23); expect(imported.batch_ids).toHaveLength(2);
    const children = await (await page.request.get(`/api/import-histories/${imported.id}/batches`, { headers: headers(owner) })).json();
    expect(children.items.flatMap((batch: { items: Array<{ name: string }> }) => batch.items).every((item: { name: string }) => !item.name.includes('/') && !item.name.includes('\\'))).toBe(true);
    await expect(page.getByText('Skipped 2 non-activity files.', { exact: true })).toBeVisible();
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('a late committed history response cannot upload or populate a replacement account', async ({ page }) => {
  await signIn(page, 'history-race');
  let committed = false, uploads = 0;
  let release!: () => void; const gate = new Promise<void>((resolve) => { release = resolve; });
  await page.route('**/api/import-histories', async (route) => {
    if (route.request().method() !== 'POST') return route.continue();
    const response = await route.fetch(); committed = response.status() === 201; await gate;
    await route.fulfill({ response });
  });
  await page.route('**/api/import-batches/*/items/*/upload', async (route) => { uploads++; await route.continue(); });
  await chooseZip(page, 'Import ZIP', await makeZip());
  await expect.poll(() => committed).toBe(true);
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Start exploring', exact: true })).toBeVisible();
  release(); await expect(page.getByRole('article', { name: /History import/ })).toHaveCount(0);
  await page.evaluate((value) => sessionStorage.setItem('city-runner.session', value), token('history-replacement'));
  await page.reload(); await page.getByRole('button', { name: 'Imports', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Your history starts here' })).toBeVisible();
  expect(uploads).toBe(0);
  expect((await (await page.request.get('/api/import-histories', { headers: headers('history-replacement') })).json()).total).toBe(0);
});
