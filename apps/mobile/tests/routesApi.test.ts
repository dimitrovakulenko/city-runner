import assert from 'node:assert/strict';
import test from 'node:test';
import type { SessionStore } from '../src/api/client';
import { ApiError } from '../src/api/client';
import { createRouteApi } from '../src/api/routes';

function session(token: string | null): SessionStore & { current: string | null } {
  return { current: token, async getToken() { return this.current; }, async setToken(value) { this.current = value; }, async clear() { this.current = null; }, async clearIfCurrent(value) { if (this.current !== value) return false; this.current = null; return true; } };
}

test('route endpoints use authenticated generated API shapes and export GPX as text', async () => {
  const sessions = session('opaque'); const requests: Array<{ url: string; method: string; auth: string | null; accept: string | null }> = [];
  const api = createRouteApi({ baseUrl: 'https://example.test/', sessionStore: sessions, fetchImpl: async (url, init) => {
    const headers = new Headers(init?.headers); requests.push({ url, method: init?.method ?? 'GET', auth: headers.get('Authorization'), accept: headers.get('Accept') });
    if (url.endsWith('/gpx')) return new Response('<gpx version="1.1"/>', { headers: { 'Content-Type': 'application/gpx+xml' } });
    if (init?.method === 'DELETE') return new Response(null, { status: 204 });
    return Response.json({});
  } });
  await api.preview({ waypoints: [[4, 50], [4.1, 50.1]], client_revision: 4 });
  await api.list({ limit: 20, offset: 40 }); await api.get('id/with space');
  await api.create({ name: 'Loop', waypoints: [[4, 50], [4.1, 50.1]] });
  await api.update('route-1', { name: 'Loop', waypoints: [[4, 50], [4.1, 50.1]], expected_revision: 3 });
  await api.delete('route-1', 4); assert.equal(await api.exportGpx('route-1'), '<gpx version="1.1"/>');
  assert.equal(requests.length, 7); assert.ok(requests.every((request) => request.auth === 'Bearer opaque'));
  assert.match(requests[1]!.url, /limit=20&offset=40/); assert.match(requests[2]!.url, /id%2Fwith%20space/);
  assert.equal(requests[6]!.accept, 'application/gpx+xml, text/plain');
});

test('route requests are never replayed after account changes during a response', async () => {
  const sessions = session('account-a'); let finish!: (response: Response) => void; let calls = 0;
  const api = createRouteApi({ baseUrl: 'https://example.test', sessionStore: sessions, fetchImpl: async () => { calls++; return new Promise((resolve) => { finish = resolve; }); } });
  const pending = api.exportGpx('route-1'); await new Promise<void>((resolve) => setImmediate(resolve)); sessions.current = 'account-b'; finish(new Response('<gpx/>'));
  await assert.rejects(pending, (error: unknown) => error instanceof ApiError && error.kind === 'stale-session');
  assert.equal(calls, 1); assert.equal(sessions.current, 'account-b');
});
