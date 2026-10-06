import assert from 'node:assert/strict';
import test from 'node:test';
import type { SessionStore } from '../src/api/client';
import { createCorrectionApi } from '../src/api/corrections';
import type { CorrectionApi } from '../src/api/corrections';
import { CorrectionStore } from '../src/correctionStore';

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((yes) => { resolve = yes; });
  return { promise, resolve };
}
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
function session(initial: string | null): SessionStore {
  let token = initial;
  return { async getToken() { return token; }, async setToken(value) { token = value; }, async clear() { token = null; },
    async clearIfCurrent(value) { if (token !== value) return false; token = null; return true; } };
}
function fakeApi(overrides: Partial<CorrectionApi> = {}): CorrectionApi {
  return { async deleteActivity() {}, async setManualCompletion() {}, async clearManualCompletion() {}, ...overrides };
}

test('correction API sends encoded string IDs, trimmed reason, bearer and caller abort signal', async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const api = createCorrectionApi({ baseUrl: 'https://api.test/', sessionStore: session('opaque'), fetchImpl: async (url, init) => {
    calls.push({ url: String(url), init });
    return new Response(null, { status: 204 });
  } });
  const signal = new AbortController().signal;
  await api.deleteActivity('9007199254740993', signal);
  await api.setManualCompletion('9007199254740995', '9007199254740997', '  surveyed  ', signal);
  await api.clearManualCompletion('9007199254740995', '9007199254740997', signal);
  assert.equal(calls[0]?.url, 'https://api.test/api/activities/9007199254740993');
  assert.equal(calls[0]?.init?.method, 'DELETE');
  assert.equal(new Headers(calls[1]?.init?.headers).get('Authorization'), 'Bearer opaque');
  assert.equal(calls[1]?.url, 'https://api.test/api/streets/9007199254740995/manual-completion?dataset_id=9007199254740997');
  assert.deepEqual(JSON.parse(String(calls[1]?.init?.body)), { reason: 'surveyed' });
  assert.ok(calls[1]?.init?.signal instanceof AbortSignal);
  assert.equal(calls[1]?.init?.signal?.aborted, false);
  assert.equal(calls[2]?.init?.method, 'DELETE');
});

test('account reset aborts delayed token reads before any mutation dispatch', async () => {
  const token = deferred<string | null>();
  const calls: string[] = [];
  const sessionStore: SessionStore = { async getToken() { return token.promise; }, async setToken() {}, async clear() {}, async clearIfCurrent() { return false; } };
  const api = createCorrectionApi({ baseUrl: 'https://api.test', sessionStore, fetchImpl: async (url) => {
    calls.push(String(url)); return new Response(null, { status: 204 });
  } });
  let succeeded = 0;
  const store = new CorrectionStore(api, () => { succeeded++; });
  const pending = store.markComplete('street-1', 'dataset-1', 'verified on foot');
  store.reset();
  token.resolve('account-B-token');
  await pending;
  assert.deepEqual(calls, []);
  assert.equal(succeeded, 0);
  assert.equal(store.getState().pending, null);
});

test('manual reason must be trimmed, nonempty and at most 500 characters', async () => {
  let calls = 0;
  const store = new CorrectionStore(fakeApi({ async setManualCompletion() { calls++; } }), () => {});
  await store.markComplete('s', 'd', '  ');
  assert.match(store.getState().error ?? '', /1 and 500/);
  await store.markComplete('s', 'd', 'x'.repeat(501));
  assert.match(store.getState().error ?? '', /1 and 500/);
  assert.equal(calls, 0);
  await store.markComplete('s', 'd', '  checked  ');
  assert.equal(calls, 1);
});

test('failed deletion is retryable and success invalidates dependent views once', async () => {
  let calls = 0;
  const completed: string[] = [];
  const store = new CorrectionStore(fakeApi({ async deleteActivity() { if (++calls === 1) throw new Error('Could not reach the server.'); } }), (op, id) => completed.push(`${op}:${id}`));
  await store.deleteActivity('act-1');
  assert.equal(store.getState().error, 'Could not reach the server.');
  await store.deleteActivity('act-1');
  assert.deepEqual(completed, ['activity-delete:act-1']);
  assert.equal(store.getState().error, null);
});

test('account reset fences a delayed successful mutation from the old account', async () => {
  const old = deferred<void>();
  let invalidations = 0;
  const store = new CorrectionStore(fakeApi({ deleteActivity() { return old.promise; } }), () => invalidations++);
  const pending = store.deleteActivity('act-1');
  await tick();
  store.reset();
  old.resolve();
  await pending;
  assert.equal(invalidations, 0);
  assert.equal(store.getState().error, null);
});

test('clearing selection feedback suppresses stale errors but successful mutation still refreshes private views', async () => {
  const delayed = deferred<void>();
  let invalidations = 0;
  const store = new CorrectionStore(fakeApi({ async clearManualCompletion() { await delayed.promise; } }), () => invalidations++);
  const pending = store.undoManualCompletion('street-a', 'dataset-a');
  store.clearFeedback();
  delayed.resolve();
  await pending;
  assert.equal(invalidations, 1);
  assert.equal(store.getState().error, null);
  assert.equal(store.getState().pending, null);
});
