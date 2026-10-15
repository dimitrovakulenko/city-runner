import { test, expect, type Page } from '@playwright/test';

const waypoints = [[3.716, 51.044], [3.732, 51.044]];
async function openRoute(page: Page, name: string, account: string) {
  const token = `web-synthetic-${account}`;
  const headers = { Authorization: `Bearer ${token}` };
  const response = await page.request.post('/api/routes', { headers, data: { name, waypoints } });
  expect(response.status()).toBe(201);
  const route = await response.json();
  await page.goto('/'); await page.evaluate((token) => sessionStorage.setItem('city-runner.session', token), token); await page.reload();
  await page.getByRole('button', { name: 'Routes', exact: true }).click();
  await expect(page.locator('.map-area')).toHaveAttribute('data-ready', 'true');
  await page.getByRole('button', { name: new RegExp(name + '.*saved route') }).click();
  await expect(page.getByRole('button', { name: 'Follow route', exact: true })).toBeEnabled();
  return { route, headers };
}
const following = (page: Page) => page.getByRole('region', { name: 'Route following', exact: true });

test('real browser GPS follows a saved route, supports pan/recenter and offline movement without creating activity data', async ({ page, context }) => {
  await context.grantPermissions(['geolocation']);
  await context.setGeolocation({ longitude: 3.724, latitude: 51.044, accuracy: 6 });
  const { route, headers } = await openRoute(page, 'Following real GPS', 'following-real');
  const before = await (await page.request.get('/api/progress', { headers })).json();
  const activitiesBefore = await (await page.request.get('/api/activities', { headers })).json();
  const remembered = await page.evaluate(() => localStorage.getItem('city-runner.viewport.web-test-following-real'));
  let routeRequests = 0;
  page.on('request', (request) => { if (new URL(request.url()).pathname.startsWith('/api/routes')) routeRequests++; });
  await page.getByRole('button', { name: 'Follow route', exact: true }).click();
  await expect(following(page)).toContainText('On route · ±6 m');
  await expect(page.locator('.map-area')).toHaveAttribute('data-located', 'true');
  const canvas = page.locator('.maplibregl-canvas'); const box = (await canvas.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2); await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 + 90, box.y + box.height / 2 + 50, { steps: 8 }); await page.mouse.up();
  await expect(following(page).getByRole('button', { name: 'Recenter route' })).toBeEnabled();
  await following(page).getByRole('button', { name: 'Recenter route' }).click();
  await expect(following(page).getByRole('button', { name: 'Recenter route' })).toBeDisabled();
  await context.setGeolocation({ longitude: 3.724, latitude: 51.046, accuracy: 6 });
  await expect(following(page)).toHaveAttribute('data-course', 'off-route');
  await expect(following(page)).toContainText('Off route · 222 m from route');
  await context.setGeolocation({ longitude: 3.724, latitude: 51.044, accuracy: 100 });
  await expect(following(page)).toContainText('accuracy is low');
  await expect(page.locator('.map-area')).toHaveAttribute('data-located', 'false');
  await context.setOffline(true);
  await context.setGeolocation({ longitude: 3.725, latitude: 51.044, accuracy: 5 });
  await expect(following(page)).toContainText('On route · ±5 m');
  const centered = page.waitForResponse((response) => {
    const url = new URL(response.url()); const bounds = (url.searchParams.get('bbox') ?? '').split(',').map(Number);
    return response.ok() && url.pathname === '/api/map' && bounds.length === 4 && Math.abs((bounds[0] + bounds[2]) / 2 - 3.725) < .0001 && Math.abs((bounds[1] + bounds[3]) / 2 - 51.044) < .0001;
  });
  await context.setOffline(false);
  await page.setViewportSize({ width: 390, height: 844 }); await centered;
  await expect(following(page).getByRole('button', { name: 'Stop following' })).toBeInViewport();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: 'test-results/web-route-following-mobile.png', fullPage: true });
  await following(page).getByRole('button', { name: 'Stop following' }).click();
  await expect(following(page)).toHaveCount(0);
  await expect(page.locator('.map-area')).toHaveAttribute('data-located', 'false');
  expect(routeRequests).toBe(0);
  expect(await page.evaluate(() => localStorage.getItem('city-runner.viewport.web-test-following-real'))).toBe(remembered);
  expect(await (await page.request.get('/api/progress', { headers })).json()).toEqual(before);
  expect(await (await page.request.get('/api/activities', { headers })).json()).toEqual(activitiesBefore);
  expect((await page.request.delete(`/api/routes/${route.id}?expected_revision=1`, { headers })).status()).toBe(204);
});

type FakeGps = { emit: (longitude: number, latitude: number, accuracy: number, id?: number, timestamp?: number) => void; fail: (code: number) => void; active: number[]; last: number };
async function fakeGps(page: Page) {
  await page.addInitScript(() => {
    const callbacks = new Map<number, { success: PositionCallback; fail: PositionErrorCallback }>();
    const gps: FakeGps = {
      active: [], last: 0,
      emit(longitude, latitude, accuracy, id = gps.last, timestamp = Date.now()) { callbacks.get(id)!.success({ timestamp, coords: { longitude, latitude, accuracy } } as GeolocationPosition); },
      fail(code) { callbacks.get(gps.last)!.fail({ code } as GeolocationPositionError); },
    };
    (window as unknown as { followingGps: FakeGps }).followingGps = gps;
    Object.defineProperty(navigator, 'geolocation', { configurable: true, value: {
      watchPosition(success: PositionCallback, fail: PositionErrorCallback) { callbacks.set(++gps.last, { success, fail }); gps.active.push(gps.last); return gps.last; },
      clearWatch(id: number) { gps.active = gps.active.filter((value) => value !== id); },
    } });
  });
}
async function emit(page: Page, id?: number, timestamp?: number) {
  await page.evaluate(({ id, timestamp }) => (window as unknown as { followingGps: FakeGps }).followingGps.emit(3.724, 51.044, 5, id, timestamp), { id, timestamp });
}
const activeWatches = (page: Page) => page.evaluate(() => (window as unknown as { followingGps: FakeGps }).followingGps.active.length);

test('GPS silence, timeout and permission revocation clear guidance, and retry fences old fixes', async ({ page }) => {
  await page.clock.install(); await fakeGps(page);
  const { route, headers } = await openRoute(page, 'Following GPS failures', 'following-failures');
  await page.getByRole('button', { name: 'Follow route', exact: true }).click(); await emit(page);
  await expect(following(page)).toContainText('On route');
  await page.clock.fastForward(15_001);
  await expect(following(page)).toContainText('GPS update overdue');
  await expect(page.locator('.map-area')).toHaveAttribute('data-located', 'false');
  await emit(page); await expect(following(page)).toContainText('On route');
  await page.evaluate(() => (window as unknown as { followingGps: FakeGps }).followingGps.fail(3));
  await expect(following(page)).toContainText('GPS timed out');
  await following(page).getByRole('button', { name: 'Retry GPS' }).click();
  expect(await activeWatches(page)).toBe(1);
  await emit(page, 1); await expect(following(page)).toContainText('Waiting for GPS');
  await emit(page); await expect(following(page)).toContainText('On route');
  await page.evaluate(() => (window as unknown as { followingGps: FakeGps }).followingGps.fail(1));
  await expect(following(page)).toContainText('permission denied');
  expect(await activeWatches(page)).toBe(0);
  await page.clock.fastForward(1); await emit(page); await expect(following(page)).toContainText('permission denied');
  await following(page).getByRole('button', { name: 'Retry GPS' }).click(); await emit(page);
  await expect(following(page)).toContainText('On route');
  await following(page).getByRole('button', { name: 'Stop following' }).click();
  expect(await activeWatches(page)).toBe(0);
  await page.request.delete(`/api/routes/${route.id}?expected_revision=1`, { headers });
});

test('hidden-page and pagehide pause require explicit resume; navigation and logout stop and fence callbacks', async ({ page }) => {
  await fakeGps(page); const { route, headers } = await openRoute(page, 'Following foreground', 'following-foreground');
  await page.getByRole('button', { name: 'Follow route', exact: true }).click(); await emit(page);
  await page.evaluate(() => { Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' }); document.dispatchEvent(new Event('visibilitychange')); });
  await expect(following(page)).toContainText('Following paused'); expect(await activeWatches(page)).toBe(0);
  await emit(page); await expect(page.locator('.map-area')).toHaveAttribute('data-located', 'false');
  await page.evaluate(() => { Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' }); document.dispatchEvent(new Event('visibilitychange')); });
  await expect(following(page)).toContainText('Following paused'); expect(await activeWatches(page)).toBe(0);
  await following(page).getByRole('button', { name: 'Resume following' }).click(); await emit(page);
  await expect(following(page)).toContainText('On route');
  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pagehide', { persisted: true })));
  await expect(following(page)).toContainText('Following paused'); expect(await activeWatches(page)).toBe(0);
  await following(page).getByRole('button', { name: 'Resume following' }).click(); await emit(page);
  await page.getByRole('button', { name: 'Activities', exact: true }).click(); expect(await activeWatches(page)).toBe(0);
  await emit(page); await expect(page.locator('.map-area')).toHaveAttribute('data-located', 'false');
  await page.getByRole('button', { name: 'Routes', exact: true }).click();
  await page.getByRole('button', { name: 'Follow route', exact: true }).click(); await emit(page);
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  await page.route('**/api/auth/session', async (request) => {
    if (request.request().method() !== 'DELETE') return request.continue();
    const response = await request.fetch(); await gate; await request.fulfill({ response });
  });
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  try {
    expect(await activeWatches(page)).toBe(0);
    await emit(page); await expect(page.locator('.map-area')).toHaveAttribute('data-located', 'false');
  } finally { release(); }
  await expect(page.getByRole('button', { name: 'Sign in', exact: true })).toBeVisible();
});

test('route editing and reload stop following without changing the saved route', async ({ page }) => {
  await fakeGps(page); const { route, headers } = await openRoute(page, 'Following edit fences', 'following-edit');
  await page.getByRole('button', { name: 'Follow route', exact: true }).click(); await emit(page);
  await page.getByRole('button', { name: 'Remove waypoint 2', exact: true }).click();
  expect(await activeWatches(page)).toBe(0); await emit(page);
  await expect(following(page)).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Follow route', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: /Following edit fences.*saved route/ }).click();
  await page.getByRole('button', { name: 'Follow route', exact: true }).click(); await emit(page);
  await page.reload(); await page.getByRole('button', { name: 'Routes', exact: true }).click();
  expect(await activeWatches(page)).toBe(0); await expect(following(page)).toHaveCount(0);
  expect(await (await page.request.get(`/api/routes/${route.id}`, { headers })).json()).toEqual(route);
  await page.request.delete(`/api/routes/${route.id}?expected_revision=1`, { headers });
});
