import { test, expect } from '@playwright/test';
import type { Page } from '@playwright/test';

async function signIn(page: Page, account = 'alice') {
  await page.goto('/');
  await page.evaluate((token) => sessionStorage.setItem('city-runner.session', token), `web-synthetic-${account}`);
  await page.reload();
  await expect(page.getByRole('button', { name: 'Sign out', exact: true })).toBeVisible();
}

test('signed-out browser is private, responsive and shows an honest sign-in failure without configuration', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Your city. A new perspective.' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Activities', exact: true })).toBeDisabled();
  await expect(page.locator('.map-area')).toHaveAttribute('data-ready', 'true', { timeout: 15_000 });
  await page.getByRole('button', { name: 'Start exploring' }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('Authentication is unavailable');
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('real backend drives activities, street contributions, manual completion and private account isolation', async ({ page }) => {
  await signIn(page);
  await expect(page.getByText('Morning loop [synthetic]', { exact: true })).toBeVisible();
  await expect(page.getByRole('checkbox', { name: 'Missing nodes' })).not.toBeChecked();
  await page.getByRole('button', { name: 'Activities', exact: true }).click();
  await page.getByRole('button', { name: /Morning loop \[synthetic\]/ }).click();
  await expect(page.getByRole('heading', { name: 'Morning loop [synthetic]' })).toBeVisible();
  await expect(page.getByText('10 original GPS samples', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Cities & streets', exact: true }).click();
  await page.getByLabel('Region', { exact: true }).selectOption({ label: 'Demo Region' });
  await page.getByRole('button', { name: /Demo City/ }).click();
  await page.getByRole('button', { name: /Maple Lane/ }).click();
  await expect(page.getByRole('button', { name: /Morning loop \[synthetic\]/ })).toBeVisible();
  await page.getByRole('button', { name: 'Back to streets' }).click();
  await page.getByRole('button', { name: /Garden Way/ }).click();
  await page.getByLabel('Manual completion reason').fill('Synthetic street blocked for QA');
  await page.getByRole('button', { name: 'Mark manually complete' }).click();
  await expect(page.getByRole('button', { name: 'Undo manual completion' })).toBeVisible();
  await expect(page.getByText('GPS nodes visited', { exact: false })).toContainText('0 / 5');
  await page.route('**/api/map?**', async (route) => {
    const response = await route.fetch(); const body = await response.json();
    for (const limit of Object.values(body.limits) as Array<{ truncated: boolean }>) limit.truncated = false;
    body.limits.missing_nodes.truncated = true;
    await route.fulfill({ response, json: body });
  });
  await page.getByRole('button', { name: /Remaining node 1/ }).click();
  await expect(page.getByRole('checkbox', { name: 'Missing nodes' })).toBeChecked();
  await expect(page.locator('.map-message')).toContainText('Some results are limited');
  await page.getByRole('checkbox', { name: 'Missing nodes' }).uncheck();
  await expect(page.locator('.map-message')).not.toContainText('Some results are limited');
  await page.screenshot({ path: 'test-results/web-street.png', fullPage: true });
  await page.getByRole('button', { name: 'Undo manual completion' }).click();
  await expect(page.getByRole('button', { name: 'Mark manually complete' })).toBeVisible();
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Start exploring' })).toBeVisible();
  await expect(page.getByText('Morning loop [synthetic]', { exact: true })).toHaveCount(0);
  await page.evaluate(() => sessionStorage.setItem('city-runner.session', 'web-synthetic-bob'));
  await page.reload();
  await expect(page.getByText('An open road ahead')).toBeVisible();
  await expect(page.getByText('Morning loop [synthetic]', { exact: true })).toHaveCount(0);
});

test('browser GPX/FIT uploads pause, resume, survive reload and remain deleted after activity deletion', async ({ page }) => {
  await signIn(page, 'bob');
  await page.getByRole('button', { name: 'Imports', exact: true }).click();
  let release!: () => void; const gate = new Promise<void>((resolve) => { release = resolve; }); let waiting = false; let uploads = 0;
  await page.route('**/api/import-batches/*/items/*/upload', async (route) => { if (++uploads === 2) { waiting = true; await gate; } await route.continue(); });
  const chooser = page.waitForEvent('filechooser');
  await page.getByRole('button', { name: 'Choose activity files' }).click();
  await (await chooser).setFiles(['tests/.fixtures/browser.gpx', 'tests/.fixtures/browser.fit']);
  await expect.poll(() => waiting).toBe(true);
  await page.getByRole('button', { name: 'Stop uploading', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Resume uploading', exact: true })).toBeVisible();
  release();
  await expect(page.getByText('1 imported', { exact: true })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText('Ready to upload', { exact: true })).toBeVisible();
  await page.reload();
  await page.getByRole('button', { name: 'Imports', exact: true }).click();
  await expect(page.getByText('1 imported', { exact: true })).toBeVisible();
  const waitingFile = page.getByRole('button', { name: 'Select file', exact: true });
  await expect(waitingFile).toBeEnabled();
  await expect(page.getByText('File needed', { exact: true })).toBeVisible();
  const reselect = page.waitForEvent('filechooser'); await waitingFile.click();
  await (await reselect).setFiles('tests/.fixtures/browser.fit');
  await expect(page.getByText('Ready to upload', { exact: true })).toBeVisible();
  await expect(page.getByText('1 of 2 files uploaded', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Resume uploading', exact: true }).click();
  await expect(page.getByText('2 imported', { exact: true })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole('progressbar', { name: 'Files uploaded' })).toHaveAttribute('value', '2');
  await page.screenshot({ path: 'test-results/web-imports.png', fullPage: true });
  await page.reload();
  await page.getByRole('button', { name: 'Imports', exact: true }).click();
  await expect(page.getByText('2 imported', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Select file', exact: true })).toHaveCount(0);
  const focusedMap = page.waitForResponse((response) => response.url().includes('/api/map?') && Number(new URL(response.url()).searchParams.get('zoom')) > 15 && response.ok());
  await page.getByRole('button', { name: 'browser.gpx', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Browser GPX [synthetic]', exact: true })).toBeVisible();
  await expect(page.getByText('5 original GPS samples', { exact: true })).toBeVisible();
  await focusedMap;
  await page.getByRole('button', { name: 'Delete activity', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Delete activity', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.getByRole('button', { name: 'Imports', exact: true }).click();
  await expect(page.getByText('Deleted', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'browser.gpx', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Select file', exact: true })).toHaveCount(0);
  const corrupt = page.waitForEvent('filechooser'); await page.getByRole('button', { name: 'Choose activity files' }).click();
  await (await corrupt).setFiles('tests/.fixtures/corrupt.fit');
  await expect(page.getByText('This file cannot be processed. Choose a corrected file and start a new import.', { exact: true })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole('button', { name: 'Retry processing', exact: true })).toHaveCount(0);
});

test('manifest creation is serialized and a failed upload has a direct retry with retained files', async ({ page }) => {
  await signIn(page, 'bob'); await page.getByRole('button', { name: 'Imports', exact: true }).click();
  let release!: () => void; const held = new Promise<void>((resolve) => { release = resolve; }); let waiting = false;
  await page.route('**/api/import-batches', async (route) => {
    if (route.request().method() !== 'POST') return route.continue();
    const response = await route.fetch(); waiting = true; await held; await route.fulfill({ response });
  });
  let aborted = false;
  await page.route('**/api/import-batches/*/items/*/upload', async (route) => {
    if (!aborted) { aborted = true; await route.abort('failed'); } else await route.continue();
  });
  const chooser = page.waitForEvent('filechooser'); await page.getByRole('button', { name: 'Choose activity files' }).click();
  await (await chooser).setFiles(['tests/.fixtures/browser.gpx', 'tests/.fixtures/browser.fit']);
  await expect.poll(() => waiting).toBe(true);
  await expect(page.getByRole('button', { name: 'Preparing files…', exact: false })).toBeDisabled();
  release();
  await expect(page.getByRole('button', { name: 'Retry uploading', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: 'Retry uploading', exact: true }).click();
  await expect(page.getByText('2 imported', { exact: true })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText('Already imported', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Choose activity files' })).toBeEnabled();
});

test('tab navigation during manifest creation cannot strand a second batch or overlap uploads', async ({ page }) => {
  await signIn(page, 'bob'); await page.getByRole('button', { name: 'Imports', exact: true }).click();
  let releaseManifest!: () => void; const manifestGate = new Promise<void>((resolve) => { releaseManifest = resolve; });
  let releaseUpload!: () => void; const uploadGate = new Promise<void>((resolve) => { releaseUpload = resolve; });
  const ids: string[] = []; let held = false; let uploading = false; let active = 0;
  await page.route('**/api/import-batches', async (route) => {
    if (route.request().method() !== 'POST') return route.continue();
    const response = await route.fetch(); ids.push((await response.json()).id);
    if (ids.length === 1) { held = true; await manifestGate; }
    await route.fulfill({ response });
  });
  await page.route('**/api/import-batches/*/items/*/upload', async (route) => {
    expect(++active).toBe(1);
    if (!uploading) { uploading = true; await uploadGate; }
    const response = page.waitForResponse((response) => response.url() === route.request().url());
    await route.continue(); await response; active--;
  });
  const select = async (filename: string) => {
    const chooser = page.waitForEvent('filechooser'); await page.getByRole('button', { name: 'Choose activity files' }).click();
    await (await chooser).setFiles(`tests/.fixtures/${filename}`);
  };
  await select('browser.gpx'); await expect.poll(() => held).toBe(true);
  await page.getByRole('button', { name: 'Activities', exact: true }).click();
  await page.getByRole('button', { name: 'Imports', exact: true }).click();
  await select('browser.fit'); await expect.poll(() => uploading).toBe(true);
  releaseManifest(); await expect(page.getByText('Ready to upload', { exact: true })).toBeVisible(); releaseUpload();
  await expect.poll(async () => Promise.all(ids.map(async (id) => {
    const response = await page.request.get(`/api/import-batches/${id}`, { headers: { Authorization: 'Bearer web-synthetic-bob' } });
    return (await response.json()).counts.awaiting_upload;
  }))).toEqual([0, 0]);
  await expect(page.getByRole('button', { name: 'Choose activity files' })).toBeEnabled();
});
