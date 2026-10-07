import { test, expect } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { readFileSync, writeFileSync, unlinkSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

const token = (account: string) => `web-synthetic-${account}`;
const signIn = async (page: import('@playwright/test').Page, account: string) => {
  await page.goto('/');
  await page.evaluate((value) => sessionStorage.setItem('city-runner.session', value), token(account));
  await page.reload();
  await expect(page.getByRole('button', { name: 'Open account settings' })).toBeVisible();
};

async function seedOriginal(page: import('@playwright/test').Page, account: string) {
  const headers = { Authorization: `Bearer ${token(account)}` };
  const source = readFileSync('tests/.fixtures/browser.gpx');
  const accepted = await page.request.post('/api/uploads', { headers, multipart: { file: { name: 'account-test.gpx', mimeType: 'application/gpx+xml', buffer: source } } });
  expect(accepted.status()).toBe(202);
  const job = await accepted.json() as { id: string };
  await expect.poll(async () => (await (await page.request.get(`/api/uploads/${job.id}`, { headers })).json()).status).toBe('succeeded');
  return source;
}

test('account archive downloads a private ZIP; cancel and request errors keep the account active', async ({ page }) => {
  const original = await seedOriginal(page, 'account-export');
  await signIn(page, 'account-export');
  await page.evaluate(() => localStorage.setItem('city-runner.viewport.web-test-account-export', JSON.stringify({ center: [3.72, 51.05], zoom: 16 })));
  await page.getByRole('button', { name: 'Open account settings' }).click();
  await page.getByRole('button', { name: 'Delete my account' }).click();
  await page.getByRole('button', { name: 'Keep account' }).click();
  await expect(page.getByRole('heading', { name: 'Delete account' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();

  await page.route('**/api/account/export', async (route) => {
    const response = await route.fetch();
    expect(response.status()).toBe(200);
    expect(response.headers()['content-type']).toContain('application/zip');
    expect(response.headers()['cache-control']).toContain('no-store');
    await route.fulfill({ response });
  });
  const downloadEvent = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Download account archive' }).click();
  const download = await downloadEvent;
  expect(download.suggestedFilename()).toBe('city-runner-account.zip');
  const stream = await download.createReadStream();
  const chunks: Buffer[] = [];
  for await (const chunk of stream!) chunks.push(Buffer.from(chunk));
  const zip = Buffer.concat(chunks);
  expect(zip.subarray(0, 4)).toEqual(Buffer.from([0x50, 0x4b, 0x03, 0x04]));
  expect(zip.includes(Buffer.from(token('account-export')))).toBe(false);
  const zipPath = join(tmpdir(), `city-runner-account-test-${Date.now()}.zip`);
  writeFileSync(zipPath, zip);
  try {
    const audit = execFileSync('python3', ['-c', `import json,sys,zipfile
with zipfile.ZipFile(sys.argv[1]) as z:
 names=z.namelist(); manifest=json.loads(z.read('manifest.json'))
 assert manifest['account_id']=='web-test-account-export'
 assert all(not n.startswith('/') and '..' not in n.split('/') for n in names)
 assert all('web-synthetic-' not in z.read(n).decode('utf-8','ignore') for n in names)
 originals=[n for n in names if n.startswith('originals/') and n.endswith('.gpx')]
 assert len(originals)==1 and z.read(originals[0])==open(sys.argv[2],'rb').read()
 print(json.dumps({'account':manifest['account_id'],'originals':len(originals)}))`, zipPath, 'tests/.fixtures/browser.gpx'], { encoding: 'utf8' });
    expect(JSON.parse(audit)).toEqual({ account: 'web-test-account-export', originals: 1 });
  } finally { unlinkSync(zipPath); }
  await expect(page.locator('.detail-card').filter({ has: page.getByRole('heading', { name: 'Export your data' }) }).getByRole('status')).toContainText('archive is ready');

  await page.route('**/api/account/export', (route) => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'Synthetic export unavailable' }) }));
  await page.getByRole('button', { name: 'Download account archive' }).click();
  await expect(page.getByRole('alert')).toContainText('Synthetic export unavailable');
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
});

test('typed account deletion revokes the old token and forgets only that account viewport', async ({ page }) => {
  await seedOriginal(page, 'account-delete');
  await signIn(page, 'account-delete');
  const oldKey = 'city-runner.viewport.web-test-account-delete';
  const replacementKey = 'city-runner.viewport.web-test-account-replacement';
  await page.evaluate(([oldKey, replacementKey]) => {
    localStorage.setItem(oldKey, JSON.stringify({ center: [3.72, 51.05], zoom: 16 }));
    localStorage.setItem(replacementKey, JSON.stringify({ center: [3.73, 51.06], zoom: 17 }));
  }, [oldKey, replacementKey]);
  await page.getByRole('button', { name: 'Open account settings' }).click();
  await page.getByRole('button', { name: 'Delete my account' }).click();
  const confirm = page.getByRole('textbox', { name: 'Account deletion confirmation' });
  await expect(page.getByRole('button', { name: 'Permanently delete account' })).toBeDisabled();
  await confirm.fill('delete');
  await expect(page.getByRole('button', { name: 'Permanently delete account' })).toBeDisabled();
  await confirm.fill('DELETE');
  await page.route('**/api/account', (route) => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'Synthetic deletion unavailable' }) }));
  await page.getByRole('button', { name: 'Permanently delete account' }).click();
  await expect(page.getByRole('alert')).toContainText('Synthetic deletion unavailable');
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  await page.unroute('**/api/account');
  const accepted = page.waitForResponse((response) => response.url().endsWith('/api/account') && response.request().method() === 'DELETE');
  await page.getByRole('button', { name: 'Permanently delete account' }).click();
  const response = await accepted;
  expect(response.status()).toBe(202);
  const receipt = await response.json() as { id: string; status: 'complete' | 'cleanup-pending' };
  expect(receipt.status).toMatch(/^(complete|cleanup-pending)$/);
  await expect(page.getByRole('status').filter({ hasText: 'account has been removed' })).toContainText('queued for removal');
  await expect(page.getByRole('button', { name: 'Start exploring' })).toBeVisible();
  await expect.poll(() => page.evaluate((key) => localStorage.getItem(key), oldKey)).toBeNull();
  expect(await page.evaluate((key) => localStorage.getItem(key), replacementKey)).not.toBeNull();
  expect(await page.evaluate(() => sessionStorage.getItem('city-runner.session'))).toBeNull();
  expect((await page.request.get('/api/account/export', { headers: { Authorization: `Bearer ${token('account-delete')}` } })).status()).toBe(401);
  expect((await page.request.get('/api/account/export', { headers: { Authorization: `Bearer ${token('account-replacement')}` } })).status()).toBe(200);
  const deletionStateUrl = `http://127.0.0.1:8004/__test__/account-deletions/${receipt.id}`;
  await expect.poll(async () => (await (await page.request.get(deletionStateUrl)).json()).status).toBe('complete');
  expect(await (await page.request.get(deletionStateUrl)).json()).toMatchObject({ status: 'complete', originals_present: false, outbox_statuses: ['complete'] });
  await page.evaluate((value) => sessionStorage.setItem('city-runner.session', value), token('account-replacement'));
  await page.reload();
  await expect(page.getByRole('button', { name: 'Open account settings' })).toBeVisible();
  await expect.poll(() => page.evaluate((key) => JSON.parse(localStorage.getItem(key)!).zoom, replacementKey)).toBe(17);
});

test('a delayed old-account deletion response cannot replace a newer session or viewport', async ({ page }) => {
  await signIn(page, 'account-race');
  const oldKey = 'city-runner.viewport.web-test-account-race';
  const replacementKey = 'city-runner.viewport.web-test-account-replacement';
  await page.evaluate(([oldKey, replacementKey]) => {
    localStorage.setItem(oldKey, JSON.stringify({ center: [3.72, 51.05], zoom: 16 }));
    localStorage.setItem(replacementKey, JSON.stringify({ center: [3.73, 51.06], zoom: 17 }));
  }, [oldKey, replacementKey]);
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  let deletionAccepted = false;
  await page.route('**/api/account', async (route) => {
    if (route.request().method() !== 'DELETE') return route.continue();
    const response = await route.fetch();
    deletionAccepted = response.status() === 202;
    await gate;
    await route.fulfill({ response });
  });
  await page.getByRole('button', { name: 'Open account settings' }).click();
  await page.getByRole('button', { name: 'Delete my account' }).click();
  await page.getByRole('textbox', { name: 'Account deletion confirmation' }).fill('DELETE');
  await page.getByRole('button', { name: 'Permanently delete account' }).click();
  await expect.poll(() => deletionAccepted).toBe(true);

  // A replaced session can arrive while the old request is still unresolved.
  await page.evaluate((value) => sessionStorage.setItem('city-runner.session', value), token('account-replacement'));
  release();
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem('city-runner.session'))).toBe(token('account-replacement'));
  await page.reload();
  await expect(page.getByRole('button', { name: 'Open account settings' })).toBeVisible();
  expect(await page.evaluate((key) => localStorage.getItem(key), replacementKey)).not.toBeNull();
  await expect.poll(() => page.evaluate((key) => localStorage.getItem(key), oldKey)).not.toBeNull();
});
