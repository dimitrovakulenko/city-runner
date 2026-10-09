import test from 'node:test';
import assert from 'node:assert/strict';
import { BrowserSessions, SESSION_KEY } from '../src/session';
import { BrowserFiles } from '../src/files';
import { createRuntime, createBrowserImportApi } from '../src/runtime';
import { restoreDevelopmentSession } from '../src/development';

function storage() {
  const values = new Map<string, string>();
  return { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); }, removeItem: (key: string) => { values.delete(key); } };
}

test('development login requires explicit enablement and loopback, preserving existing sessions', async () => {
  const sessions = new BrowserSessions(storage()); let requests = 0;
  const request = async () => { requests++; return Response.json({ session_token: 'test-session' }); };
  await restoreDevelopmentSession(sessions, false, '127.0.0.1', request);
  await restoreDevelopmentSession(sessions, true, 'app.example.com', request);
  assert.equal(requests, 0);
  await restoreDevelopmentSession(sessions, true, '127.0.0.1', request);
  assert.equal(await sessions.getToken(), 'test-session'); assert.equal(requests, 1);
  await restoreDevelopmentSession(sessions, true, 'localhost', request); assert.equal(requests, 1);
});

test('a delayed development login never replaces a newer account', async () => {
  const sessions = new BrowserSessions(storage()); let finish!: (response: Response) => void;
  const pending = restoreDevelopmentSession(sessions, true, '127.0.0.1', () => new Promise<Response>((resolve) => { finish = resolve; }));
  await Promise.resolve(); await sessions.setToken('new-account'); finish(Response.json({ session_token: 'test-session' }));
  await pending; assert.equal(await sessions.getToken(), 'new-account');
});

test('tab session compare-and-clear cannot remove a replacement account', async () => {
  const backing = storage(); const sessions = new BrowserSessions(backing); const changes: Array<string | null> = [];
  sessions.subscribe((token) => changes.push(token));
  await sessions.setToken('old'); await sessions.setToken('new');
  assert.equal(await sessions.clearIfCurrent('old'), false);
  assert.equal(await sessions.getToken(), 'new');
  assert.equal(backing.getItem(SESSION_KEY), 'new');
  assert.equal(await sessions.clearIfCurrent('new'), true);
  assert.deepEqual(changes, ['old', 'new', null]);
});

test('cancelled conditional commit removes its own write and preserves a newer token', async () => {
  const sessions = new BrowserSessions(storage()); let current = true;
  sessions.subscribe((token) => { if (token === 'cancelled') { current = false; void sessions.setToken('replacement'); } });
  assert.equal(await sessions.setTokenIfCurrent('cancelled', () => current), false);
  assert.equal(await sessions.getToken(), 'replacement');
  assert.equal(await sessions.setTokenIfCurrent('ignored', () => false), false);
});

test('browser multipart preserves original File bytes and requires explicit reselection after clear', async () => {
  const files = new BrowserFiles(); const file = new File(['synthetic GPX bytes'], 'run.gpx', { type: 'application/gpx+xml' });
  const selection = files.register(file); const body = files.form(selection).get('file') as File;
  assert.equal(body.name, 'run.gpx'); assert.equal(await body.text(), 'synthetic GPX bytes');
  files.clear(); assert.throws(() => files.form(selection), /Select this file again/);
});

test('browser upload uses a File multipart body, account bearer and original string IDs', async () => {
  const sessions = new BrowserSessions(storage()); await sessions.setToken('alice');
  const paths: string[] = [];
  const files = new BrowserFiles();
  const api = createBrowserImportApi({ sessionStore: sessions, baseUrl: 'http://example.test', fetchImpl: async (url, init) => {
    paths.push(String(url)); assert.equal(new Headers(init?.headers).get('Authorization'), 'Bearer alice');
    assert.equal(new Headers(init?.headers).get('Content-Type'), null);
    assert.equal(await ((init?.body as FormData).get('file') as File).text(), 'FIT bytes');
    return Response.json({ id: '0', name: 'run.fit', format: 'fit', status: 'queued', source_id: '9007199254740993', activity_id: null, duplicate: false, error: null });
  } }, files);
  const selection = files.register(new File(['FIT bytes'], 'run.fit'));
  assert.equal((await api.uploadItem('9007199254740995', '0', selection)).source_id, '9007199254740993');
  assert.deepEqual(paths, ['http://example.test/api/import-batches/9007199254740995/items/0/upload']);
  assert.throws(() => files.form(selection), /Select this file again/);
});

test('session replacement aborts old requests and clears every private store and File reference', async () => {
  const sessions = new BrowserSessions(storage()); await sessions.setToken('alice');
  let finish!: (response: Response) => void;
  const runtime = createRuntime(sessions, '', () => new Promise<Response>((resolve) => { finish = resolve; }));
  runtime.planner.setName('Alice private synthetic route'); runtime.planner.addWaypoint([4, 50]);
  const local = runtime.files.register(new File(['private synthetic'], 'run.gpx'));
  const pending = runtime.activities.loadPage(); await Promise.resolve(); await Promise.resolve();
  await sessions.setToken('bob');
  finish(Response.json({ items: [{ id: '1', name: 'Alice private activity', date: 'unknown', type: 'unknown', processed: false, unmapped_points: 0 }], page: 1, page_size: 20, total: 1 }));
  await pending;
  assert.deepEqual(runtime.activities.getState().items, []);
  assert.deepEqual(runtime.imports.getState().batches, []);
  assert.equal(runtime.explore.getState().map, null);
  assert.equal(runtime.planner.getState().name, ''); assert.deepEqual(runtime.planner.getState().waypoints, []);
  assert.throws(() => runtime.files.form(local), /Select this file again/);
});

test('runtime acknowledges status only after views refresh and unchanged polls skip those reads', async () => {
  const sessions = new BrowserSessions(storage()); await sessions.setToken('alice');
  const since: Array<string | null> = []; let token = 'v1'; let activityReads = 0; let failList = false;
  const files = { queued: 0, processing: 0, imported: 0, failed: 0, unavailable: 0 };
  const runtime = createRuntime(sessions, 'http://example.test', async (input) => {
    const url = new URL(String(input));
    if (url.pathname === '/api/sync/status') {
      const previous = url.searchParams.get('since'); since.push(previous);
      if (previous === token) return new Response(null, { status: 204 });
      return Response.json({ change_token: token, activity_count: 0, oldest_activity_date: null, last_import_at: null,
        files: { gpx: files, fit: files }, coverage: { ready: 0, pending: 0, failed: 0, unavailable: 0 },
        batches: { waiting: 0, accepted: 0, duplicates: 0, deleted: 0, stopped: 0 }, providers: [] });
    }
    if (url.pathname === '/api/activities/filters') return Response.json({ activity_types: [], types_truncated: false });
    if (url.pathname === '/api/activities') {
      activityReads++;
      if (failList) { failList = false; return Response.json({ detail: 'Temporary failure' }, { status: 503 }); }
      return Response.json({ items: [], page: 1, page_size: 20, total: 0 });
    }
    if (url.pathname === '/api/progress') return Response.json({ state: 'unsupported-geography', rule: 'normal', datasets: [], datasets_truncated: false, unmapped_points: 0, pending_imports: 0 });
    if (url.pathname === '/api/import-batches') return Response.json({ items: [], page: 1, page_size: 20, total: 0 });
    throw new Error(`Unexpected request: ${url.pathname}`);
  });
  runtime.planner.setName('Keep my draft'); runtime.sync.setEligible(true);
  assert.equal(await runtime.sync.pollIfDue(10_000), true);
  assert.equal(await runtime.sync.pollIfDue(12_500), true); assert.equal(activityReads, 1);
  token = 'v2'; failList = true;
  assert.equal(await runtime.sync.pollIfDue(15_000), false);
  assert.equal(runtime.sync.getState().data?.change_token, 'v2');
  assert.match(runtime.sync.getState().error ?? '', /views could not refresh/);
  assert.equal(await runtime.sync.pollIfDue(17_500), true);
  assert.deepEqual(since, [null, 'v1', 'v1', 'v1']);
  assert.equal(runtime.planner.getState().name, 'Keep my draft');
  await sessions.setToken('bob');
  assert.equal(runtime.sync.getState().data, null); assert.equal(runtime.sync.getState().eligible, false);
});
