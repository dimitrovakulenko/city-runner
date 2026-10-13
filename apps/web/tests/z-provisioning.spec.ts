import { test, expect } from '@playwright/test';
import type { CoveragePreparationResponse } from '../../mobile/src/api/generated';

const fixture = 'http://127.0.0.1:8004/__test__/provisioning';
const headers = (owner: string) => ({ Authorization: `Bearer web-synthetic-${owner}` });
const recording = (longitude: number) => `<gpx version="1.1"><trk><name>Automatic city [synthetic]</name><type>running</type><trkseg>${Array.from({ length: 5 }, (_, index) => `<trkpt lat="51.044" lon="${longitude + .004 * index}"><time>2026-10-13T07:00:0${index}Z</time></trkpt>`).join('')}</trkseg></trk></gpx>`;
async function signIn(page: import('@playwright/test').Page, owner: string) {
  await page.goto('/');
  await page.evaluate((token) => sessionStorage.setItem('city-runner.session', token), `web-synthetic-${owner}`);
  await page.reload();
  await expect(page.getByRole('button', { name: 'Sign out', exact: true })).toBeVisible();
}
async function upload(page: import('@playwright/test').Page, owner: string, longitude: number) {
  const accepted = await page.request.post('/api/uploads', { headers: headers(owner), multipart: { file: { name: 'automatic-city.gpx', mimeType: 'application/gpx+xml', buffer: Buffer.from(recording(longitude)) } } });
  expect(accepted.status()).toBe(202);
  const source = await accepted.json();
  await expect.poll(async () => (await (await page.request.get(`/api/uploads/${source.id}`, { headers: headers(owner) })).json()).status).toBe('succeeded');
  return (await (await page.request.get(`/api/uploads/${source.id}`, { headers: headers(owner) })).json()).activity_id as string;
}

test('first activity prepares its city automatically and a second owner reuses the shared geography', async ({ page }) => {
  test.setTimeout(120_000);
  expect((await page.request.post(`${fixture}/enable`)).ok()).toBe(true);
  await signIn(page, 'provisioning');
  const activity = await upload(page, 'provisioning', 4.516);
  await page.getByRole('button', { name: 'Imports', exact: true }).click();
  const card = page.getByRole('region', { name: 'City preparation', exact: true });
  await expect(card).toContainText('Preparing Antwerp coverage', { timeout: 30_000 });
  const original = await (await page.request.get(`/api/activities/${activity}`, { headers: headers('provisioning') })).json();
  expect(original.processed).toBe(false);
  expect((await page.request.post(`${fixture}/release`)).ok()).toBe(true);
  await expect.poll(async () => {
    const status: CoveragePreparationResponse = await (await page.request.get('/api/coverage-preparation', { headers: headers('provisioning') })).json();
    return status.state;
  }, { timeout: 45_000 }).toBe('ready');
  await expect.poll(async () => (await (await page.request.get(`/api/activities/${activity}`, { headers: headers('provisioning') })).json()).processed).toBe(true);
  const after = await (await page.request.get(`/api/activities/${activity}`, { headers: headers('provisioning') })).json();
  expect(after.tracks).toEqual(original.tracks); expect(after.timestamps).toEqual(original.timestamps);
  const catalog = await (await page.request.get('/api/datasets', { headers: headers('provisioning') })).json();
  const city = catalog.items.find((item: { region: string }) => item.region === 'Antwerp, BE');
  expect(city.visited_node_count).toBe(5); expect(city.completed_streets).toBe(1);
  const requestsBefore = await (await page.request.get(`${fixture}/requests`)).json();
  const otherActivity = await upload(page, 'provisioning-other', 4.516);
  await expect.poll(async () => (await (await page.request.get(`/api/activities/${otherActivity}`, { headers: headers('provisioning-other') })).json()).processed, { timeout: 30_000 }).toBe(true);
  expect(await (await page.request.get(`${fixture}/requests`)).json()).toEqual(requestsBefore);
  const otherStatus = await (await page.request.get('/api/coverage-preparation', { headers: headers('provisioning-other') })).json();
  expect(otherStatus.cities).toHaveLength(1); expect(otherStatus.cities[0].name).toBe('Antwerp');
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('failed preparation retries without another upload and late preparation responses cannot restore a signed-out owner', async ({ page }) => {
  test.setTimeout(90_000);
  await signIn(page, 'provisioning-retry');
  const activity = await upload(page, 'provisioning-retry', 4.916);
  await page.getByRole('button', { name: 'Sources & sync', exact: true }).click();
  const card = page.getByRole('region', { name: 'City preparation', exact: true });
  await expect(card).toContainText('City preparation needs attention', { timeout: 30_000 });
  expect((await page.request.post(`${fixture}/allow-retry`)).ok()).toBe(true);
  await card.getByRole('button', { name: 'Retry city preparation', exact: true }).click();
  await expect.poll(async () => (await (await page.request.get(`/api/activities/${activity}`, { headers: headers('provisioning-retry') })).json()).processed, { timeout: 30_000 }).toBe(true);
  let release!: () => void; const gate = new Promise<void>((resolve) => { release = resolve; }); let held = false;
  await page.route('**/api/coverage-preparation', async (route) => {
    const response = await route.fetch(); held = true; await gate;
    try { await route.fulfill({ response }); } catch { /* Logout aborted the old request. */ }
  });
  await page.getByRole('button', { name: 'Refresh your data', exact: true }).click();
  await expect.poll(() => held).toBe(true);
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  release();
  await expect(card).toHaveCount(0);
  await expect(page.getByText('Retry City', { exact: true })).toHaveCount(0);
});
