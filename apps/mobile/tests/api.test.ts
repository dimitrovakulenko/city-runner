import assert from 'node:assert/strict';
import test from 'node:test';
import { ApiError, createActivityApi } from '../src/api/client';
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

test('old-token 401 retries with, and preserves, a replacement session', async () => {
  const store = sessions('old');
  let calls = 0;
  const api = createActivityApi({ baseUrl: 'https://example.test', sessionStore: store, fetchImpl: async (_url, init) => {
    calls++;
    if (calls === 1) {
      assert.equal(new Headers(init?.headers).get('Authorization'), 'Bearer old');
      store.current = 'new';
      return new Response(null, { status: 401 });
    }
    assert.equal(new Headers(init?.headers).get('Authorization'), 'Bearer new');
    return Response.json({ id: '7', name: 'run', date: 'today', type: 'running', processed: true, unmapped_points: 0, tracks: [], timestamps: [], bounds: null });
  } });
  await api.getActivity('7');
  assert.equal(calls, 2);
  assert.equal(store.current, 'new');
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
