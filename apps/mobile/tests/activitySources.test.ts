import assert from 'node:assert/strict';
import test from 'node:test';
import { ApiError } from '../src/api/client';
import type { SessionStore } from '../src/api/client';
import { createActivitySourceApi } from '../src/api/activitySources';
import type { ActivitySourceApi } from '../src/api/activitySources';
import { ActivitySourceStore } from '../src/activitySourceStore';
import type { ActivitySourcePage } from '../src/api/generated';

function session(initial = 'owner-token'): SessionStore {
  let token: string | null = initial;
  return { async getToken() { return token; }, async setToken(value) { token = value; }, async clear() { token = null; }, async clearIfCurrent(value) { if (token !== value) return false; token = null; return true; } };
}
function page(activityId: string, sourceId: string, pageNumber = 1, total = 1): ActivitySourcePage {
  return { activity_id: activityId, items: sourceId ? [{ id: sourceId, source_kind: 'fit', created_at: '2026-10-12T12:00:00Z' }] : [], page: pageNumber, page_size: 20, total };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function fakeApi(overrides: Partial<ActivitySourceApi> = {}): ActivitySourceApi {
  return { async listActivitySources(id, input) { return page(id, 'source-1', input.page); }, async deleteActivitySource() {}, ...overrides };
}

test('source API pages with exact BIGINT string IDs and authenticated source removal', async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const api = createActivitySourceApi({ baseUrl: 'https://api.test/', sessionStore: session(), fetchImpl: async (url, init) => {
    calls.push({ url: String(url), init });
    return calls.length === 1 ? Response.json(page('-9007199254740993', '9007199254740997')) : new Response(null, { status: 204 });
  } });
  const signal = new AbortController().signal;
  const result = await api.listActivitySources('-9007199254740993', { page: 2, pageSize: 20 }, signal);
  await api.deleteActivitySource('-9007199254740993', '9007199254740997', signal);
  const url = new URL(calls[0]!.url);
  assert.equal(url.pathname, '/api/activities/-9007199254740993/sources');
  assert.equal(url.searchParams.get('page'), '2'); assert.equal(url.searchParams.get('page_size'), '20');
  assert.equal(result.items[0]?.id, '9007199254740997');
  assert.equal(calls[1]?.url, 'https://api.test/api/activities/-9007199254740993/sources/9007199254740997');
  assert.equal(calls[1]?.init?.method, 'DELETE');
  assert.equal(new Headers(calls[1]?.init?.headers).get('Authorization'), 'Bearer owner-token');
});

test('loads and appends bounded pages, rejecting mismatched activity or page echoes', async () => {
  const api = fakeApi({ async listActivitySources(id, input) { return page(id, `source-${input.page}`, input.page, 21); } });
  const store = new ActivitySourceStore(api);
  assert.equal(await store.load('activity-1'), true);
  assert.equal(await store.loadMore(), true);
  assert.deepEqual(store.getState().items.map((item) => item.id), ['source-1', 'source-2']);
  assert.equal(store.getState().total, 21);

  const bad = new ActivitySourceStore(fakeApi({ async listActivitySources(id) { return page(`${id}-wrong`, 'source-1'); } }));
  assert.equal(await bad.load('activity-1'), false);
  assert.equal(bad.getState().status, 'error');
  assert.match(bad.getState().error ?? '', /response was invalid/);
});

test('old list response cannot replace a newly selected activity', async () => {
  const old = deferred<ActivitySourcePage>();
  const store = new ActivitySourceStore(fakeApi({ listActivitySources(id) {
    return id === 'activity-a' ? old.promise : Promise.resolve(page(id, 'source-b'));
  } }));
  const pending = store.load('activity-a');
  await store.load('activity-b');
  old.resolve(page('activity-a', 'private-source-a'));
  assert.equal(await pending, false);
  assert.equal(store.getState().activityId, 'activity-b');
  assert.deepEqual(store.getState().items.map((item) => item.id), ['source-b']);
});

test('clear selection fences delayed 404 and never calls missing-selection callback', async () => {
  const pendingList = deferred<ActivitySourcePage>();
  const store = new ActivitySourceStore(fakeApi({ listActivitySources() { return pendingList.promise; } }));
  const pending = store.load('activity-a');
  store.clearSelection();
  pendingList.reject(new ApiError('http', 'Activity not found.', 404));
  assert.equal(await pending, false);
  assert.equal(store.getState().activityId, null);
  assert.equal(store.getState().activityMissing, false);
});

test('same-account accepted removal refreshes provenance and notifies even after selection changes', async () => {
  const removal = deferred<void>();
  const refreshed: string[] = [];
  const queried: string[] = [];
  const store = new ActivitySourceStore(fakeApi({
    async listActivitySources(id) { queried.push(id); return page(id, id === 'activity-a' ? 'source-a' : 'source-b'); },
    deleteActivitySource() { return removal.promise; },
  }), (id) => { refreshed.push(id); });
  await store.load('activity-a');
  const pending = store.remove('source-a');
  await store.load('activity-b');
  removal.resolve();
  assert.equal(await pending, false);
  assert.deepEqual(refreshed, ['activity-a']);
  assert.deepEqual(queried, ['activity-a', 'activity-b', 'activity-b']);
  assert.equal(store.getState().activityId, 'activity-b');
  assert.equal(store.getState().items[0]?.id, 'source-b');
});

test('final-source removal marks current activity missing and accepted callback still fires', async () => {
  const refreshed: string[] = [];
  const store = new ActivitySourceStore(fakeApi({
    async listActivitySources(id) {
      if (id === 'activity-a' && removed) throw new ApiError('http', 'Activity not found.', 404);
      return page(id, 'source-a');
    },
    async deleteActivitySource() { removed = true; },
  }), (id) => { refreshed.push(id); });
  let removed = false;
  await store.load('activity-a');
  assert.equal(await store.remove('source-a'), true);
  assert.equal(store.getState().activityMissing, true);
  assert.deepEqual(refreshed, ['activity-a']);
});

test('selection changes preserve removal lock and late removal errors cannot replace current selection', async () => {
  const removal = deferred<void>();
  const store = new ActivitySourceStore(fakeApi({ deleteActivitySource() { return removal.promise; } }));
  await store.load('activity-a');
  const pending = store.remove('source-1');
  await store.load('activity-b');
  assert.equal(store.getState().removingSourceId, 'source-1');
  assert.equal(await store.remove('source-1'), false);
  removal.reject(new Error('old removal failed'));
  assert.equal(await pending, false);
  assert.equal(store.getState().activityId, 'activity-b');
  assert.equal(store.getState().error, null);
  assert.equal(store.getState().removingSourceId, null);
});

test('account reset fences old accepted removal from new-account callbacks and state', async () => {
  const removal = deferred<void>();
  let refreshed = 0;
  const store = new ActivitySourceStore(fakeApi({ deleteActivitySource() { return removal.promise; } }), () => { refreshed++; });
  await store.load('activity-a');
  const pending = store.remove('source-1');
  store.reset();
  removal.resolve();
  assert.equal(await pending, false);
  assert.equal(refreshed, 0);
  assert.equal(store.getState().activityId, null);
});

test('401 clears private source list and requires a new sign-in', async () => {
  const store = new ActivitySourceStore(fakeApi({ async listActivitySources() { throw new ApiError('sign-in-required', 'Session expired.', 401); } }));
  await store.load('activity-a');
  assert.equal(store.getState().activityId, null);
  assert.equal(store.getState().items.length, 0);
  assert.equal(store.getState().status, 'sign-in-required');
});

test('401 during source removal clears private provenance and fences accepted callbacks', async () => {
  let refreshed = 0;
  const store = new ActivitySourceStore(fakeApi({ async deleteActivitySource() {
    throw new ApiError('sign-in-required', 'Session expired.', 401);
  } }), () => { refreshed++; });
  await store.load('activity-a');
  assert.equal(await store.remove('source-1'), false);
  assert.equal(store.getState().activityId, null);
  assert.equal(store.getState().items.length, 0);
  assert.equal(store.getState().status, 'sign-in-required');
  assert.equal(refreshed, 0);
});
