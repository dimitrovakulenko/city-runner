import assert from 'node:assert/strict';
import test from 'node:test';
import { ActivityStore } from '../src/activityStore';
import { ApiError } from '../src/api/client';
import type { ActivityApi } from '../src/api/client';
import type { ActivityDetail, ActivityPage } from '../src/api/generated';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

const page = (name: string, pageNum = 1): ActivityPage => ({ items: [{ id: '1', name, date: 'today', type: 'running', processed: true, unmapped_points: 0 }], page: pageNum, page_size: 1, total: 1 });
const detail = (id: string): ActivityDetail => ({ id, name: id, date: 'today', type: 'running', processed: true, unmapped_points: 0, tracks: [], timestamps: [], bounds: null });
function fakeApi(overrides: Partial<ActivityApi> = {}): ActivityApi {
  return {
    async listActivities() { return page('default'); },
    async getActivity(id) { return detail(id); },
    async getMe() { throw new Error('unused'); },
    async createChallenge() { throw new Error('unused'); },
    async exchange() { throw new Error('unused'); },
    async logout() {},
    ...overrides,
  };
}

test('new search wins when earlier list response arrives late', async () => {
  const first = deferred<ActivityPage>();
  const api = fakeApi({ listActivities: (input) => input?.query === 'old' ? first.promise : Promise.resolve(page('new')) });
  const store = new ActivityStore(api);
  const oldRequest = store.loadPage('old');
  await store.loadPage('new');
  first.resolve(page('old'));
  await oldRequest;
  assert.equal(store.getState().query, 'new');
  assert.equal(store.getState().items[0]?.name, 'new');
});

test('rapid detail switching ignores the previous response', async () => {
  const previous = deferred<ActivityDetail>();
  const store = new ActivityStore(fakeApi({ getActivity: (id) => id === 'first' ? previous.promise : Promise.resolve(detail(id)) }));
  const oldRequest = store.selectActivity('first');
  await store.selectActivity('second');
  previous.resolve(detail('first'));
  await oldRequest;
  assert.equal(store.getState().selectedId, 'second');
  assert.equal(store.getState().detail?.id, 'second');
});

test('401 clears list and detail and fences outstanding responses', async () => {
  const pendingDetail = deferred<ActivityDetail>();
  let listCalls = 0;
  const store = new ActivityStore(fakeApi({
    listActivities: () => { listCalls++; return listCalls === 1 ? Promise.resolve(page('private')) : Promise.reject(new ApiError('sign-in-required', 'expired', 401)); },
    getActivity: () => pendingDetail.promise,
  }));
  await store.loadPage();
  const detailRequest = store.selectActivity('private-id');
  await store.retryList();
  pendingDetail.resolve(detail('private-id'));
  await detailRequest;
  const state = store.getState();
  assert.equal(state.listStatus, 'sign-in-required');
  assert.deepEqual(state.items, []);
  assert.equal(state.selectedId, null);
  assert.equal(state.detail, null);
});

test('detail 401 clears private list state and invalidates a pending search', async () => {
  const pendingList = deferred<ActivityPage>();
  const store = new ActivityStore(fakeApi({
    listActivities: (input) => input?.query ? pendingList.promise : Promise.resolve(page('private')),
    getActivity: async () => { throw new ApiError('sign-in-required', 'expired', 401); },
  }));
  await store.loadPage();
  const search = store.loadPage('new');
  await store.selectActivity('private-id');
  pendingList.resolve(page('stale'));
  await search;
  assert.equal(store.getState().listStatus, 'sign-in-required');
  assert.deepEqual(store.getState().items, []);
  assert.equal(store.getState().detail, null);
});

test('account reset clears private state and fences outstanding detail', async () => {
  const pending = deferred<ActivityDetail>();
  const store = new ActivityStore(fakeApi({ getActivity: () => pending.promise }));
  await store.loadPage();
  const oldRequest = store.selectActivity('private-id');
  store.reset();
  pending.resolve(detail('private-id'));
  await oldRequest;
  assert.deepEqual(store.getState().items, []);
  assert.equal(store.getState().selectedId, null);
  assert.equal(store.getState().detail, null);
});
