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
  await page.getByRole('button', { name: /Remaining node 1/ }).click();
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
  await expect(page.getByText('awaiting upload', { exact: true })).toBeVisible();
  await page.reload();
  await page.getByRole('button', { name: 'Imports', exact: true }).click();
  await expect(page.getByText('1 imported', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Resume uploading', exact: true }).click();
  const waitingFile = page.getByRole('button', { name: 'Select file', exact: true });
  await expect(waitingFile).toBeEnabled();
  await expect(page.getByText('awaiting upload', { exact: true })).toBeVisible();
  const reselect = page.waitForEvent('filechooser'); await waitingFile.click();
  await (await reselect).setFiles('tests/.fixtures/browser.fit');
  await expect(page.getByText('2 imported', { exact: true })).toBeVisible({ timeout: 15_000 });
  await page.screenshot({ path: 'test-results/web-imports.png', fullPage: true });
  await page.reload();
  await page.getByRole('button', { name: 'Imports', exact: true }).click();
  await expect(page.getByText('2 imported', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Select file', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'Activities', exact: true }).click();
  await page.getByRole('button', { name: /Browser GPX \[synthetic\]/ }).click();
  await page.getByRole('button', { name: 'Delete activity', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Delete activity', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.getByRole('button', { name: 'Imports', exact: true }).click();
  await expect(page.getByText('deleted', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Select file', exact: true })).toHaveCount(0);
});
