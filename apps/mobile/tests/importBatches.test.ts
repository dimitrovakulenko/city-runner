import assert from 'node:assert/strict';
import test from 'node:test';
import { createImportBatchApi } from '../src/api/importBatches';
import type { SessionStore } from '../src/api/client';

function sessions(): SessionStore { return { async getToken() { return 'session'; }, async setToken() {}, async clear() {}, async clearIfCurrent() { return false; } }; }

test('batch manifest and status requests use the authenticated injected transport', async () => {
  const seen: Array<{ url: string; authorization: string | null; method: string }> = [];
  const api = createImportBatchApi({ baseUrl: 'https://api.test/', sessionStore: sessions(), fetchImpl: async (url, init) => {
    seen.push({ url, authorization: new Headers(init?.headers).get('Authorization'), method: init?.method ?? 'GET' });
    if (String(url).endsWith('/api/import-batches')) return Response.json({ items: [], page: 1, page_size: 20, total: 0 });
    return Response.json({ id: '10', state: 'open', created_at: 'now', items: [], counts: {} });
  } });
  await api.createBatch({ request_id: '00000000-0000-4000-8000-000000000001', files: [{ name: 'run.fit', format: 'fit' }] });
  await api.listBatches(1);
  await api.getBatch('10');
  assert.deepEqual(seen.map((call) => call.authorization), ['Bearer session', 'Bearer session', 'Bearer session']);
  assert.equal(seen[0]?.method, 'POST');
  assert.match(seen[1]?.url ?? '', /page_size=20/);
});

test('item uploads preserve multipart boundaries and address item IDs as opaque strings', async () => {
  const original = globalThis.FormData;
  const fields: Array<[string, unknown]> = [];
  class FakeFormData { append(name: string, value: unknown) { fields.push([name, value]); } }
  Object.defineProperty(globalThis, 'FormData', { configurable: true, value: FakeFormData });
  let calledUrl = ''; let contentType: string | null = null; let authorization: string | null = null;
  try {
    const api = createImportBatchApi({ baseUrl: 'https://api.test', sessionStore: sessions(), fetchImpl: async (url, init) => {
      calledUrl = url; contentType = new Headers(init?.headers).get('Content-Type'); authorization = new Headers(init?.headers).get('Authorization');
      return Response.json({ id: '9007199254740993', name: 'run.fit', format: 'fit', status: 'queued', source_id: '9', activity_id: null, duplicate: false, error: null });
    } });
    const item = await api.uploadItem('9007199254740991', '9007199254740992', { uri: 'file://run', name: 'run.fit' });
    assert.equal(item.id, '9007199254740993');
    assert.match(calledUrl, /9007199254740991\/items\/9007199254740992\/upload$/);
    assert.equal(contentType, null);
    assert.equal(authorization, 'Bearer session');
    assert.equal(fields[0]?.[0], 'file');
  } finally { Object.defineProperty(globalThis, 'FormData', { configurable: true, value: original }); }
});
