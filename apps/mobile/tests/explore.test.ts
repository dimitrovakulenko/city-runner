import assert from 'node:assert/strict';
import test from 'node:test';
import { ApiError } from '../src/api/client';
import type { SessionStore } from '../src/api/client';
import { createExploreApi, createFixtureExploreApi } from '../src/api/explore';
import type { ExploreApi, GpxFile } from '../src/api/explore';
import { ExploreStore, type Viewport } from '../src/exploreStore';
import type { MapResponse, ProgressResponse, UploadResponse, UploadStatusResponse } from '../src/api/generated';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

const mapResult = (overrides: Partial<MapResponse> = {}) => ({
  bbox: [3.6, 51, 3.85, 51.1], zoom: 12, geography_state: 'supported', pending_imports: 0,
  dataset_truncated: false, datasets: [], cities: [], tracks: [], streets: [], missing_nodes: [],
  node_state: 'not-requested', limits: {}, ...overrides,
}) as unknown as MapResponse;
const progressResult = (overrides: Partial<ProgressResponse> = {}) => ({
  state: 'pending', rule: 'normal', datasets: [{ dataset_id: '9007199254740995', region: 'gent', state: 'pending',
    progress_revision: '2', visited_node_count: null, unsupported_sample_count: null, pending_sources: 1,
    failed_sources: 0, eligible_streets: null, completed_streets: null, eligible_nodes: null }],
  datasets_truncated: false, unmapped_points: 4, pending_imports: 0, ...overrides,
}) as unknown as ProgressResponse;
const viewport: Viewport = { bbox: [3.6, 51, 3.85, 51.1], zoom: 12 };
const uploadResponse = (overrides: Partial<UploadStatusResponse> = {}) => ({
  id: '9007199254740993', status: 'queued', activity_id: null, job_id: '1', error: null, ...overrides,
}) as UploadStatusResponse;

function api(overrides: Partial<ExploreApi> = {}): ExploreApi {
  return {
    async getMap() { return mapResult(); },
    async getProgress() { return progressResult({ state: 'ready', datasets: [] }); },
    async upload() { return { id: '9007199254740993', status: 'queued', job_id: '1', duplicate: false } as UploadResponse; },
    async getUpload() { return uploadResponse({ status: 'processing' }); },
    ...overrides,
  };
}

function sessionStore(initial: string | null): SessionStore & { current: string | null } {
  return {
    current: initial,
    async getToken() { return this.current; },
    async setToken(token) { this.current = token; },
    async clear() { this.current = null; },
    async clearIfCurrent(token) {
      if (this.current !== token) return false;
      this.current = null;
      return true;
    },
  };
}

test('explore client sends bearer, string bbox, selected rule, and keeps huge IDs', async () => {
  const store = sessionStore('opaque');
  let calledUrl = '';
  let authorization: string | null = null;
  const client = createExploreApi({ baseUrl: 'https://api.test/', sessionStore: store, fetchImpl: async (url, init) => {
    calledUrl = url;
    authorization = new Headers(init?.headers).get('Authorization');
    return Response.json(mapResult({ streets: [{ street_id: '9007199254740993' }] } as unknown as Partial<MapResponse>));
  } });
  const result = await client.getMap(viewport.bbox, viewport.zoom, 'strict');
  const url = new URL(calledUrl);
  assert.equal(url.pathname, '/api/map');
  assert.equal(url.searchParams.get('bbox'), viewport.bbox.join(','));
  assert.equal(url.searchParams.get('rule'), 'strict');
  assert.equal(authorization, 'Bearer opaque');
  assert.equal(result.streets[0]?.street_id, '9007199254740993');
});

test('multipart upload leaves boundary header to fetch and returns duplicate state', async () => {
  const store = sessionStore('opaque');
  let init: RequestInit | undefined;
  const nativeFormData = globalThis.FormData;
  class FakeFormData {
    values: Array<[string, unknown]> = [];
    append(name: string, value: unknown) { this.values.push([name, value]); }
  }
  globalThis.FormData = FakeFormData as unknown as typeof FormData;
  try {
    const client = createExploreApi({ baseUrl: 'https://api.test', sessionStore: store, fetchImpl: async (_url, request) => {
      init = request;
      return Response.json({ id: '9007199254740993', status: 'succeeded', job_id: '4', duplicate: true });
    } });
    const result = await client.upload({ uri: 'file:///route.gpx', name: 'route.gpx', mimeType: 'application/gpx+xml' });
    assert.equal(result.duplicate, true);
    assert.equal(new Headers(init?.headers).has('Content-Type'), false);
    assert.deepEqual((init?.body as unknown as FakeFormData).values[0], ['file', { uri: 'file:///route.gpx', name: 'route.gpx', type: 'application/gpx+xml' }]);
  } finally {
    globalThis.FormData = nativeFormData;
  }
});

test('explore client rejects delayed successful response from replaced account', async () => {
  const store = sessionStore('account-a');
  const pending = deferred<Response>();
  const client = createExploreApi({ baseUrl: 'https://api.test', sessionStore: store, fetchImpl: async () => pending.promise });
  const request = client.getProgress('normal');
  store.current = 'account-b';
  pending.resolve(Response.json(progressResult({ state: 'ready' })));
  await assert.rejects(request, (error: unknown) => error instanceof ApiError && error.kind === 'stale-session');
  assert.equal(store.current, 'account-b');
});

test('fixture Explore API stays local and disables uploads', async () => {
  const fixture = createFixtureExploreApi();
  const map = await fixture.getMap(viewport.bbox, viewport.zoom, 'normal');
  assert.equal(map.geography_state, 'geography_pending');
  assert.deepEqual(map.tracks, []);
  await assert.rejects(fixture.upload({ uri: 'file:///route.gpx', name: 'route.gpx' }), /fixture mode/);
});

test('viewport refresh ignores an older map and progress response', async () => {
  const oldMap = deferred<MapResponse>();
  const oldProgress = deferred<ProgressResponse>();
  const service = api({
    getMap: (_bbox, _zoom, rule) => rule === 'normal' ? oldMap.promise : Promise.resolve(mapResult({ geography_state: 'geography_pending' })),
    getProgress: (rule) => rule === 'normal' ? oldProgress.promise : Promise.resolve(progressResult({ state: 'unsupported-geography', datasets: [] })),
  });
  const store = new ExploreStore(service);
  const oldRequest = store.refreshViewport(viewport);
  store.setRule('strict');
  await new Promise((resolve) => setTimeout(resolve, 0));
  oldMap.resolve(mapResult({ streets: [{ completed: false }] } as unknown as Partial<MapResponse>));
  oldProgress.resolve(progressResult({ state: 'ready' }));
  await oldRequest;
  assert.equal(store.getState().rule, 'strict');
  assert.equal(store.getState().map?.geography_state, 'geography_pending');
  assert.equal(store.getState().progress?.state, 'unsupported-geography');
});

test('pending progress retains null counts instead of showing zero-like values', async () => {
  const store = new ExploreStore(api({ getProgress: async () => progressResult() }));
  await store.refreshViewport(viewport);
  const dataset = store.getState().progress?.datasets[0];
  assert.equal(dataset?.eligible_streets, null);
  assert.equal(dataset?.completed_streets, null);
  assert.equal(dataset?.visited_node_count, null);
});

test('upload displays duplicates and refreshes after terminal processing', async () => {
  let mapReads = 0;
  let progressReads = 0;
  const file: GpxFile = { uri: 'file:///run.gpx', name: 'run.gpx' };
  const store = new ExploreStore(api({
    getMap: async () => { mapReads++; return mapResult(); },
    getProgress: async () => { progressReads++; return progressResult({ state: 'ready', datasets: [] }); },
    upload: async () => ({ id: 'upload-1', status: 'succeeded', job_id: 'job-1', duplicate: true } as UploadResponse),
  }));
  await store.refreshViewport(viewport);
  const before = mapReads;
  await store.uploadFile(file);
  assert.equal(store.getState().uploads[0]?.duplicate, true);
  assert.equal(store.getState().uploads[0]?.status, 'succeeded');
  assert.equal(mapReads, before + 1);
  assert.equal(progressReads, 2);
});

test('upload status polling is bounded while queued', async () => {
  const store = new ExploreStore(api({
    upload: async () => ({ id: 'upload-1', status: 'queued', job_id: 'job-1', duplicate: false } as UploadResponse),
    getUpload: async () => uploadResponse({ id: 'upload-1', status: 'processing' }),
  }));
  await store.uploadFile({ uri: 'file:///a.gpx', name: 'a.gpx' });
  for (let index = 0; index < 31; index++) await store.pollUploads();
  assert.equal(store.getState().uploads[0]?.polls, 30);
  assert.equal(store.getState().uploads[0]?.polling, 'stopped');
});

test('ready coverage with queued imports remains bounded', async () => {
  let reads = 0;
  const store = new ExploreStore(api({
    getProgress: async () => { reads++; return progressResult({ state: 'ready', pending_imports: 1, datasets: [] }); },
  }));
  await store.refreshViewport(viewport);
  for (let index = 0; index < 30; index++) await store.refreshForeground();
  assert.equal(reads, 31);
  assert.equal(store.getState().foregroundPollingStopped, true);
  await store.refreshForeground();
  assert.equal(reads, 31);
});

test('queued-source and pending-match datasets keep one shared poll budget', async () => {
  let reads = 0;
  const pendingDataset = { ...progressResult().datasets[0]!, state: 'pending' as const, pending_sources: 0 };
  const store = new ExploreStore(api({ getProgress: async () => {
    reads++;
    return progressResult({ state: 'ready', pending_imports: 0, datasets: [pendingDataset] });
  } }));
  await store.refreshProgress();
  for (let index = 0; index < 30; index++) await store.refreshForeground();
  assert.equal(reads, 31);
  assert.equal(store.getState().foregroundPollingStopped, true);
});

test('foreground refresh does not overlap slow progress requests', async () => {
  let reads = 0;
  const slow = deferred<ProgressResponse>();
  const store = new ExploreStore(api({ getProgress: async () => {
    reads++;
    return reads === 1 ? progressResult({ state: 'pending' }) : slow.promise;
  } }));
  await store.refreshProgress();
  const running = store.refreshForeground();
  await store.refreshForeground();
  assert.equal(reads, 2);
  slow.resolve(progressResult({ state: 'pending' }));
  await running;
});

test('foreground refresh cannot publish the old viewport during the camera debounce', async () => {
  let progressReads = 0;
  let mapReads = 0;
  const waiting = deferred<ProgressResponse>();
  const nextViewport: Viewport = { bbox: [3.7, 51.1, 3.9, 51.2], zoom: 14 };
  const store = new ExploreStore(api({
    getMap: async (bbox) => { mapReads++; return mapResult({ bbox: bbox as MapResponse['bbox'] }); },
    getProgress: async () => ++progressReads === 1 ? progressResult() : waiting.promise,
  }));
  await store.refreshViewport(viewport);
  const foreground = store.refreshForeground();
  store.invalidateViewport();
  waiting.resolve(progressResult());
  await foreground;
  assert.equal(mapReads, 1);
  assert.equal(store.getState().map, null);
  await store.refreshViewport(nextViewport);
  assert.equal(mapReads, 2);
  assert.deepEqual(store.getState().map?.bbox, nextViewport.bbox);
});

test('manual retry resumes a source status check after the polling budget stops', async () => {
  let status: UploadStatusResponse = uploadResponse({ id: 'upload-1', status: 'processing' });
  const store = new ExploreStore(api({
    upload: async () => ({ id: 'upload-1', status: 'queued', job_id: 'job-1', duplicate: false } as UploadResponse),
    getUpload: async () => status,
  }));
  await store.uploadFile({ uri: 'file:///a.gpx', name: 'a.gpx' });
  for (let index = 0; index < 30; index++) await store.pollUploads();
  assert.equal(store.getState().uploads[0]?.polling, 'stopped');
  status = uploadResponse({ id: 'upload-1', status: 'succeeded', activity_id: 'activity-7' });
  await store.retry();
  assert.equal(store.getState().uploads[0]?.polls, 1);
  assert.equal(store.getState().uploads[0]?.status, 'succeeded');
  assert.equal(store.getState().uploads[0]?.activity_id, 'activity-7');
});

test('reset releases old account polling without allowing old finally to unlock new polling', async () => {
  const accountA = deferred<UploadStatusResponse>();
  const accountB = deferred<UploadStatusResponse>();
  let calls = 0;
  let uploadNumber = 0;
  const store = new ExploreStore(api({
    upload: async () => ({ id: ++uploadNumber === 1 ? 'a' : 'b', status: 'queued', job_id: 'job', duplicate: false } as UploadResponse),
    getUpload: async (id) => { calls++; return id === 'a' ? accountA.promise : accountB.promise; },
  }));
  await store.uploadFile({ uri: 'file:///a.gpx', name: 'a.gpx' });
  const oldPoll = store.pollUploads();
  store.reset();
  await store.uploadFile({ uri: 'file:///b.gpx', name: 'b.gpx' });
  const newPoll = store.pollUploads();
  assert.equal(calls, 2);
  accountA.resolve(uploadResponse({ id: 'a', status: 'succeeded' }));
  await oldPoll;
  const thirdPoll = store.pollUploads();
  assert.equal(calls, 2);
  accountB.resolve(uploadResponse({ id: 'b', status: 'succeeded' }));
  await Promise.all([newPoll, thirdPoll]);
});

test('map refreshes when matching finishes after upload polling stopped', async () => {
  let mapReads = 0;
  let progressReads = 0;
  const store = new ExploreStore(api({
    getMap: async () => { mapReads++; return mapResult({ node_state: mapReads === 1 ? 'pending' : 'ready' }); },
    getProgress: async () => ++progressReads === 1 ? progressResult() : progressResult({ state: 'ready', datasets: [] }),
  }));
  await store.refreshViewport(viewport);
  const previousReads = mapReads;
  await store.refreshForeground();
  assert.equal(progressReads, 2);
  assert.equal(mapReads, previousReads + 1);
  assert.equal(store.getState().map?.node_state, 'ready');
});

test('401 expires exploration state while old-session responses cannot repopulate it', async () => {
  const pending = deferred<MapResponse>();
  const store = new ExploreStore(api({
    getMap: () => pending.promise,
    getProgress: async () => { throw new ApiError('sign-in-required', 'expired', 401); },
  }));
  const request = store.refreshViewport(viewport);
  await Promise.resolve();
  pending.resolve(mapResult({ tracks: [{ activity_id: '123', geometry: { type: 'MultiLineString', coordinates: [] } }] } as unknown as Partial<MapResponse>));
  await request;
  await Promise.resolve();
  assert.equal(store.getState().map, null);
  assert.equal(store.getState().mapStatus, 'sign-in-required');
  assert.deepEqual(store.getState().uploads, []);
});

test('account reset fences pending viewport data', async () => {
  const pending = deferred<MapResponse>();
  const store = new ExploreStore(api({ getMap: () => pending.promise }));
  const request = store.refreshViewport(viewport);
  store.reset();
  pending.resolve(mapResult({ cities: [{ id: '1' }] } as unknown as Partial<MapResponse>));
  await request;
  assert.equal(store.getState().map, null);
  assert.equal(store.getState().viewport, null);
});
