import assert from 'node:assert/strict';
import test from 'node:test';
import { ActivityStore } from '../src/activityStore';
import { ApiError } from '../src/api/client';
import type { ActivityApi } from '../src/api/client';
import type { ActivityDetail, ActivityImpactPage, ActivityPage } from '../src/api/generated';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

const page = (name: string, pageNum = 1): ActivityPage => ({ items: [{ id: '1', name, date: 'today', type: 'running', processed: true, unmapped_points: 0 }], page: pageNum, page_size: 1, total: 1 });
const detail = (id: string): ActivityDetail => ({ id, name: id, date: 'today', type: 'running', processed: true, unmapped_points: 0, tracks: [], timestamps: [], bounds: null });
const impact = (activityId: string, datasetId: string, rule: 'normal' | 'strict' = 'normal', pageNum = 1, revision = '5', streets: ActivityImpactPage['streets'] = []): ActivityImpactPage => ({
  activity_id: activityId, dataset_id: datasetId, dataset_state: 'active', rule,
  coverage: { status: 'ready', progress_revision: revision, pending_sources: 0, failed_sources: 0, pending_imports: 0, visited_node_count: 2, unsupported_sample_count: 0 },
  history_status: 'ready', supported_nodes: 3, new_nodes: 2, streets_advanced: 1, streets_completed: 0,
  streets, page: pageNum, page_size: 1, total: 2,
});
const impactStreet = (streetId: string, extra: Partial<ActivityImpactPage['streets'][number]> = {}): ActivityImpactPage['streets'][number] => ({
  street_id: streetId, city_id: 'city-1', dataset_id: 'dataset-1', name: streetId, city_name: 'Ghent', eligible_nodes: 4,
  supported_nodes: 2, new_nodes: 1, before_nodes: 1, after_nodes: 2, completed_by_activity: false, current_nodes: 3,
  bounds: [3.7, 51, 3.8, 51.1], ...extra,
});
function fakeApi(overrides: Partial<ActivityApi> = {}): ActivityApi {
  return {
    async listActivities() { return page('default'); },
    async getActivityFilters() { return { activity_types: ['running'], types_truncated: false }; },
    async getActivity(id) { return detail(id); },
    async getActivityImpact(id, input) { return impact(id, input.datasetId, input.rule, input.page); },
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

test('activity filter changes restart pagination and fence old list and detail results', async () => {
  const staleList = deferred<ActivityPage>(); const staleDetail = deferred<ActivityDetail>();
  const seen: Array<string | null> = [];
  const store = new ActivityStore(fakeApi({
    listActivities: (input) => {
      seen.push(input?.filters?.activity_type ?? null);
      return input?.filters?.activity_type ? Promise.resolve(page('filtered')) : staleList.promise;
    },
    getActivity: () => staleDetail.promise,
  }));
  const oldPage = store.loadPage(); const oldDetail = store.selectActivity('old');
  store.setFilters({ date_from: null, date_to: null, activity_type: ' RUNNING ', source: 'all' });
  await new Promise((resolve) => setTimeout(resolve, 0));
  staleList.resolve(page('stale')); staleDetail.resolve(detail('old'));
  await Promise.all([oldPage, oldDetail]);
  assert.deepEqual(seen, [null, 'running']);
  assert.equal(store.getState().page, 1); assert.equal(store.getState().items[0]?.name, 'filtered');
  assert.equal(store.getState().selectedId, null); assert.equal(store.getState().detail, null);
  assert.deepEqual(store.getState().filters, { date_from: null, date_to: null, activity_type: 'running', source: 'all' });
});

test('activity pagination carries the canonical filter selection to every page', async () => {
  const requested: Array<{ page?: number; type: string | null }> = [];
  const store = new ActivityStore(fakeApi({ listActivities: async (input) => {
    requested.push({ page: input?.page, type: input?.filters?.activity_type ?? null });
    return { items: [{ id: String(input?.page), name: `page-${input?.page}`, date: 'today', type: 'running', processed: true, unmapped_points: 0 }], page: input?.page ?? 1, page_size: 1, total: 2 };
  } }));
  store.setFilters({ activity_type: ' running ', source: 'fit' });
  await new Promise((resolve) => setTimeout(resolve, 0));
  await store.loadMore();
  assert.deepEqual(requested.map(({ page, type }) => [page, type]), [[1, 'running'], [2, 'running']]);
  assert.deepEqual(store.getState().items.map((item) => item.name), ['page-1', 'page-2']);
});

test('filter option requests are account fenced and retryable', async () => {
  const old = deferred<{ activity_types: string[]; types_truncated: boolean }>(); let calls = 0;
  const store = new ActivityStore(fakeApi({ getActivityFilters: () => ++calls === 1 ? old.promise : Promise.resolve({ activity_types: ['walking'], types_truncated: false }) }));
  const pending = store.loadFilterOptions(); store.reset(); await store.retryFilterOptions();
  old.resolve({ activity_types: ['private type'], types_truncated: false }); await pending;
  assert.deepEqual(store.getState().filterOptions, { activity_types: ['walking'], types_truncated: false });
  assert.equal(store.getState().filterOptionsStatus, 'ready');
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

test('impact selection automatically loads after activity detail and paged streets append in server order', async () => {
  const calls: number[] = [];
  const store = new ActivityStore(fakeApi({ getActivityImpact: async (id, input) => {
    calls.push(input.page);
    return impact(id, input.datasetId, input.rule, input.page, '5', [impactStreet(input.page === 1 ? 'completed-first' : 'advanced-next')]);
  } }));
  store.selectImpactDataset('dataset-1', 'strict');
  await store.selectActivity('9007199254740993'); await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(calls, [1]); assert.equal(store.getState().impactRule, 'strict');
  assert.deepEqual(store.getState().impact?.streets.map((street) => street.street_id), ['completed-first']);
  await store.loadMoreImpact();
  assert.deepEqual(calls, [1, 2]);
  assert.deepEqual(store.getState().impact?.streets.map((street) => street.street_id), ['completed-first', 'advanced-next']);
  assert.equal(store.getState().impactPage, 2);
});

test('late activity, dataset and rule impact responses cannot replace current selection', async () => {
  const pending = new Map<string, ReturnType<typeof deferred<ActivityImpactPage>>>();
  const store = new ActivityStore(fakeApi({ getActivityImpact: (id, input) => {
    const key = `${id}/${input.datasetId}/${input.rule}`;
    const request = deferred<ActivityImpactPage>(); pending.set(key, request); return request.promise;
  } }));
  await store.selectActivity('activity-a'); store.selectImpactDataset('dataset-a'); await new Promise((resolve) => setTimeout(resolve, 0));
  store.selectImpactDataset('dataset-b'); await new Promise((resolve) => setTimeout(resolve, 0));
  store.selectImpactDataset('dataset-b', 'strict'); await new Promise((resolve) => setTimeout(resolve, 0));
  pending.get('activity-a/dataset-a/normal')!.resolve(impact('activity-a', 'dataset-a'));
  pending.get('activity-a/dataset-b/normal')!.resolve(impact('activity-a', 'dataset-b'));
  pending.get('activity-a/dataset-b/strict')!.resolve(impact('activity-a', 'dataset-b', 'strict', 1, '5', [impactStreet('strict-current')]));
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(store.getState().impactDatasetId, 'dataset-b'); assert.equal(store.getState().impactRule, 'strict');
  assert.deepEqual(store.getState().impact?.streets.map((street) => street.street_id), ['strict-current']);
});

test('late impact from a previous selected activity cannot replace the current activity', async () => {
  const pending = new Map<string, ReturnType<typeof deferred<ActivityImpactPage>>>();
  const store = new ActivityStore(fakeApi({ getActivityImpact: (id, input) => {
    const key = `${id}/${input.datasetId}`; const request = deferred<ActivityImpactPage>(); pending.set(key, request); return request.promise;
  } }));
  store.selectImpactDataset('dataset-1'); await store.selectActivity('activity-old'); await new Promise((resolve) => setTimeout(resolve, 0));
  await store.selectActivity('activity-new'); await new Promise((resolve) => setTimeout(resolve, 0));
  pending.get('activity-old/dataset-1')!.resolve(impact('activity-old', 'dataset-1', 'normal', 1, '5', [impactStreet('old')]));
  pending.get('activity-new/dataset-1')!.resolve(impact('activity-new', 'dataset-1', 'normal', 1, '5', [impactStreet('new')]));
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(store.getState().selectedId, 'activity-new');
  assert.equal(store.getState().impact?.activity_id, 'activity-new'); assert.deepEqual(store.getState().impact?.streets.map((street) => street.street_id), ['new']);
});

test('impact page revision change restarts at page one and replaces the stale page', async () => {
  const calls: number[] = [];
  const store = new ActivityStore(fakeApi({ getActivityImpact: async (id, input) => {
    calls.push(input.page);
    if (input.page === 2) return impact(id, input.datasetId, input.rule, 2, '6', [impactStreet('old-revision-page')]);
    return impact(id, input.datasetId, input.rule, 1, calls.length === 1 ? '5' : '6', [impactStreet(calls.length === 1 ? 'old-page-one' : 'fresh-page-one')]);
  } }));
  store.selectImpactDataset('dataset-1'); await store.selectActivity('activity-1'); await new Promise((resolve) => setTimeout(resolve, 0));
  await store.loadMoreImpact();
  assert.deepEqual(calls, [1, 2, 1]);
  assert.deepEqual(store.getState().impact?.streets.map((street) => street.street_id), ['fresh-page-one']);
  assert.equal(store.getState().impact?.coverage.progress_revision, '6'); assert.equal(store.getState().impactPage, 1);
});

test('unavailable impact metrics remain null and missing support does not become a false zero', async () => {
  const store = new ActivityStore(fakeApi({ getActivityImpact: async (id, input) => ({
    ...impact(id, input.datasetId, input.rule), history_status: 'unknown-dates', new_nodes: null, streets_advanced: null, streets_completed: null,
    streets: [impactStreet('affected', { new_nodes: null, before_nodes: null, after_nodes: null, completed_by_activity: null })], total: 1,
  }) }));
  store.selectImpactDataset('dataset-1'); await store.selectActivity('activity-1'); await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(store.getState().impactStatus, 'ready'); assert.equal(store.getState().impact?.history_status, 'unknown-dates');
  assert.equal(store.getState().impact?.new_nodes, null); assert.equal(store.getState().impact?.streets[0]?.new_nodes, null);

  const pendingStore = new ActivityStore(fakeApi({ getActivityImpact: async (id, input) => ({
    ...impact(id, input.datasetId, input.rule), coverage: { ...impact(id, input.datasetId).coverage, status: 'pending', progress_revision: '7' },
    supported_nodes: null, new_nodes: null, streets_advanced: null, streets_completed: null, streets: [], total: null,
  }) }));
  pendingStore.selectImpactDataset('dataset-1'); await pendingStore.selectActivity('activity-2'); await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(pendingStore.getState().impactStatus, 'ready'); assert.equal(pendingStore.getState().impact?.total, null);
  assert.equal(pendingStore.getState().impact?.supported_nodes, null);
});

test('impact response identity and requested rule must match before publishing', async () => {
  const store = new ActivityStore(fakeApi({ getActivityImpact: async (_id, input) => impact('wrong-activity', input.datasetId, 'normal') }));
  store.selectImpactDataset('dataset-1', 'strict'); await store.selectActivity('activity-1'); await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(store.getState().impact, null); assert.equal(store.getState().impactStatus, 'error');
  assert.match(store.getState().impactError ?? '', /did not match/);
});

test('same-ID deletion invalidates a late impact result immediately', async () => {
  const pending = deferred<ActivityImpactPage>();
  const store = new ActivityStore(fakeApi({ getActivityImpact: () => pending.promise }));
  store.selectImpactDataset('dataset-1'); await store.selectActivity('activity-1'); await new Promise((resolve) => setTimeout(resolve, 0));
  store.activityDeleted('activity-1'); pending.resolve(impact('activity-1', 'dataset-1')); await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(store.getState().selectedId, null); assert.equal(store.getState().impact, null); assert.equal(store.getState().impactStatus, 'idle');
});

test('account reset and impact 401 clear private impact state', async () => {
  const pending = deferred<ActivityImpactPage>();
  const store = new ActivityStore(fakeApi({ getActivityImpact: () => pending.promise }));
  store.selectImpactDataset('dataset-1'); await store.selectActivity('activity-1'); await new Promise((resolve) => setTimeout(resolve, 0));
  store.reset(); pending.resolve(impact('activity-1', 'dataset-1')); await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(store.getState().impact, null); assert.equal(store.getState().impactDatasetId, null);
  const unauthorized = new ActivityStore(fakeApi({ getActivityImpact: async () => { throw new ApiError('sign-in-required', 'expired', 401); } }));
  unauthorized.selectImpactDataset('dataset-1'); await unauthorized.selectActivity('private'); await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(unauthorized.getState().listStatus, 'sign-in-required'); assert.equal(unauthorized.getState().selectedId, null);
  assert.equal(unauthorized.getState().impact, null); assert.equal(unauthorized.getState().impactDatasetId, null);

  const oldOwner = deferred<ActivityImpactPage>();
  const switched = new ActivityStore(fakeApi({ getActivityImpact: (id, input) => id === 'old-owner' ? oldOwner.promise : Promise.resolve(impact(id, input.datasetId)) }));
  switched.selectImpactDataset('dataset-old'); await switched.selectActivity('old-owner'); await new Promise((resolve) => setTimeout(resolve, 0));
  switched.reset(); switched.selectImpactDataset('dataset-new'); await switched.selectActivity('new-owner'); await new Promise((resolve) => setTimeout(resolve, 0));
  oldOwner.reject(new ApiError('sign-in-required', 'old token expired', 401)); await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(switched.getState().selectedId, 'new-owner'); assert.equal(switched.getState().impact?.activity_id, 'new-owner');
  assert.equal(switched.getState().impactStatus, 'ready');
});

test('refreshImpact clears immediately and reloads the current activity and dataset', async () => {
  const calls: string[] = [];
  const store = new ActivityStore(fakeApi({ getActivityImpact: async (id, input) => {
    calls.push(`${id}/${input.datasetId}`); return impact(id, input.datasetId);
  } }));
  store.selectImpactDataset('dataset-1'); await store.selectActivity('activity-1'); await new Promise((resolve) => setTimeout(resolve, 0));
  await store.refreshImpact();
  assert.deepEqual(calls, ['activity-1/dataset-1', 'activity-1/dataset-1']);
  assert.equal(store.getState().impactStatus, 'ready');
});

test('impact retry is available after an offline failure', async () => {
  let requests = 0;
  const store = new ActivityStore(fakeApi({ getActivityImpact: async (id, input) => {
    if (requests++ === 0) throw new ApiError('offline', 'offline');
    return impact(id, input.datasetId);
  } }));
  store.selectImpactDataset('dataset-1'); await store.selectActivity('activity-1'); await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(store.getState().impactStatus, 'offline');
  await store.retryImpact();
  assert.equal(store.getState().impactStatus, 'ready'); assert.equal(store.getState().impact?.activity_id, 'activity-1');
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

test('successful deletion clears only the deleted detail and reloads activity history', async () => {
  const reads: string[] = [];
  const store = new ActivityStore(fakeApi({
    listActivities: async (input) => { reads.push(input?.query ?? ''); return page('refreshed'); },
    getActivity: async (id) => detail(id),
  }));
  await store.loadPage('runs');
  await store.selectActivity('deleted-id');
  store.activityDeleted('deleted-id');
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(store.getState().selectedId, null);
  assert.deepEqual(store.getState().items.map((item) => item.name), ['refreshed']);
  assert.deepEqual(reads, ['runs', 'runs']);
});
