import { test, expect } from '@playwright/test';

const headers = { Authorization: 'Bearer web-synthetic-impact' };

test('activity impact compares retained history, focuses streets and refreshes after deletion', async ({ page }) => {
  let mapZoom = 0;
  page.on('response', (response) => { if (response.ok() && response.url().includes('/api/map?')) mapZoom = Number(new URL(response.url()).searchParams.get('zoom')); });
  async function upload(name: string, day: string, offsets: number[]) {
    const points = offsets.map((offset, index) => `<trkpt lat="51.048" lon="${3.716 + offset * .004}"><time>${day}T08:00:0${index}Z</time></trkpt>`).join('');
    const result = await page.request.post('/api/uploads', { headers, multipart: { file: { name: name + '.gpx', mimeType: 'application/gpx+xml', buffer: Buffer.from(`<gpx version="1.1" creator="synthetic"><trk><name>${name}</name><trkseg>${points}</trkseg></trk></gpx>`) } } });
    expect(result.status()).toBe(202);
    const job = await result.json();
    await expect.poll(async () => (await (await page.request.get(`/api/uploads/${job.id}`, { headers })).json()).status).toBe('succeeded');
    await expect.poll(async () => (await (await page.request.get('/api/progress', { headers })).json()).state).toBe('ready');
    const activities = await (await page.request.get('/api/activities?q=' + encodeURIComponent(name), { headers })).json();
    return activities.items[0].id as string;
  }
  // Deliberately import older history last. Credit follows recorded dates, not uploads.
  const finish = await upload('Finished Garden', '2026-10-06', [2, 3, 4]);
  const repeat = await upload('Revisited Garden', '2026-10-07', [0, 1, 2, 3, 4]);
  const early = await upload('First Garden', '2026-10-05', [0, 1]);
  const datasets = await (await page.request.get('/api/progress', { headers })).json();
  const dataset = datasets.datasets[0].dataset_id;
  const impact = await (await page.request.get(`/api/activities/${finish}/impact?dataset_id=${dataset}`, { headers })).json();
  expect(impact).toMatchObject({ new_nodes: 3, streets_advanced: 1, streets_completed: 1 });
  expect(impact.streets[0]).toMatchObject({ name: 'Garden Way', before_nodes: 2, after_nodes: 5, completed_by_activity: true });
  await page.goto('/'); await page.evaluate(() => sessionStorage.setItem('city-runner.session', 'web-synthetic-impact')); await page.reload();
  await page.getByRole('button', { name: 'Activities', exact: true }).click();
  await page.getByRole('button', { name: /Finished Garden/ }).click();
  const result = page.getByRole('region', { name: 'Activity contribution', exact: true });
  await expect(result.locator('[data-metric="new-nodes"]')).toHaveText('3New GPS nodes');
  await expect(result.locator('[data-metric="completed"]')).toHaveText('1Streets completed');
  await expect(result).toContainText('2 → 5 / 5 GPS nodes');
  await expect(result).toContainText('same-day ordering is approximate');
  await result.getByRole('button', { name: 'Show Garden Way on map', exact: true }).click();
  await expect(page.locator('.map-message')).toContainText('Highlighted Garden Way');
  const focusedZoom = mapZoom;
  const zoomOut = page.waitForResponse((response) => response.ok() && response.url().includes('/api/map?') && Number(new URL(response.url()).searchParams.get('zoom')) < focusedZoom - .5);
  await page.getByRole('button', { name: 'Zoom out', exact: true }).click();
  const awayZoom = Number(new URL((await zoomOut).url()).searchParams.get('zoom'));
  const refocused = page.waitForResponse((response) => response.ok() && response.url().includes('/api/map?') && Number(new URL(response.url()).searchParams.get('zoom')) > awayZoom + .5);
  await result.getByRole('button', { name: 'Show Garden Way on map', exact: true }).click();
  await refocused;
  await page.route('**/api/map?**', async (route) => {
    const response = await route.fetch(); const body = await response.json();
    body.streets = body.streets.filter((street: { name: string }) => street.name !== 'Garden Way');
    await route.fulfill({ response, json: body });
  });
  await page.getByRole('button', { name: 'Zoom out', exact: true }).click();
  await expect(page.locator('.map-message')).toContainText('display geometry is unavailable');
  await page.unroute('**/api/map?**');
  await result.getByRole('button', { name: 'Show Garden Way on map', exact: true }).click();
  await expect(page.locator('.map-message')).toContainText('Highlighted Garden Way');
  await result.getByRole('button', { name: 'Garden Way details', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Garden Way', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Activities', exact: true }).click();
  await page.getByRole('button', { name: /Revisited Garden/ }).click();
  await expect(result.locator('[data-metric="new-nodes"]')).toHaveText('0New GPS nodes');
  await expect(result).toContainText('Revisited streets');
  await expect(page.locator('.map-message')).not.toContainText('Highlighted Garden Way');
  // HTTP deletion changes the historical comparison on explicit refresh.
  expect((await page.request.delete(`/api/activities/${early}`, { headers })).status()).toBe(204);
  await page.getByRole('button', { name: /Finished Garden/ }).click();
  await page.getByRole('button', { name: 'Refresh your data', exact: true }).click();
  await expect(result).toContainText('0 → 3 / 5 GPS nodes');
  await expect(result.locator('[data-metric="completed"]')).toHaveText('0Streets completed');
  const strict = page.waitForResponse((response) => response.ok() && response.url().includes('/impact?') && new URL(response.url()).searchParams.get('rule') === 'strict');
  await result.getByLabel('Contribution completion rule', { exact: true }).selectOption('strict');
  await strict;
  await expect(result).toContainText('0 → 3 / 5 GPS nodes');
  await page.route('**/api/activities/*/impact?**', async (route) => {
    const response = await route.fetch(); const body = await response.json();
    body.history_status = 'unknown-dates'; body.new_nodes = body.streets_advanced = body.streets_completed = null;
    for (const street of body.streets) street.new_nodes = street.before_nodes = street.after_nodes = street.completed_by_activity = null;
    await route.fulfill({ response, json: body });
  });
  await page.getByRole('button', { name: 'Refresh your data', exact: true }).click();
  await expect(result).toContainText('dates are missing or invalid');
  await expect(result.locator('[data-metric="new-nodes"]')).toHaveText('—New GPS nodes');
  await expect(result.getByRole('button', { name: 'Show Garden Way on map', exact: true })).toBeVisible();
  await page.unroute('**/api/activities/*/impact?**');
  await page.route('**/api/activities/*/impact?**', async (route) => {
    const response = await route.fetch(); const body = await response.json();
    body.coverage.status = 'pending'; body.supported_nodes = body.new_nodes = body.streets_advanced = body.streets_completed = body.total = null; body.streets = [];
    await route.fulfill({ response, json: body });
  });
  await page.getByRole('button', { name: 'Refresh your data', exact: true }).click();
  await expect(result).toContainText('Coverage is still processing');
  await expect(result.locator('.impact-street')).toHaveCount(0);
  await page.unroute('**/api/activities/*/impact?**');
  await page.route('**/api/activities/*/impact?**', (route) => route.fulfill({ status: 503, json: { detail: 'Try again later' } }));
  await page.getByRole('button', { name: 'Refresh your data', exact: true }).click();
  await expect(result.getByRole('alert')).toBeVisible();
  await page.unroute('**/api/activities/*/impact?**');
  await result.getByRole('button', { name: 'Retry contribution', exact: true }).click();
  await expect(result).toContainText('0 → 3 / 5 GPS nodes');
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: 'test-results/web-activity-impact.png', fullPage: true });
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await expect(result).toHaveCount(0);
  await expect(page.locator('.map-message')).toHaveCount(0);
  // Owner isolation is enforced on the actual endpoint before dataset lookup.
  expect((await page.request.get(`/api/activities/${repeat}/impact?dataset_id=${dataset}`, { headers: { Authorization: 'Bearer web-synthetic-bob' } })).status()).toBe(404);
});
