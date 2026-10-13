import assert from 'node:assert/strict';
import test from 'node:test';
import { ApiError } from '../src/api/client';
import type { SessionStore } from '../src/api/client';
import { createCoveragePreparationApi } from '../src/api/coveragePreparation';
import type { CoveragePreparationApi } from '../src/api/coveragePreparation';
import type { CoveragePreparationResponse } from '../src/api/generated';
import { CoveragePreparationStore } from '../src/coveragePreparationStore';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
function sessions(token: string | null): SessionStore {
  let current = token;
  return { async getToken() { return current; }, async setToken(next) { current = next; }, async clear() { current = null; },
    async clearIfCurrent(expected) { if (current !== expected) return false; current = null; return true; } };
}
const snapshot = (overrides: Partial<CoveragePreparationResponse> = {}): CoveragePreparationResponse => ({
  enabled: true, state: 'pending', pending_sources: 1, failed_sources: 0, unsupported_sources: 0,
  cities: [{ osm_relation_id: '123456', name: 'Antwerp', state: 'fetching', error: null }], cities_truncated: false, ...overrides,
});
function api(overrides: Partial<CoveragePreparationApi> = {}): CoveragePreparationApi {
  return { async getStatus() { return snapshot(); }, async retry() {}, ...overrides };
}

test('coverage preparation API uses authenticated status and 204 retry routes', async () => {
  const calls: Array<{ url: string; method: string; authorization: string | null }> = [];
  const client = createCoveragePreparationApi({ baseUrl: 'https://api.test/', sessionStore: sessions('opaque'), fetchImpl: async (url, init) => {
    calls.push({ url: String(url), method: init?.method ?? 'GET', authorization: new Headers(init?.headers).get('Authorization') });
    return calls.length === 1 ? Response.json(snapshot()) : new Response(null, { status: 204 });
  } });
  assert.deepEqual(await client.getStatus(), snapshot());
  await client.retry();
  assert.deepEqual(calls.map(({ url, method, authorization }) => ({ path: new URL(url).pathname, method, authorization })), [
    { path: '/api/coverage-preparation', method: 'GET', authorization: 'Bearer opaque' },
    { path: '/api/coverage-preparation/retry', method: 'POST', authorization: 'Bearer opaque' },
  ]);
});

test('disabled and unsupported preparation values remain distinct and unmodified', async () => {
  const values = [snapshot({ enabled: false, state: 'ready', pending_sources: 0 }),
    snapshot({ state: 'unsupported', unsupported_sources: 2, cities: [] }),
    snapshot({ state: 'failed', failed_sources: 1, cities: [{ osm_relation_id: '789', name: 'Ghent', state: 'failed', error: 'Missing boundary.' }] })];
  let index = 0;
  const store = new CoveragePreparationStore(api({ async getStatus() { return values[index++]!; } }));
  for (const expected of values) {
    assert.equal(await store.refresh(), true);
    assert.deepEqual(store.getState().data, expected);
    assert.equal(store.getState().status, 'ready');
  }
});

test('late status and retry responses are fenced by account reset', async () => {
  const read = deferred<CoveragePreparationResponse>();
  const post = deferred<void>();
  const store = new CoveragePreparationStore(api({ getStatus: () => read.promise, retry: () => post.promise }));
  const oldRead = store.refresh();
  store.reset();
  read.resolve(snapshot());
  assert.equal(await oldRead, false);
  assert.equal(store.getState().data, null);

  const fresh = new CoveragePreparationStore(api({ retry: () => post.promise }));
  await fresh.refresh();
  const oldRetry = fresh.retry();
  assert.equal(fresh.getState().retrying, true);
  fresh.reset();
  post.resolve(undefined);
  assert.equal(await oldRetry, false);
  assert.deepEqual(fresh.getState(), { data: null, status: 'idle', error: null, retrying: false });
});

test('retry is serialized, does not run when disabled, and refreshes accepted work', async () => {
  let posts = 0;
  let reads = 0;
  const wait = deferred<void>();
  const store = new CoveragePreparationStore(api({
    async getStatus() { reads++; return snapshot({ state: reads > 1 ? 'pending' : 'failed', failed_sources: reads > 1 ? 0 : 1 }); },
    async retry() { posts++; await wait.promise; },
  }));
  await store.refresh();
  const retry = store.retry();
  assert.equal(await store.retry(), false);
  assert.equal(store.getState().retrying, true);
  wait.resolve();
  assert.equal(await retry, true);
  assert.equal(posts, 1);
  assert.equal(reads, 2);
  assert.equal(store.getState().data?.failed_sources, 0);

  const disabled = new CoveragePreparationStore(api({ async getStatus() { return snapshot({ enabled: false }); } }));
  await disabled.refresh();
  assert.equal(await disabled.retry(), false);
});

test('accepted retry reports failure when its required status refresh fails', async () => {
  let reads = 0;
  let posts = 0;
  const store = new CoveragePreparationStore(api({
    async getStatus() {
      if (++reads > 1) throw new ApiError('offline', 'Connection lost.');
      return snapshot({ state: 'failed', failed_sources: 1 });
    },
    async retry() { posts++; },
  }));
  await store.refresh();
  assert.equal(await store.retry(), false);
  assert.equal(posts, 1);
  assert.equal(store.getState().status, 'offline');
  assert.equal(store.getState().error, 'Connection lost.');
  assert.equal(store.getState().data?.failed_sources, 1);
  assert.equal(store.getState().retrying, false);
});

test('401 clears cached preparation data and malformed pages fail closed', async () => {
  const store = new CoveragePreparationStore(api());
  await store.refresh();
  const unauthorized = new CoveragePreparationStore(api({ async getStatus() { throw new ApiError('sign-in-required', 'Sign in again.', 401); } }));
  await unauthorized.refresh();
  assert.equal(unauthorized.getState().status, 'sign-in-required');
  assert.equal(unauthorized.getState().data, null);

  const malformed = new CoveragePreparationStore(api({ async getStatus() { return snapshot({ cities: Array.from({ length: 21 }, (_item, index) => ({
    osm_relation_id: String(index), name: 'City', state: 'ready', error: null,
  })) }); } }));
  assert.equal(await malformed.refresh(), false);
  assert.equal(malformed.getState().data, null);
  assert.equal(malformed.getState().status, 'error');

  const invalidCity = new CoveragePreparationStore(api({ async getStatus() { return snapshot({ cities: [
    { osm_relation_id: '0', name: 'City', state: 'ready', error: null },
  ] }); } }));
  assert.equal(await invalidCity.refresh(), false);
  assert.equal(invalidCity.getState().data, null);
  const longName = new CoveragePreparationStore(api({ async getStatus() { return snapshot({ cities: [
    { osm_relation_id: '123456', name: 'x'.repeat(201), state: 'ready', error: null },
  ] }); } }));
  assert.equal(await longName.refresh(), false);
  assert.equal(longName.getState().data, null);
});

test('newer refresh wins when an older status response arrives late', async () => {
  const old = deferred<CoveragePreparationResponse>();
  let first = true;
  const store = new CoveragePreparationStore(api({ getStatus() {
    if (first) { first = false; return old.promise; }
    return Promise.resolve(snapshot({ state: 'ready', pending_sources: 0, cities: [] }));
  } }));
  const earlier = store.refresh();
  assert.equal(await store.refresh(), true);
  old.resolve(snapshot({ state: 'failed', failed_sources: 1 }));
  assert.equal(await earlier, false);
  assert.equal(store.getState().data?.state, 'ready');
});

test('refresh callback can safely be superseded without publishing late error', async () => {
  const old = deferred<CoveragePreparationResponse>();
  const store = new CoveragePreparationStore(api({ getStatus() { return old.promise; } }));
  const refreshing = store.refresh();
  await tick();
  store.reset();
  old.reject(new Error('old account failure'));
  assert.equal(await refreshing, false);
  assert.equal(store.getState().error, null);
});
