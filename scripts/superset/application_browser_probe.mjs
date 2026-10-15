// Production web bundle and the real authenticated Superset services.
import { chromium, expect } from '../../apps/web/node_modules/@playwright/test/index.mjs';
import { readFile, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';

const web = process.env.ANALYTICS_ACCEPTANCE_WEB || 'http://127.0.0.1:4183', analytics = process.env.ANALYTICS_ACCEPTANCE_UI || 'http://127.0.0.1:8091';
const argument = (name, fallback) => process.argv.includes(name) ? process.argv[process.argv.indexOf(name) + 1] : fallback;
const output = argument('--output', '/tmp/d46-browser.json');
const screenshot = (name) => output.replace(/\.json$/, `-${name}.png`);
const fixture = JSON.parse(await readFile(argument('--fixture', '/tmp/city-runner-analytics-acceptance.json'), 'utf8'));
const browser = await chromium.launch({ channel: process.env.PILOT_BROWSER_CHANNEL || 'chrome', headless: true });
expect.configure({ timeout: 30_000 });
const report = { checks: {}, failures: [], objects: {} };
report.chart_errors = [];
report.chart_statuses = {};
report.browser_errors = [];
let current;
const pages = {};

async function title(page, name, value) {
  const field = page.getByRole('textbox', { name });
  await field.click(); await field.fill(value); await field.press('Tab');
}

try {
  for (const [label, account] of Object.entries(fixture.accounts)) {
    const context = await browser.newContext();
    await context.addInitScript(({ origin, token }) => {
      if (location.origin === origin && !sessionStorage.getItem('city-runner.session')) sessionStorage.setItem('city-runner.session', token);
    }, { origin: web, token: account.token });
    const page = current = await context.newPage();
    pages[label] = page;
    page.setDefaultTimeout(30_000);
    await page.goto(web);
    await page.getByRole('button', { name: 'Insights', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Open Insights', exact: true })).toBeEnabled();
    report.checks[`web_insights_ready_${label}`] = true;
    await page.screenshot({ path: screenshot(`insights-${label}`) });
    const opened = page.waitForEvent('popup');
    await page.getByRole('button', { name: 'Copy Activity template', exact: true }).click();
    const board = current = await opened;
    board.on('pageerror', (error) => report.browser_errors.push(String(error)));
    board.on('response', async (response) => {
      if (response.status() >= 400 && /\/api\/v1\/(?:dashboard|chart)\b/.test(response.url())) report.browser_errors.push({ path: new URL(response.url()).pathname, status: response.status() });
    });
    board.on('response', async (response) => {
      if (!/\/api\/v1\/chart\/(?:[0-9]+\/)?data\b/.test(response.url())) return;
      try {
        const body = await response.json();
        report.chart_statuses[response.status()] = (report.chart_statuses[response.status()] || 0) + 1;
        if (response.status() >= 400 || body.result?.some((result) => result.error)) report.chart_errors.push({ status: response.status(), message: body.message, errors: body.errors });
      } catch { if (response.status() >= 400) report.chart_errors.push({ status: response.status(), non_json: true }); }
    });
    board.setDefaultTimeout(30_000);
    await board.waitForURL(`${analytics}/superset/dashboard/**`);
    await expect(board.getByRole('table')).toHaveCount(5, { timeout: 15_000 });
    await expect(board.getByRole('alert')).toHaveCount(0);
    await expect(board.getByRole('link', { name: 'Open activity', exact: true }).first()).toBeVisible();
    expect(await board.evaluate(() => window.opener === null)).toBe(true);
    report.checks[`starter_renders_and_opener_removed_${label}`] = true;
    const id = new URL(board.url()).pathname.split('/')[3];
    const marker = `Private web ${label} ${Date.now()}`;
    await board.getByRole('button', { name: 'Edit dashboard', exact: true }).click();
    await title(board, 'Dashboard title', marker);
    await board.getByRole('button', { name: 'Save', exact: true }).click();
    await board.getByRole('button', { name: 'Edit dashboard', exact: true }).waitFor();
    await board.reload();
    await expect(board.getByRole('textbox', { name: 'Dashboard title' })).toHaveValue(marker);
    report.checks[`edit_reopen_private_board_${label}`] = true;
    await board.getByRole('button', { name: 'Menu actions trigger', exact: true }).click();
    await board.getByRole('button', { name: 'Save as', exact: true }).click();
    const duplicate = board.getByRole('dialog', { name: 'Save dashboard', exact: true });
    await duplicate.getByRole('textbox').fill(marker + ' copy');
    await duplicate.getByRole('checkbox', { name: 'also copy (duplicate) charts', exact: true }).check();
    const [copied] = await Promise.all([
      board.waitForResponse((response) => /\/api\/v1\/dashboard\/[^/]+\/copy\/?$/.test(response.url()) && response.request().method() === 'POST'),
      duplicate.getByRole('button', { name: 'Save', exact: true }).click(),
    ]);
    expect(copied.status()).toBe(200);
    await expect(duplicate).toHaveCount(0);
    await board.waitForURL((url) => url.pathname !== `/superset/dashboard/${id}/`);
    await expect(board.getByRole('textbox', { name: 'Dashboard title' })).toHaveValue(marker + ' copy');
    const copyId = new URL(board.url()).pathname.split('/')[3];
    expect(copyId).not.toBe(id);
    report.checks[`duplicate_board_and_charts_${label}`] = true;
    await board.goto(`${analytics}/dashboard/list/`);
    const copyRow = board.getByRole('row').filter({ hasText: marker + ' copy' });
    await copyRow.waitFor();
    await copyRow.getByRole('button', { name: /delete/i }).click();
    const deletion = board.getByRole('dialog');
    await deletion.getByRole('textbox').fill('DELETE');
    await deletion.getByRole('textbox').press('Tab');
    const [deleted] = await Promise.all([
      board.waitForResponse((response) => response.url().endsWith(`/api/v1/dashboard/${copyId}`) && response.request().method() === 'DELETE'),
      deletion.getByRole('button', { name: /delete/i }).click({ force: true }),
    ]);
    expect(deleted.status()).toBe(200);
    await expect(deletion).toHaveCount(0);
    await expect(copyRow).toHaveCount(0);
    report.checks[`delete_own_board_${label}`] = true;
    await board.goto(`${analytics}/superset/dashboard/${id}/`);
    await page.getByLabel('Default board', { exact: true }).selectOption(id);
    await expect(page.getByLabel('Default board', { exact: true })).toHaveValue(id);
    await page.reload();
    await page.getByRole('button', { name: 'Insights', exact: true }).click();
    await expect(page.getByLabel('Default board', { exact: true })).toHaveValue(id);
    report.checks[`default_survives_web_reload_${label}`] = true;
    const links = board.getByRole('link', { name: 'Open activity', exact: true });
    const href = await links.first().getAttribute('href');
    const link = new URL(href);
    expect(link.searchParams.get('owner')).toBe(createHash('sha256').update(account.id).digest('hex'));
    const detail = await context.newPage();
    await detail.goto(href);
    await expect(detail.getByText('SELECTED ACTIVITY', { exact: true })).toBeVisible();
    report.checks[`owner_activity_deep_link_${label}`] = true;
    await detail.close();
    const sqlOpened = page.waitForEvent('popup');
    await page.getByRole('button', { name: 'SQL Lab', exact: true }).click();
    const sql = current = await sqlOpened;
    sql.on('pageerror', (error) => report.browser_errors.push(String(error)));
    sql.on('response', (response) => {
      if (!/\/(?:tabstateview|api\/v1\/saved_query)\b/.test(response.url()) || response.status() < 400) return;
      report.browser_errors.push({ path: new URL(response.url()).pathname, status: response.status() });
    });
    sql.setDefaultTimeout(30_000);
    await sql.waitForURL(`${analytics}/sqllab/**`);
    await sql.getByRole('tablist').first().waitFor();
    const add = sql.getByRole('tab', { name: 'Add a new tab plus-circle', exact: true });
    if (await add.count()) await add.click();
    await sql.locator('.ace_editor').click();
    await sql.keyboard.press(process.platform === 'darwin' ? 'Meta+A' : 'Control+A');
    await sql.keyboard.insertText('SELECT count(*) AS retained_activities FROM analytics.activities');
    await sql.getByRole('button', { name: 'caret-right Run', exact: true }).click();
    await expect(sql.getByRole('grid')).toContainText(String((fixture.activity_counts || { A: 4, B: 2 })[label]));
    report.checks[`real_sql_lab_owner_grain_${label}`] = true;
    await sql.getByRole('button', { name: 'Save', exact: true }).click();
    const saveQuery = sql.getByRole('dialog', { name: 'save Save query' });
    await saveQuery.getByRole('textbox').first().fill(marker + ' SQL');
    const saveAsNew = saveQuery.getByRole('button', { name: 'Save as new', exact: true });
    const [savedResponse] = await Promise.all([
      sql.waitForResponse((response) => response.url().endsWith('/api/v1/saved_query/') && response.request().method() === 'POST'),
      (await saveAsNew.count() ? saveAsNew : saveQuery.getByRole('button', { name: 'Save', exact: true })).click(),
    ]);
    const savedQuery = await savedResponse.json();
    expect(savedQuery.id).toBeGreaterThan(0);
    await expect(saveQuery).toHaveCount(0);
    await sql.goto(`${analytics}/sqllab?savedQueryId=${savedQuery.id}`);
    await expect(sql.locator('.ace_editor')).toContainText('SELECT count(*) AS retained_activities', { timeout: 60_000 });
    report.checks[`save_reopen_sql_${label}`] = true;
    await sql.goto(`${analytics}/chart/add`);
    await sql.getByRole('combobox').first().click();
    await sql.getByText('activities', { exact: true }).click();
    await sql.getByText('Table', { exact: true }).last().click();
    await sql.getByRole('button', { name: 'Create new chart', exact: true }).click();
    await sql.waitForURL('**/explore/**');
    await sql.getByText('Raw records', { exact: true }).click();
    await sql.getByText('Drop columns here or click', { exact: true }).click();
    await sql.getByRole('combobox', { name: 'Columns and metrics' }).click();
    await sql.getByText('activity_id', { exact: true }).last().click();
    await sql.getByRole('tooltip').getByRole('button', { name: 'Save', exact: true }).click();
    await sql.getByRole('button', { name: 'Create chart', exact: true }).click();
    await expect(sql.getByRole('table')).toContainText('activity_id');
    await sql.getByRole('button', { name: 'save Save', exact: true }).click();
    const saveChart = sql.getByRole('dialog', { name: 'Save chart' });
    await saveChart.getByRole('textbox', { name: 'Name', exact: true }).fill(marker + ' visual');
    const target = saveChart.getByRole('combobox', { name: 'Select a dashboard' });
    await target.click(); await target.fill(marker);
    await sql.locator('.ant-select-item-option').filter({ hasText: marker }).last().click();
    const [createdChart] = await Promise.all([
      sql.waitForResponse((response) => response.url().endsWith('/api/v1/chart/') && response.request().method() === 'POST'),
      saveChart.getByRole('button', { name: 'Save & go to dashboard', exact: true }).click(),
    ]);
    const chart = await createdChart.json();
    expect(chart.id).toBeGreaterThan(0);
    await sql.waitForURL(`${analytics}/superset/dashboard/**`);
    await expect(sql.getByRole('table')).toHaveCount(6);
    await sql.goto(`${analytics}/explore/?slice_id=${chart.id}`);
    await expect(sql.getByRole('table')).toContainText('activity_id');
    await title(sql, 'Chart title', marker + ' visual edited');
    await sql.getByRole('button', { name: 'save Save', exact: true }).click();
    await sql.getByRole('dialog', { name: 'Save chart' }).getByRole('button', { name: 'Save', exact: true }).click();
    await expect(sql.getByRole('dialog', { name: 'Save chart' })).toHaveCount(0);
    await sql.reload();
    await expect(sql.getByRole('textbox', { name: 'Chart title' })).toHaveValue(marker + ' visual edited');
    report.checks[`visual_chart_create_compose_edit_reopen_${label}`] = true;
    report.objects[label] = { board: id, marker, activity_link: href };
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(page.getByRole('heading', { name: 'Insights', exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: screenshot(`insights-mobile-${label}`) });
    report.checks[`responsive_web_entry_${label}`] = true;
  }
  for (const [label, other] of [['A', 'B'], ['B', 'A']]) {
    const page = current = pages[label];
    const response = await page.context().request.get(`${analytics}/api/v1/dashboard/${report.objects[other].board}`);
    expect([403, 404]).toContain(response.status());
    report.checks[`foreign_board_denied_${label}`] = true;
    await page.goto(report.objects[other].activity_link);
    await expect(page.getByRole('alert').filter({ hasText: 'belongs to another account' })).toBeVisible();
    await expect(page.getByText('SELECTED ACTIVITY', { exact: true })).toHaveCount(0);
    report.checks[`foreign_deep_link_withheld_${label}`] = true;
  }
  const states = current = pages.B;
  let mode = 'empty';
  await states.route('**/api/analytics', (route) => route.fulfill(mode === 'error'
    ? { status: 503, json: { detail: 'Synthetic analytics outage' } }
    : { json: { state: mode === 'pending' ? 'refreshing' : 'ready', dataset_id: null, default_dashboard_id: null, boards: [] } }));
  await states.getByRole('button', { name: 'Insights', exact: true }).click();
  await expect(states.getByText('No boards yet.', { exact: false })).toBeVisible();
  report.checks.empty_board_state = true;
  mode = 'pending';
  await states.getByRole('button', { name: 'Activities', exact: true }).click();
  await states.getByRole('button', { name: 'Insights', exact: true }).click();
  await expect(states.getByRole('button', { name: 'Open Insights', exact: true })).toBeDisabled();
  await expect(states.getByText('Your data is refreshing.', { exact: false })).toBeVisible();
  report.checks.stale_state_withholds_authoring = true;
  mode = 'error';
  await expect(states.getByRole('alert').filter({ hasText: 'Synthetic analytics outage' })).toBeVisible({ timeout: 10_000 });
  await expect(states.getByRole('button', { name: 'Open Insights', exact: true })).toBeDisabled();
  mode = 'empty';
  await states.getByRole('button', { name: 'Retry Insights', exact: true }).click();
  await expect(states.getByText('No boards yet.', { exact: false })).toBeVisible();
  report.checks.error_retry_recovers = true;
  await states.unroute('**/api/analytics');
  const logout = current = pages.A;
  await logout.goto(web);
  await logout.getByRole('button', { name: 'Insights', exact: true }).click();
  await expect(logout.getByRole('button', { name: 'Open Insights', exact: true })).toBeEnabled();
  let release, captured;
  const gate = new Promise((resolve) => { release = resolve; });
  const held = new Promise((resolve) => { captured = resolve; });
  await logout.route('**/api/analytics/handoff', async (route) => {
    const response = await route.fetch(); captured(); await gate;
    try { await route.fulfill({ response }); } catch { /* The logged-out page may abort the old request. */ }
  });
  const pendingPopup = logout.waitForEvent('popup');
  await logout.getByRole('button', { name: 'Open Insights', exact: true }).click();
  const stalePopup = await pendingPopup;
  await held;
  await logout.getByRole('button', { name: 'Sign out', exact: true }).click();
  await expect(logout.getByRole('button', { name: 'Sign in', exact: true })).toBeVisible();
  release();
  await expect.poll(() => stalePopup.isClosed()).toBe(true);
  report.checks.logout_fences_delayed_handoff = true;
  expect((await logout.context().request.get(`${analytics}/api/v1/dashboard/${report.objects.A.board}`)).status()).toBe(401);
  expect((await states.context().request.get(`${analytics}/api/v1/dashboard/${report.objects.B.board}`)).status()).toBe(200);
  report.checks.logout_revokes_analytics_without_affecting_other_account = true;
  report.checks.completed = true;
} catch (error) {
  report.checks.completed = false;
  report.failures.push(String(error));
  if (current) { console.log(await current.locator('body').ariaSnapshot()); await current.screenshot({ path: screenshot('failure') }); }
  process.exitCode = 1;
} finally {
  report.probe_passed = Object.values(report.checks).every(Boolean);
  await writeFile(output, JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report, null, 2));
  await browser.close();
}
