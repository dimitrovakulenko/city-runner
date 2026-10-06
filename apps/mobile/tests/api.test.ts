import assert from 'node:assert/strict';
import test from 'node:test';
import { ApiError, createActivityApi, createApiRequest } from '../src/api/client';
import type { SessionStore } from '../src/api/client';

function sessions(initial: string | null): SessionStore & { current: string | null } {
  return {
    current: initial,
    async getToken() { return this.current; },
    async setToken(value) { this.current = value; },
    async clear() { this.current = null; },
    async clearIfCurrent(value) {
      if (this.current !== value) return false;
      this.current = null;
      return true;
    },
  };
}

test('requires a session and preserves large string IDs and track segments', async () => {
  let calls = 0;
  const store = sessions(null);
  const api = createActivityApi({ baseUrl: 'https://example.test/', sessionStore: store, fetchImpl: async () => { calls++; return Response.json({}); } });
  await assert.rejects(api.listActivities(), (error: unknown) => error instanceof ApiError && error.kind === 'sign-in-required');
  assert.equal(calls, 0);

  store.current = 'opaque-token';
  let requested = '';
  const activity = { id: '-9007199254740993', tracks: [[[1, 2]], [[3, 4]]], timestamps: [['a'], [null]], bounds: null };
  const authenticatedApi = createActivityApi({ baseUrl: 'https://example.test', sessionStore: store, fetchImpl: async (url, init) => {
    requested = `${url} ${new Headers(init?.headers).get('Authorization')}`;
    return Response.json(activity);
  } });
  const actual = await authenticatedApi.getActivity(activity.id);
  assert.match(requested, /-9007199254740993 Bearer opaque-token$/);
  assert.equal(actual.id, activity.id);
  assert.deepEqual(actual.tracks, activity.tracks);
  assert.deepEqual(actual.timestamps, activity.timestamps);
});

test('old-token 401 does not replay under or clear a replacement session', async () => {
  const store = sessions('old');
  let calls = 0;
  const api = createActivityApi({ baseUrl: 'https://example.test', sessionStore: store, fetchImpl: async (_url, init) => {
    calls++;
    assert.equal(new Headers(init?.headers).get('Authorization'), 'Bearer old');
    store.current = 'new';
    return new Response(null, { status: 401 });
  } });
  await assert.rejects(api.getActivity('7'), (error: unknown) => error instanceof ApiError && error.kind === 'stale-session');
  assert.equal(calls, 1);
  assert.equal(store.current, 'new');
});

test('delayed old-token success is rejected after account replacement', async () => {
  const store = sessions('account-a');
  let finish!: (response: Response) => void;
  const response = new Promise<Response>((resolve) => { finish = resolve; });
  const api = createActivityApi({ baseUrl: 'https://example.test', sessionStore: store, fetchImpl: async () => response });
  const oldRequest = api.getActivity('7');
  store.current = 'account-b';
  finish(Response.json({ id: '7', name: 'private from A', date: 'today', type: 'running', processed: true, unmapped_points: 0, tracks: [], timestamps: [], bounds: null }));
  await assert.rejects(oldRequest, (error: unknown) => error instanceof ApiError && error.kind === 'stale-session');
  assert.equal(store.current, 'account-b');
});

test('current-token 401 conditionally clears session', async () => {
  const store = sessions('expired');
  const api = createActivityApi({ baseUrl: 'https://example.test', sessionStore: store, fetchImpl: async () => new Response(null, { status: 401 }) });
  await assert.rejects(api.listActivities(), (error: unknown) => error instanceof ApiError && error.kind === 'sign-in-required');
  assert.equal(store.current, null);
});

test('network failures are exposed as retryable offline errors', async () => {
  const api = createActivityApi({ baseUrl: 'https://example.test', sessionStore: sessions('token'), fetchImpl: async () => { throw new TypeError('network'); } });
  await assert.rejects(api.listActivities(), (error: unknown) => error instanceof ApiError && error.kind === 'offline');
});

test('provider exchange sends no bearer and stores the returned opaque token', async () => {
  const store = sessions(null);
  let authorization: string | null = 'unexpected';
  const api = createActivityApi({ baseUrl: 'https://example.test', sessionStore: store, fetchImpl: async (_url, init) => {
    authorization = new Headers(init?.headers).get('Authorization');
    return Response.json({ token: 'opaque-session', expires_at: 'soon', account: { id: 'account-1' } });
  } });
  const result = await api.exchange({ challenge_id: 'challenge', id_token: 'provider-proof' });
  assert.equal(authorization, null);
  assert.equal(store.current, 'opaque-session');
  assert.equal(result.token, 'opaque-session');
});

test('auth controller can defer persistence until its login attempt is still current', async () => {
  const store = sessions(null);
  const api = createActivityApi({ baseUrl: 'https://example.test', sessionStore: store,
    fetchImpl: async () => Response.json({ token: 'opaque-session', expires_at: 'soon', account: { id: 'account-1' } }),
  });
  const result = await api.exchange({ challenge_id: 'challenge', id_token: 'provider-proof' }, { persist: false });
  assert.equal(store.current, null);
  assert.equal(result.token, 'opaque-session');
});

test('unreachable server requests time out without clearing the session', async () => {
  const store = sessions('current');
  const api = createActivityApi({ baseUrl: 'https://example.test', sessionStore: store, timeoutMs: 5,
    fetchImpl: async (_url, init) => new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(new Error('aborted')), { once: true });
    }),
  });
  await assert.rejects(api.getMe(), (error: unknown) => error instanceof ApiError && error.kind === 'offline');
  assert.equal(store.current, 'current');
});


test('account cancellation during token read prevents sending a mutation with the replacement session', async () => {
  const store = sessions('account-a');
  let finish!: (token: string) => void;
  store.getToken = () => new Promise<string>((resolve) => { finish = resolve; });
  let calls = 0;
  const request = createApiRequest({ baseUrl: 'https://example.test', sessionStore: store,
    fetchImpl: async () => { calls++; return new Response(null, { status: 204 }); } });
  const controller = new AbortController();
  const pending = request('/api/streets/1/manual-completion?dataset_id=1', {
    method: 'PUT', body: JSON.stringify({ reason: 'Checked for account A' }), signal: controller.signal,
  });
  controller.abort();
  store.current = 'account-b';
  finish('account-b');
  await assert.rejects(pending, (error: unknown) => error instanceof ApiError && error.kind === 'stale-session');
  assert.equal(calls, 0);
  assert.equal(store.current, 'account-b');
});
