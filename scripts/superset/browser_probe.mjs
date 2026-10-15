// Synthetic full-UI acceptance against the pinned disposable pilot.
import { chromium, expect } from '../../apps/web/node_modules/@playwright/test/index.mjs';
import { writeFile } from 'node:fs/promises';

const base = 'http://127.0.0.1:8090';
const outputIndex = process.argv.indexOf('--output');
const output = outputIndex < 0 ? '/tmp/d43-browser.json' : process.argv[outputIndex + 1];
const browser = await chromium.launch({ channel: process.env.PILOT_BROWSER_CHANNEL || 'chrome', headless: true });
const report = { version: '6.1.0', checks: {}, objects: {}, failures: [] };
const pages = {};
const pending = new Set();
let current;

async function editTitle(page, name, value) {
  const title = page.getByRole('textbox', { name });
  await title.click();
  await title.fill(value);
  await title.press('Tab');
}

async function author(name) {
  const page = await browser.newPage();
  current = pages[name] = page;
  page.setDefaultTimeout(20_000);
  const rejected = [];
  page.on('response', response => {
    if (response.status() === 429) rejected.push(response.url());
  });
  page.on('request', request => pending.add(request.url()));
  page.on('requestfinished', request => pending.delete(request.url()));
  page.on('requestfailed', request => pending.delete(request.url()));
  const marker = `Browser ${name} ${Date.now()}`;
  const board = `${marker} board`;
  await page.goto(`${base}/pilot/`);
  await page.getByRole('button', { name: `Continue as ${name}` }).click();
  await page.waitForURL('**/superset/welcome/');
  await page.getByRole('button', { name: 'Dashboards', exact: true }).click();
  await page.getByText('Dashboard', { exact: true }).click();
  await page.waitForURL('**/superset/dashboard/**');
  await page.getByRole('button', { name: 'Save', exact: true }).waitFor();
  await editTitle(page, 'Dashboard title', board);
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await page.getByRole('button', { name: 'Edit dashboard', exact: true }).waitFor();
  const boardUrl = page.url();
  await page.getByRole('button', { name: 'Charts', exact: true }).click();
  await page.getByText('Chart', { exact: true }).click();
  await page.waitForURL('**/chart/add');
  await page.getByRole('combobox').first().click();
  await page.getByText('activities', { exact: true }).click();
  await page.getByText('Table', { exact: true }).last().click();
  await page.getByRole('button', { name: 'Create new chart', exact: true }).click();
  await page.waitForURL('**/explore/**');
  await page.getByText('Raw records', { exact: true }).click();
  await page.getByText('Drop columns here or click', { exact: true }).click();
  await page.getByRole('combobox', { name: 'Columns and metrics' }).click();
  await page.getByText('distance_m', { exact: true }).last().click();
  await page.getByRole('tooltip').getByRole('button', { name: 'Save', exact: true }).click();
  await page.getByRole('button', { name: 'Create chart', exact: true }).click();
  await expect(page.getByRole('table')).toContainText(name === 'A' ? '5000' : '9000');
  await page.getByRole('button', { name: 'save Save', exact: true }).click();
  const save = page.getByRole('dialog', { name: 'Save chart' });
  await save.getByRole('textbox', { name: 'Name', exact: true }).fill(marker);
  await save.getByRole('combobox', { name: 'Select a dashboard' }).click();
  await save.getByRole('combobox', { name: 'Select a dashboard' }).fill(board);
  await expect(page.getByRole('option', { name: board, exact: true }).last()).toHaveText(new URL(boardUrl).pathname.split('/')[3]);
  await page.locator('.ant-select-item-option').filter({ hasText: board }).last().click();
  const created = page.waitForResponse(response => response.url().endsWith('/api/v1/chart/') && response.request().method() === 'POST');
  await save.getByRole('button', { name: 'Save & go to dashboard', exact: true }).click();
  const chart = await (await created).json();
  expect(chart.id).toBeGreaterThan(0);
  await page.waitForURL('**/superset/dashboard/**');
  await expect(page.getByRole('table')).toContainText(name === 'A' ? '5000' : '9000');
  report.checks[`create_chart_and_dashboard_${name}`] = true;

  await page.goto(`${base}/explore/?slice_id=${chart.id}`);
  await expect(page.getByRole('table')).toContainText(name === 'A' ? '5000' : '9000');
  await editTitle(page, 'Chart title', `${marker} edited`);
  await page.getByRole('button', { name: 'save Save', exact: true }).click();
  await page.getByRole('dialog', { name: 'Save chart' }).getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByRole('dialog', { name: 'Save chart' })).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole('textbox', { name: 'Chart title' })).toHaveValue(`${marker} edited`);
  report.checks[`edit_reopen_chart_${name}`] = true;

  await page.goto(boardUrl);
  await expect(page.getByRole('table')).toContainText(name === 'A' ? '5000' : '9000');
  await page.getByRole('button', { name: 'Edit dashboard', exact: true }).click();
  await editTitle(page, 'Dashboard title', `${board} edited`);
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await page.getByRole('button', { name: 'Edit dashboard', exact: true }).waitFor();
  await page.reload();
  await expect(page.getByRole('textbox', { name: 'Dashboard title' })).toHaveValue(`${board} edited`);
  await expect(page.getByRole('table')).toContainText(name === 'A' ? '5000' : '9000');
  expect(rejected).toEqual([]);
  report.checks[`edit_reopen_dashboard_${name}`] = true;
  report.checks[`metadata_admission_${name}`] = true;
  report.objects[name] = { chart: chart.id, dashboard: Number(new URL(boardUrl).pathname.split('/')[3]), marker };
  await page.screenshot({ path: output.replace(/\.json$/, `-${name}.png`) });
  await page.goto(`${base}/sqllab/`);
  const addTab = page.getByRole('tab', { name: 'Add a new tab plus-circle', exact: true });
  await page.getByRole('tablist').first().waitFor({ timeout: 60_000 });
  if (await addTab.count()) await addTab.click();
  await page.getByRole('button', { name: /Run/ }).first().waitFor({ timeout: 60_000 });
  const editor = page.locator('.ace_editor');
  await editor.click();
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+A' : 'Control+A');
  await page.keyboard.insertText('SELECT activity_id, distance_m FROM analytics.activities ORDER BY activity_id');
  await page.getByRole('button', { name: 'caret-right Run', exact: true }).click();
  await expect(page.getByRole('grid')).toContainText(name === 'A' ? '5000' : '9000', { timeout: 15_000 });
  report.checks[`execute_sql_${name}`] = true;
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  const saveQuery = page.getByRole('dialog', { name: 'save Save query' });
  await saveQuery.getByRole('textbox').first().fill(`${marker} SQL`);
  const saved = page.waitForResponse(response => response.url().endsWith('/api/v1/saved_query/') && response.request().method() === 'POST');
  await saveQuery.getByRole('button', { name: 'Save as new', exact: true }).click();
  const query = await (await saved).json();
  expect(query.id).toBeGreaterThan(0);
  report.objects[name].saved_query = query.id;
  await page.goto(`${base}/sqllab?savedQueryId=${query.id}`);
  await expect(page.locator('.ace_editor')).toContainText('SELECT activity_id, distance_m', { timeout: 60_000 });
  await page.getByRole('button', { name: 'caret-right Run', exact: true }).click();
  await expect(page.getByRole('grid')).toContainText(name === 'A' ? '5000' : '9000', { timeout: 15_000 });
  expect(rejected).toEqual([]);
  report.checks[`save_reopen_sql_${name}`] = true;
}

try {
  for (const name of ['A', 'B']) await author(name);
  for (const [name, other] of [['A', 'B'], ['B', 'A']]) {
    const page = current = pages[name];
    for (const resource of ['chart', 'dashboard', 'saved_query']) {
      const response = await page.request.get(`${base}/api/v1/${resource}/${report.objects[other][resource]}`);
      expect([403, 404]).toContain(response.status());
      expect(await response.text()).not.toContain(report.objects[other].marker);
      report.checks[`foreign_${resource}_denied_${name}`] = true;
    }
  }
  report.checks.probe_completed = true;
} catch (error) {
  report.checks.probe_completed = false;
  report.failures.push(String(error));
  console.log('Pending requests', [...pending]);
  if (current) {
    console.log(await current.locator('body').ariaSnapshot());
    await current.screenshot({ path: output.replace(/\.json$/, '-failure.png') });
  }
  process.exitCode = 1;
} finally {
  report.probe_passed = Object.values(report.checks).every(Boolean);
  await writeFile(output, `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify(report, null, 2));
  await browser.close();
}
