import assert from 'node:assert/strict';
import test from 'node:test';
import type { ImportBatchApi } from '../src/api/importBatches';
import type { ImportBatchItemResponse, ImportBatchResponse } from '../src/api/generated';
import { ImportStore } from '../src/importStore';

const counts = () => ({ awaiting_upload: 0, queued: 0, processing: 0, succeeded: 0, failed: 0, deleted: 0 });
const item = (id: string, status: ImportBatchItemResponse['status'], name = `${id}.gpx`, extra: Partial<ImportBatchItemResponse> = {}): ImportBatchItemResponse => ({
  id, name, format: name.endsWith('.fit') ? 'fit' : 'gpx', status, source_id: null, activity_id: null, duplicate: false, error: null, ...extra,
});
const batch = (items: ImportBatchItemResponse[], state: ImportBatchResponse['state'] = 'open'): ImportBatchResponse => {
  const tally = counts(); for (const row of items) tally[row.status]++;
  return { id: 'batch-1', state, created_at: '2026-10-06T00:00:00Z', items, counts: tally };
};
const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;
const tick = () => new Promise<void>((resolve) => setImmediate(resolve));

function fakeApi(initial: ImportBatchResponse) {
  let current = clone(initial);
  const uploaded: string[] = [];
  let upload = async (id: string) => { uploaded.push(id); const row = current.items.find((candidate) => candidate.id === id)!; row.status = 'queued'; row.source_id = `source-${id}`; current = batch(current.items, current.state); return clone(row); };
  let getBatch = async (_id: string) => clone(current);
  let listBatches = async (_page: number) => ({ items: [clone(current)], page: 1, page_size: 20, total: 1 });
  const api: ImportBatchApi = {
    async createBatch(body) { current = batch(body.files.map((file, index) => item(`item-${index + 1}`, 'awaiting_upload', file.name))); return clone(current); },
    async listBatches(page) { return listBatches(page); },
    async getBatch(id) { return getBatch(id); },
    async uploadItem(_batch, id) { return upload(id); },
    async stopBatch() { current.state = 'stopped'; },
    async resumeBatch() { current.state = 'open'; },
    async retryUpload(sourceId) { const row = current.items.find((candidate) => candidate.source_id === sourceId)!; row.status = 'queued'; current = batch(current.items, current.state); },
  };
  return { api, uploaded, get current() { return current; }, setUpload(fn: typeof upload) { upload = fn; }, setGetBatch(fn: typeof getBatch) { getBatch = fn; }, setList(fn: typeof listBatches) { listBatches = fn; } };
}

test('uploads one file at a time; Stop uploading lets the active request settle and pauses later items', async () => {
  const fake = fakeApi(batch([]));
  let release!: () => void;
  fake.setUpload(async (id) => { fake.uploaded.push(id); await new Promise<void>((resolve) => { release = resolve; });
    const row = fake.current.items.find((candidate) => candidate.id === id)!; row.status = 'queued'; row.source_id = `source-${id}`;
    const current = batch(fake.current.items, fake.current.state); Object.assign(fake.current, current); return clone(row); });
  const store = new ImportStore(fake.api, () => '00000000-0000-4000-8000-000000000001');
  const importing = store.createFromSelection([{ uri: 'a', name: 'one.gpx' }, { uri: 'b', name: 'two.fit' }]);
  await tick(); await store.stop('batch-1'); release(); await importing;
  assert.deepEqual(fake.uploaded, ['item-1']);
  assert.equal(fake.current.state, 'stopped');
  assert.equal(store.getState().batches[0]?.counts.queued, 1);
  fake.setUpload(async (id) => { fake.uploaded.push(id); const row = fake.current.items.find((candidate) => candidate.id === id)!; row.status = 'queued'; return clone(row); });
  await store.resume('batch-1');
  assert.deepEqual(fake.uploaded, ['item-1', 'item-2']);
});

test('restart recovers manifest but uploads no awaiting file until its exact file is reselected', async () => {
  const fake = fakeApi(batch([item('item-1', 'awaiting_upload', 'run.fit'), item('item-2', 'queued', 'accepted.gpx')]));
  const store = new ImportStore(fake.api, () => 'unused');
  await store.loadPage();
  assert.equal(fake.uploaded.length, 0);
  assert.equal(await store.reselect('batch-1', 'item-1', { uri: 'wrong', name: 'other.fit' }), false);
  assert.deepEqual(fake.uploaded, []);
  assert.equal(await store.reselect('batch-1', 'item-1', { uri: 'right', name: 'run.fit' }), true);
  assert.deepEqual(fake.uploaded, ['item-1']);
  assert.equal(await store.reselect('batch-1', 'item-2', { uri: 'accepted', name: 'accepted.gpx' }), false);
});

test('reselecting a paused file reports it ready without uploading until explicit resume', async () => {
  const fake = fakeApi(batch([item('item-1', 'awaiting_upload', 'run.fit')], 'stopped'));
  const store = new ImportStore(fake.api, () => 'unused'); await store.loadPage();
  assert.equal(store.hasSelectedFile('batch-1', 'item-1'), false);
  assert.equal(await store.reselect('batch-1', 'item-1', { uri: 'file', name: 'run.fit' }), true);
  assert.equal(store.hasSelectedFile('batch-1', 'item-1'), true);
  assert.deepEqual(fake.uploaded, []);
  await store.resume('batch-1'); assert.deepEqual(fake.uploaded, ['item-1']);
  assert.equal(store.hasSelectedFile('batch-1', 'item-1'), false);
  store.reset(); assert.equal(store.hasSelectedFile('batch-1', 'item-1'), false);
});

test('deleted manifest items stay terminal and retry only invokes the source retry endpoint', async () => {
  const deleted = item('deleted-1', 'deleted', 'old.gpx', { source_id: 'source-deleted' });
  const failed = item('failed-1', 'failed', 'bad.fit', { source_id: 'source-failed' });
  const fake = fakeApi(batch([deleted, failed]));
  let retried = '';
  const originalRetry = fake.api.retryUpload;
  fake.api.retryUpload = async (sourceId, signal) => { retried = sourceId; return originalRetry(sourceId, signal); };
  const store = new ImportStore(fake.api, () => 'unused'); await store.loadPage();
  assert.equal(await store.reselect('batch-1', 'deleted-1', { uri: 'old', name: 'old.gpx' }), false);
  await store.retry('batch-1', 'failed-1');
  assert.equal(retried, 'source-failed');
  assert.deepEqual(fake.uploaded, []);
  assert.equal(store.getState().batches[0]?.items[0]?.status, 'deleted');
  assert.equal(store.getState().batches[0]?.items[1]?.status, 'queued');
});

test('account reset fences delayed manifest creation and response cannot upload into the next account', async () => {
  const fake = fakeApi(batch([]));
  let release!: (value: ImportBatchResponse) => void;
  fake.api.createBatch = async () => new Promise((resolve) => { release = resolve; });
  const store = new ImportStore(fake.api, () => 'request');
  const pending = store.createFromSelection([{ uri: 'a', name: 'run.gpx' }]); await tick();
  store.reset(); release(batch([item('item-1', 'awaiting_upload')])); await pending;
  assert.equal(store.getState().batches.length, 0);
  assert.deepEqual(fake.uploaded, []);
});

test('a manifest created during another upload queues its files and drains sequentially', async () => {
  const fake = fakeApi(batch([])); const batches = new Map<string, ImportBatchResponse>(); const uploaded: string[] = [];
  fake.api.createBatch = async (body) => {
    const created = { ...batch(body.files.map((file) => item(file.name, 'awaiting_upload', file.name))), id: body.request_id };
    batches.set(created.id, created); return clone(created);
  };
  fake.api.getBatch = async (id) => clone(batches.get(id)!);
  let release!: () => void; let active = 0;
  fake.api.uploadItem = async (id, itemId) => {
    assert.equal(++active, 1); uploaded.push(itemId);
    if (uploaded.length === 1) await new Promise<void>((resolve) => { release = resolve; });
    const current = batches.get(id)!; const row = current.items.find((entry) => entry.id === itemId)!; row.status = 'queued';
    Object.assign(current, batch(current.items), { id }); active--; return clone(row);
  };
  let request = 0; const store = new ImportStore(fake.api, () => `batch-${++request}`);
  const first = store.createFromSelection([{ uri: 'a', name: 'one.gpx' }]); await tick();
  await store.createFromSelection([{ uri: 'b', name: 'two.fit' }]);
  assert.deepEqual(uploaded, ['one.gpx']); release(); await first;
  assert.deepEqual(uploaded, ['one.gpx', 'two.fit']);
  assert.equal(store.getState().batches.every((entry) => entry.counts.awaiting_upload === 0), true);
});

test('foreground manifest polling does not overlap slow requests and account reset releases the old owner safely', async () => {
  const fake = fakeApi(batch([item('item-1', 'processing')]));
  await fake.api.listBatches(1);
  const store = new ImportStore(fake.api, () => 'unused'); await store.loadPage();
  const releases: Array<() => void> = []; let calls = 0;
  fake.setGetBatch(async () => { calls++; await new Promise<void>((resolve) => { releases.push(resolve); }); return clone(fake.current); });
  const old = store.pollForeground(); await tick(); await store.pollForeground(); assert.equal(calls, 1);
  store.reset(); await store.loadPage();
  const current = store.pollForeground(); await tick(); assert.equal(calls, 2);
  releases[0]?.(); await old; await store.pollForeground(); assert.equal(calls, 2);
  releases[1]?.(); await current;
});

test('a delayed list snapshot cannot restore a deleted item over a newer detail response', async () => {
  const fake = fakeApi(batch([item('item-1', 'queued', 'run.gpx')]));
  const store = new ImportStore(fake.api, () => 'unused'); await store.loadPage();
  let releaseList!: (value: { items: ImportBatchResponse[]; page: number; page_size: number; total: number }) => void;
  fake.setList(() => new Promise((resolve) => { releaseList = resolve; }));
  const oldList = store.loadPage(1);
  const deleted = batch([item('item-1', 'deleted', 'run.gpx')]);
  Object.assign(fake.current, deleted);
  await store.refreshBatch('batch-1');
  releaseList({ items: [batch([item('item-1', 'queued', 'run.gpx')])], page: 1, page_size: 20, total: 1 });
  await oldList;
  assert.equal(store.getState().batches[0]?.items[0]?.status, 'deleted');
});

test('a delayed detail response cannot restore an accepted item over a newer deleted manifest snapshot', async () => {
  const fake = fakeApi(batch([item('item-1', 'queued', 'run.gpx')]));
  const store = new ImportStore(fake.api, () => 'unused'); await store.loadPage();
  let releaseDetail!: (value: ImportBatchResponse) => void;
  fake.setGetBatch(() => new Promise((resolve) => { releaseDetail = resolve; }));
  const oldDetail = store.refreshBatch('batch-1'); await tick();
  const deleted = batch([item('item-1', 'deleted', 'run.gpx')]); Object.assign(fake.current, deleted);
  fake.setList(async () => ({ items: [deleted], page: 1, page_size: 20, total: 1 }));
  await store.loadPage(1);
  releaseDetail(batch([item('item-1', 'queued', 'run.gpx')])); await oldDetail;
  assert.equal(store.getState().batches[0]?.items[0]?.status, 'deleted');
});

test('an older detail response cannot reopen a batch after a newer list snapshot says stopped', async () => {
  const fake = fakeApi(batch([item('item-1', 'awaiting_upload')], 'open'));
  const store = new ImportStore(fake.api, () => 'unused'); await store.loadPage();
  let releaseDetail!: (value: ImportBatchResponse) => void;
  fake.setGetBatch(() => new Promise((resolve) => { releaseDetail = resolve; }));
  const stale = store.refreshBatch('batch-1'); await tick();
  const stopped = batch([item('item-1', 'awaiting_upload')], 'stopped'); Object.assign(fake.current, stopped);
  fake.setList(async () => ({ items: [stopped], page: 1, page_size: 20, total: 1 }));
  await store.loadPage(1);
  releaseDetail(batch([item('item-1', 'awaiting_upload')], 'open')); await stale;
  assert.equal(store.getState().batches[0]?.state, 'stopped');
});

test('a delayed empty list cannot hide a new batch after it has been stopped', async () => {
  const fake = fakeApi(batch([]));
  let releaseList!: (value: { items: ImportBatchResponse[]; page: number; page_size: number; total: number }) => void;
  fake.setList(() => new Promise((resolve) => { releaseList = resolve; }));
  const store = new ImportStore(fake.api, () => 'new-request');
  const oldList = store.loadPage(1); await tick();
  await store.createFromSelection([{ uri: 'file', name: 'run.gpx' }]);
  await store.stop('batch-1');
  releaseList({ items: [], page: 1, page_size: 20, total: 0 }); await oldList;
  assert.equal(store.getState().batches[0]?.id, 'batch-1');
  assert.equal(store.getState().batches[0]?.state, 'stopped');
  assert.equal(store.getState().total, 1);
});

test('list refresh notices a newly succeeded item once and refreshes dependent activity views', async () => {
  const fake = fakeApi(batch([item('item-1', 'queued')]));
  let refreshed = 0;
  const store = new ImportStore(fake.api, () => 'unused', () => { refreshed++; }); await store.loadPage();
  Object.assign(fake.current, batch([item('item-1', 'succeeded')]));
  await store.refresh();
  assert.equal(refreshed, 1);
  await store.refresh();
  assert.equal(refreshed, 1);
});

test('foreground polling caps each pass at 20 manifests', async () => {
  const fake = fakeApi(batch([]));
  const activeBatches = Array.from({ length: 45 }, (_, index) => ({ ...batch([item(`item-${index}`, 'processing')]), id: `batch-${index}` }));
  fake.setList(async (page) => ({ items: activeBatches.slice((page - 1) * 20, page * 20), page, page_size: 20, total: activeBatches.length }));
  const store = new ImportStore(fake.api, () => 'unused'); await store.loadPage(); await store.loadMore(); await store.loadMore();
  const calls: string[] = []; fake.setGetBatch(async (id) => { calls.push(id); return activeBatches.find((entry) => entry.id === id)!; });
  await store.pollForeground(); assert.equal(calls.length, 20);
  await store.pollForeground(); assert.equal(calls.length, 40);
  assert.equal(calls.slice(0, 20).some((id) => calls.slice(20, 40).includes(id)), false);
  await store.pollForeground(); assert.equal(calls.length, 60);
  assert.equal(calls.slice(40, 60).includes('batch-44'), true);
});

test('resume requested during an active stop-raced upload drains the remaining local queue after settlement', async () => {
  const fake = fakeApi(batch([]));
  let rejectActive!: (error: Error) => void;
  fake.setUpload(async (id): Promise<ImportBatchItemResponse> => { fake.uploaded.push(id); await new Promise<never>((_resolve, reject) => { rejectActive = reject; }); throw new Error('unreachable'); });
  const store = new ImportStore(fake.api, () => 'request');
  const upload = store.createFromSelection([{ uri: 'a', name: 'one.gpx' }, { uri: 'b', name: 'two.gpx' }]);
  await tick(); await store.stop('batch-1'); await store.resume('batch-1');
  fake.setUpload(async (id) => { fake.uploaded.push(id); const row = fake.current.items.find((candidate) => candidate.id === id)!; row.status = 'queued'; return clone(row); });
  rejectActive(new Error('Upload lost the stop race.'));
  await upload;
  assert.deepEqual(fake.uploaded, ['item-1', 'item-1', 'item-2']);
});

test('Stop uploading remains available during resumed upload and pauses the next file', async () => {
  const fake = fakeApi(batch([]));
  const releases: Array<() => void> = [];
  fake.setUpload(async (id) => { fake.uploaded.push(id); await new Promise<void>((resolve) => { releases.push(resolve); });
    const row = fake.current.items.find((candidate) => candidate.id === id)!; row.status = 'queued'; row.source_id = `source-${id}`;
    Object.assign(fake.current, batch(fake.current.items, fake.current.state)); return clone(row); });
  const store = new ImportStore(fake.api, () => 'request');
  const initialUpload = store.createFromSelection([{ uri: 'a', name: 'one.gpx' }, { uri: 'b', name: 'two.gpx' }, { uri: 'c', name: 'three.gpx' }]);
  await tick(); await store.stop('batch-1'); releases[0]?.(); await initialUpload;
  const resume = store.resume('batch-1'); await tick(); assert.deepEqual(fake.uploaded, ['item-1', 'item-2']);
  await store.stop('batch-1'); releases[1]?.(); await resume;
  assert.deepEqual(fake.uploaded, ['item-1', 'item-2']);
  assert.equal(store.getState().batches[0]?.counts.awaiting_upload, 1);
});

test('reselecting a zero-width filename matches the printable server manifest basename', async () => {
  const fake = fakeApi(batch([item('item-1', 'awaiting_upload', 'run.fit')]));
  const store = new ImportStore(fake.api, () => 'unused'); await store.loadPage();
  assert.equal(await store.reselect('batch-1', 'item-1', { uri: 'file', name: 'folder/run\u200b.fit' }), true);
  assert.deepEqual(fake.uploaded, ['item-1']);
});
