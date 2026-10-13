import assert from 'node:assert/strict';
import test from 'node:test';
import type { ImportBatchApi } from '../src/api/importBatches';
import type { HistoryImportApi } from '../src/api/importHistories';
import { createHistoryImportApi } from '../src/api/importHistories';
import type { SessionStore } from '../src/api/client';
import type { GpxFile } from '../src/api/explore';
import type { ImportBatchCounts, ImportBatchItemResponse, ImportBatchPage, ImportBatchResponse, ImportHistoryCreate, ImportHistoryPage, ImportHistoryState } from '../src/api/generated';
import { ImportStore } from '../src/importStore';
import type { PreparedHistoryFile } from '../src/historyImportSelection';

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const counts = (items: ImportBatchItemResponse[]): ImportBatchCounts => {
  const result: ImportBatchCounts = { awaiting_upload: 0, queued: 0, processing: 0, succeeded: 0, failed: 0, deleted: 0 };
  for (const item of items) result[item.status]++;
  return result;
};
const prepared = (index: number, name = `activity-${index}.gpx`, hash = index.toString(16).padStart(64, '0')): PreparedHistoryFile => ({
  uri: `memory:${index}`, name, kind: name.toLowerCase().endsWith('.fit') ? 'fit' : 'gpx', size: 100 + index, sha256: hash,
});

class MemoryHistoryApi implements ImportBatchApi, HistoryImportApi {
  histories = new Map<string, ImportHistoryState>();
  batches = new Map<string, ImportBatchResponse>();
  bodies: ImportHistoryCreate[] = [];
  uploads: string[] = [];
  activeUploads = 0;
  maxActiveUploads = 0;
  failCreates = 0;
  beforeCreate: (() => Promise<void>) | null = null;
  beforeUpload: ((batchId: string, itemId: string, file: GpxFile) => Promise<void>) | null = null;
  private historyId = 0;
  private batchId = 0;

  async createHistory(body: ImportHistoryCreate): Promise<ImportHistoryState> {
    this.bodies.push(body);
    if (this.beforeCreate) await this.beforeCreate();
    if (this.failCreates-- > 0) throw new Error('Connection lost after submission.');
    const existing = [...this.histories.values()].find((history) => history.id === body.request_id);
    if (existing) return this.getHistory(existing.id);
    const id = `history-${++this.historyId}`;
    const batchIds: string[] = [];
    for (let start = 0; start < body.files.length; start += 20) {
      const batchId = `batch-${++this.batchId}`; batchIds.push(batchId);
      const files = body.files.slice(start, start + 20);
      const items: ImportBatchItemResponse[] = files.map((file, offset) => ({
        id: `${batchId}-item-${offset}`, name: file.name, format: file.format, status: 'awaiting_upload',
        source_id: null, activity_id: null, duplicate: false, error: null, expected_sha256: file.sha256, expected_size: file.size,
      }));
      this.batches.set(batchId, { id: batchId, state: 'open', created_at: 'now', history_id: id, items, counts: counts(items) });
    }
    const history: ImportHistoryState = { id, label: body.label, state: 'open', created_at: 'now', file_count: body.files.length,
      batch_ids: batchIds, counts: this.totalCounts(batchIds), duplicate_count: 0 };
    this.histories.set(id, history);
    return this.getHistory(id);
  }
  async listHistories(page: number): Promise<ImportHistoryPage> {
    const items = await Promise.all([...this.histories.values()].slice((page - 1) * 20, page * 20).map((history) => this.getHistory(history.id)));
    return { items, page, page_size: 20, total: this.histories.size };
  }
  async getHistory(id: string): Promise<ImportHistoryState> {
    const history = this.histories.get(id);
    if (!history) throw new Error('missing history');
    return { ...history, counts: this.totalCounts(history.batch_ids) };
  }
  async listHistoryBatches(id: string, page: number): Promise<ImportBatchPage> {
    const history = this.histories.get(id);
    if (!history) throw new Error('missing history');
    return { items: history.batch_ids.slice((page - 1) * 20, page * 20).map((batchId) => this.getBatchValue(batchId)), page, page_size: 20, total: history.batch_ids.length };
  }
  async stopHistory(id: string): Promise<void> { this.setHistoryState(id, 'stopped'); }
  async resumeHistory(id: string): Promise<void> { this.setHistoryState(id, 'open'); }
  async createBatch(body: { request_id: string; files: Array<{ name: string; format: 'gpx' | 'fit' }> }): Promise<ImportBatchResponse> {
    const id = `ordinary-${this.batches.size + 1}`;
    const items: ImportBatchItemResponse[] = body.files.map((file, index) => ({ id: `${id}-${index}`, name: file.name,
      format: file.format, status: 'awaiting_upload', source_id: null, activity_id: null, duplicate: false, error: null }));
    const batch = { id, state: 'open' as const, created_at: 'now', items, counts: counts(items) };
    this.batches.set(id, batch); return batch;
  }
  async listBatches(page: number): Promise<ImportBatchPage> {
    const items = [...this.batches.values()].filter((batch) => !batch.history_id).slice((page - 1) * 20, page * 20);
    return { items, page, page_size: 20, total: items.length };
  }
  async getBatch(id: string): Promise<ImportBatchResponse> { return this.getBatchValue(id); }
  async uploadItem(batchId: string, itemId: string, file: GpxFile): Promise<ImportBatchItemResponse> {
    this.uploads.push(itemId); this.activeUploads++; this.maxActiveUploads = Math.max(this.maxActiveUploads, this.activeUploads);
    try { if (this.beforeUpload) await this.beforeUpload(batchId, itemId, file); }
    finally { this.activeUploads--; }
    const batch = this.getBatchValue(batchId);
    const updated = { ...batch, items: batch.items.map((item) => item.id === itemId ? { ...item, status: 'succeeded' as const, source_id: `source-${itemId}` } : item) };
    this.batches.set(batchId, { ...updated, counts: counts(updated.items) });
    return updated.items.find((item) => item.id === itemId)!;
  }
  async stopBatch(id: string): Promise<void> { this.batches.set(id, { ...this.getBatchValue(id), state: 'stopped' }); }
  async resumeBatch(id: string): Promise<void> { this.batches.set(id, { ...this.getBatchValue(id), state: 'open' }); }
  async retryUpload(): Promise<void> {}

  resetBackend(): void { this.histories.clear(); this.batches.clear(); this.historyId = 0; this.batchId = 0; }

  seedHistory(body: ImportHistoryCreate, state: 'open' | 'stopped' = 'open'): ImportHistoryState {
    const summary = this.createHistorySync(body, state);
    return summary;
  }
  private createHistorySync(body: ImportHistoryCreate, state: 'open' | 'stopped'): ImportHistoryState {
    const id = `history-${++this.historyId}`; const batchIds: string[] = [];
    for (let start = 0; start < body.files.length; start += 20) {
      const batchId = `batch-${++this.batchId}`; batchIds.push(batchId);
      const files = body.files.slice(start, start + 20);
      const items: ImportBatchItemResponse[] = files.map((file, index) => ({ id: `${batchId}-item-${index}`, name: file.name,
        format: file.format, status: 'awaiting_upload', source_id: null, activity_id: null, duplicate: false, error: null,
        expected_sha256: file.sha256, expected_size: file.size }));
      this.batches.set(batchId, { id: batchId, state, created_at: 'now', history_id: id, items, counts: counts(items) });
    }
    const history = { id, label: body.label, state, created_at: 'now', file_count: body.files.length, batch_ids: batchIds,
      counts: this.totalCounts(batchIds), duplicate_count: 0 } satisfies ImportHistoryState;
    this.histories.set(id, history); return history;
  }
  private setHistoryState(id: string, state: 'open' | 'stopped'): void {
    const history = this.histories.get(id); if (!history) return;
    this.histories.set(id, { ...history, state });
    for (const batchId of history.batch_ids) this.batches.set(batchId, { ...this.getBatchValue(batchId), state });
  }
  private getBatchValue(id: string): ImportBatchResponse {
    const batch = this.batches.get(id); if (!batch) throw new Error('missing batch'); return batch;
  }
  private totalCounts(batchIds: string[]): ImportBatchCounts {
    const result: ImportBatchCounts = { awaiting_upload: 0, queued: 0, processing: 0, succeeded: 0, failed: 0, deleted: 0 };
    for (const id of batchIds) for (const item of this.getBatchValue(id).items) result[item.status]++;
    return result;
  }
}

test('history API uses authenticated bounded paged routes and exact manifest fields', async () => {
  const requests: Array<{ url: string; method?: string; auth: string | null; body?: string }> = [];
  const sessions: SessionStore = { async getToken() { return 'token'; }, async setToken() {}, async clear() {}, async clearIfCurrent() { return false; } };
  const api = createHistoryImportApi({ baseUrl: 'https://api.test/', sessionStore: sessions, fetchImpl: async (url, init) => {
    requests.push({ url: String(url), method: init?.method, auth: new Headers(init?.headers).get('Authorization'), body: typeof init?.body === 'string' ? init.body : undefined });
    return requests.length === 1 ? Response.json({}) : new Response(null, { status: 204 });
  } });
  const body = { request_id: 'request-1', label: 'August', files: [{ name: 'run.fit', format: 'fit' as const, size: 50, sha256: 'a'.repeat(64) }] };
  await api.createHistory(body); await api.listHistories(3); await api.getHistory('9007199254740993');
  await api.listHistoryBatches('9007199254740993', 2); await api.stopHistory('9007199254740993'); await api.resumeHistory('9007199254740993');
  assert.equal(JSON.parse(requests[0]!.body!).request_id, 'request-1');
  assert.equal(JSON.parse(requests[0]!.body!).files[0].sha256, 'a'.repeat(64));
  assert.equal(new URL(requests[1]!.url).searchParams.get('page_size'), '20');
  assert.equal(requests[2]!.url, 'https://api.test/api/import-histories/9007199254740993');
  assert.equal(new URL(requests[3]!.url).searchParams.get('page'), '2');
  assert.equal(requests[4]!.method, 'POST'); assert.equal(requests[5]!.method, 'POST');
  assert.ok(requests.every((request) => request.auth === 'Bearer token'));
});

test('history creation loads every child manifest before sequential cross-batch uploads', async () => {
  const api = new MemoryHistoryApi();
  const files = Array.from({ length: 21 }, (_item, index) => prepared(index));
  const store = new ImportStore(api, () => 'stable-request', undefined, api);
  await store.createHistoryFromSelection('My runs', files);
  assert.equal(api.bodies.length, 1);
  assert.equal(api.bodies[0]!.files.length, 21);
  assert.equal(api.uploads.length, 21);
  assert.equal(api.maxActiveUploads, 1);
  assert.equal(store.getState().historyBatches['history-1']?.length, 2);
  assert.equal(store.getState().pendingHistoryCreation, false);
  assert.equal(store.getState().historyBatches['history-1']?.flatMap((batch) => batch.items).every((item) => item.status === 'succeeded'), true);
  assert.equal(store.getState().histories[0]?.counts.awaiting_upload, 0);
  assert.equal(store.getState().histories[0]?.counts.succeeded, 21);
});

test('uncertain history creation retries the exact request UUID and bytes', async () => {
  const api = new MemoryHistoryApi(); api.failCreates = 1;
  let uuid = 0;
  const store = new ImportStore(api, () => `request-${++uuid}`, undefined, api);
  await store.createHistoryFromSelection('Recover me', [prepared(1)]);
  assert.equal(store.getState().pendingHistoryCreation, false);
  assert.equal(store.hasPendingHistoryCreation(), true);
  assert.match(store.getState().historyError ?? '', /Connection lost/);
  await store.retryHistoryCreation();
  assert.equal(api.bodies.length, 2);
  assert.equal(api.bodies[0]!.request_id, api.bodies[1]!.request_id);
  assert.equal(api.bodies[0]!.files[0]!.sha256, api.bodies[1]!.files[0]!.sha256);
  assert.equal(api.uploads.length, 1);
});

test('history stop fences later files while active upload settles; resume drains retained bytes', async () => {
  const api = new MemoryHistoryApi(); const started = deferred<void>(); const active = deferred<void>(); let first = true;
  api.beforeUpload = async () => { if (first) { first = false; started.resolve(); await active.promise; } };
  const store = new ImportStore(api, () => 'request', undefined, api);
  const creating = store.createHistoryFromSelection('Pause me', [prepared(1), prepared(2)]);
  await started.promise;
  await store.stopHistory('history-1');
  active.resolve(); await creating;
  assert.equal(api.uploads.length, 1);
  assert.equal(api.batches.get('batch-1')?.items[0]?.status, 'succeeded');
  assert.equal(api.batches.get('batch-1')?.items[1]?.status, 'awaiting_upload');
  assert.equal(store.getState().histories[0]?.state, 'stopped');
  await store.resumeHistory('history-1');
  assert.equal(api.uploads.length, 2);
  assert.equal(api.batches.get('batch-1')?.items[1]?.status, 'succeeded');
});

test('Stop remains available during resumed uploads and prevents the next selected file', async () => {
  const api = new MemoryHistoryApi(); const started = deferred<void>(); const active = deferred<void>();
  api.beforeUpload = async () => { started.resolve(); await active.promise; };
  const files = [prepared(11), prepared(12)];
  const body: ImportHistoryCreate = { request_id: 'stop-resume', label: 'Stop resume', files: files.map((file) => ({ name: file.name, format: file.kind, size: file.size, sha256: file.sha256 })) };
  const summary = api.seedHistory(body, 'stopped');
  const store = new ImportStore(api, () => 'new', undefined, api);
  await store.loadHistories(); await store.reselectHistory(summary.id, files);
  const resuming = store.resumeHistory(summary.id);
  await started.promise;
  await store.stopHistory(summary.id);
  active.resolve(); await resuming;
  assert.equal(api.uploads.length, 1);
  assert.equal(store.getState().histories[0]?.state, 'stopped');
});

test('parent activity summaries keep polling active work before child batches are opened', async () => {
  const api = new MemoryHistoryApi();
  const summary = api.seedHistory({ request_id: 'background', label: 'Background', files: [prepared(1)].map((file) => ({ name: file.name, format: file.kind, size: file.size, sha256: file.sha256 })) }, 'stopped');
  const batch = api.batches.get(summary.batch_ids[0]!)!;
  batch.items[0]!.status = 'queued'; batch.items[0]!.source_id = 'source-1'; batch.counts = counts(batch.items);
  api.histories.set(summary.id, { ...summary, counts: counts(batch.items) });
  let reads = 0; const getHistory = api.getHistory.bind(api);
  api.getHistory = async (id) => { reads++; return getHistory(id); };
  const store = new ImportStore(api, () => 'request', undefined, api);
  await store.loadHistories();
  const afterList = reads;
  assert.equal(store.getState().historyBatches[summary.id], undefined);
  await store.pollForeground();
  assert.equal(reads, afterList + 1);
  assert.equal(store.getState().histories[0]?.counts.queued, 1);
});

test('a history list snapshot fences a detail refresh that started earlier', async () => {
  const api = new MemoryHistoryApi();
  const summary = api.seedHistory({ request_id: 'stale-detail', label: 'Race', files: [prepared(1)].map((file) => ({ name: file.name, format: file.kind, size: file.size, sha256: file.sha256 })) });
  const store = new ImportStore(api, () => 'request', undefined, api);
  await store.loadHistories();
  const oldRead = deferred<void>(); let first = true; const getHistory = api.getHistory.bind(api);
  api.getHistory = async (id) => {
    const snapshot = await getHistory(id);
    if (first) { first = false; await oldRead.promise; }
    return snapshot;
  };
  const staleDetail = store.refreshHistory(summary.id);
  await tick();
  api.histories.set(summary.id, { ...summary, state: 'stopped' });
  await store.loadHistories();
  oldRead.resolve(); await staleDetail;
  assert.equal(store.getState().histories[0]?.state, 'stopped');
});

test('old-account upload failure cleanup cannot discard a new account queue with reused IDs', async () => {
  const api = new MemoryHistoryApi(); const oldStarted = deferred<void>(); const oldFailure = deferred<void>();
  const newStarted = deferred<void>(); const newRelease = deferred<void>();
  api.beforeUpload = async (_batchId, _itemId, file) => {
    if (file.uri === 'memory:0') { oldStarted.resolve(); await oldFailure.promise; throw new Error('old upload failed'); }
    if (file.uri === 'memory:101') { newStarted.resolve(); await newRelease.promise; }
  };
  const store = new ImportStore(api, () => 'request', undefined, api);
  const oldCreate = store.createHistoryFromSelection('Old', Array.from({ length: 21 }, (_item, index) => prepared(index)));
  await oldStarted.promise;
  store.reset(); api.resetBackend();
  const newCreate = store.createHistoryFromSelection('New', Array.from({ length: 21 }, (_item, index) => prepared(index + 101)));
  await newStarted.promise;
  oldFailure.resolve(); await oldCreate;
  newRelease.resolve(); await newCreate;
  assert.equal(api.uploads.length, 22);
  assert.equal(api.uploads.includes('batch-2-item-0'), true);
});

test('partial history reselection merges file refs and refresh releases server-accepted or deleted refs', async () => {
  const api = new MemoryHistoryApi();
  const files = [prepared(31), prepared(32), prepared(33)];
  const summary = api.seedHistory({ request_id: 'partial', label: 'Partial', files: files.map((file) => ({ name: file.name, format: file.kind, size: file.size, sha256: file.sha256 })) }, 'stopped');
  const store = new ImportStore(api, () => 'request', undefined, api);
  await store.loadHistories();
  assert.equal(await store.reselectHistory(summary.id, [files[0]!]), true);
  assert.equal(await store.reselectHistory(summary.id, [files[2]!]), true);
  assert.equal(store.retainsFile(files[0]!.uri), true);
  assert.equal(store.retainsFile(files[2]!.uri), true);
  const batch = api.batches.get(summary.batch_ids[0]!)!;
  batch.items[0] = { ...batch.items[0]!, status: 'failed', source_id: 'source-accepted' };
  batch.items[1] = { ...batch.items[1]!, status: 'deleted' };
  batch.counts = counts(batch.items);
  await store.loadHistoryBatches(summary.id);
  assert.equal(store.retainsFile(files[0]!.uri), false);
  assert.equal(store.retainsFile(files[2]!.uri), true);
});

test('restart reselection fetches all child pages, matches by hash identity, and never submits deleted items', async () => {
  const api = new MemoryHistoryApi();
  const files = Array.from({ length: 1_000 }, (_item, index) => prepared(index));
  const body: ImportHistoryCreate = { request_id: 'old', label: 'Old run folder', files: files.map((item) => ({ name: item.name, format: item.kind, size: item.size, sha256: item.sha256 })) };
  const summary = api.seedHistory(body, 'stopped');
  api.batches.get(summary.batch_ids[0]!)!.items[0]!.status = 'deleted';
  api.batches.get(summary.batch_ids[0]!)!.counts = counts(api.batches.get(summary.batch_ids[0]!)!.items);
  let pageCalls = 0;
  const realList = api.listHistoryBatches.bind(api);
  api.listHistoryBatches = async (id, page) => { pageCalls++; return realList(id, page); };
  const store = new ImportStore(api, () => 'new-request', undefined, api);
  await store.loadHistories();
  assert.equal(await store.reselectHistory(summary.id, [...files].reverse()), true);
  assert.equal(pageCalls, 3);
  assert.equal(store.getState().historyBatches[summary.id]?.length, 50);
  assert.equal(files.slice(1).every((file, index) => store.hasSelectedFile(summary.batch_ids[Math.floor((index + 1) / 20)]!, `${summary.batch_ids[Math.floor((index + 1) / 20)]}-item-${(index + 1) % 20}`)), true);
  assert.equal(api.uploads.length, 0);
  await store.resumeHistory(summary.id);
  assert.equal(store.getState().historyError, null, store.getState().historyError ?? '');
  assert.equal(api.uploads.length, 999);
  assert.equal(api.uploads.includes(`${summary.batch_ids[0]}-item-0`), false);
});

test('one identical-byte selection can satisfy duplicate manifest rows safely', async () => {
  const api = new MemoryHistoryApi();
  const duplicateA = prepared(1, 'run.gpx', 'f'.repeat(64));
  const duplicateB = prepared(2, 'run.gpx', 'f'.repeat(64));
  duplicateB.size = duplicateA.size;
  const body: ImportHistoryCreate = { request_id: 'dupes', label: 'Duplicates', files: [duplicateA, duplicateB].map((file) => ({ name: file.name, format: file.kind, size: file.size, sha256: file.sha256 })) };
  const summary = api.seedHistory(body, 'stopped');
  const released: string[] = [];
  const store = new ImportStore(api, () => 'new', undefined, api, (uri) => released.push(uri));
  await store.loadHistories();
  assert.equal(await store.reselectHistory(summary.id, [duplicateA]), true);
  assert.equal(store.hasSelectedFile(summary.batch_ids[0]!, `${summary.batch_ids[0]}-item-1`), true);
  assert.equal(store.retainsFile(duplicateA.uri), true);
  await store.resumeHistory(summary.id);
  assert.equal(api.uploads.join(','), 'batch-1-item-0,batch-1-item-1');
  assert.deepEqual(released, [duplicateA.uri]);
  assert.equal(store.retainsFile(duplicateA.uri), false);
});

test('accepted and deleted server items release retained local file references after refresh', async () => {
  const api = new MemoryHistoryApi();
  const file = prepared(5);
  const store = new ImportStore(api, () => 'request', undefined, api);
  await store.createHistoryFromSelection('Retention', [file]);
  assert.equal(store.retainsFile(file.uri), false);

  const deletedFile = prepared(6);
  const summary = api.seedHistory({ request_id: 'deleted', label: 'Deleted', files: [deletedFile].map((item) => ({ name: item.name, format: item.kind, size: item.size, sha256: item.sha256 })) }, 'stopped');
  await store.loadHistories();
  await store.reselectHistory(summary.id, [deletedFile]);
  assert.equal(store.retainsFile(deletedFile.uri), true);
  const batch = api.batches.get(summary.batch_ids[0]!)!;
  api.batches.set(batch.id, { ...batch, items: batch.items.map((item) => ({ ...item, status: 'deleted' })), counts: { ...counts(batch.items), awaiting_upload: 0, deleted: 1 } });
  await store.refreshBatch(batch.id);
  assert.equal(store.retainsFile(deletedFile.uri), false);
});

test('history and standalone batch paging remain independent', async () => {
  const api = new MemoryHistoryApi();
  const group = api.seedHistory({ request_id: 'group', label: 'Group', files: [prepared(1)].map((file) => ({ name: file.name, format: file.kind, size: file.size, sha256: file.sha256 })) });
  await api.createBatch({ request_id: 'standalone', files: [{ name: 'other.gpx', format: 'gpx' }] });
  const store = new ImportStore(api, () => 'new', undefined, api);
  await store.loadHistories(); await store.loadHistoryBatches(group.id); await store.loadPage(1);
  assert.equal(store.getState().batches.some((batch) => batch.history_id), false);
  assert.equal(store.getState().historyBatches[group.id]?.[0]?.id, group.batch_ids[0]);
});

test('account reset fences delayed history creation before child manifests or uploads', async () => {
  const api = new MemoryHistoryApi(); const creation = deferred<void>();
  api.beforeCreate = () => creation.promise;
  const store = new ImportStore(api, () => 'request', undefined, api);
  const pending = store.createHistoryFromSelection('Account A', [prepared(1)]);
  await tick(); store.reset(); creation.resolve(); await pending;
  assert.equal(store.getState().histories.length, 0);
  assert.equal(api.uploads.length, 0);
});
