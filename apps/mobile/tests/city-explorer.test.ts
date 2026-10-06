import assert from 'node:assert/strict';
import test from 'node:test';
import { ApiError } from '../src/api/client';
import type { SessionStore } from '../src/api/client';
import { createCityExplorerApi, createFixtureCityExplorerApi } from '../src/api/cities';
import type { CityExplorerApi } from '../src/api/cities';
import { CityExplorerStore } from '../src/cityExplorerStore';
import type { CityPage, ContributionPage, ProgressDataset, ProgressResponse, StreetDetail, StreetPage } from '../src/api/generated';

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((yes) => { resolve = yes; });
  return { promise, resolve };
}
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
const coverage = (status: 'ready' | 'pending' | 'failed' = 'ready', revision = '5') => ({ status, progress_revision: revision,
  pending_sources: 0, failed_sources: 0, pending_imports: 0, visited_node_count: status === 'ready' ? 2 : null, unsupported_sample_count: 0 });
const dataset = (id: string): ProgressDataset => ({ dataset_id: id, region: id, state: 'ready', progress_revision: '5', visited_node_count: 2,
  unsupported_sample_count: 0, pending_sources: 0, failed_sources: 0, eligible_streets: 1, completed_streets: 0,
  manual_completed_streets: 0, effective_completed_streets: 0, eligible_nodes: 2 });
const progress = (datasets = [dataset('d1')], datasets_truncated = false): ProgressResponse => ({ state: 'ready', rule: 'normal', datasets, datasets_truncated, unmapped_points: 0, pending_imports: 0 });
const cities = (datasetId: string, page = 1, revision = '5', ids = ['c1'], dataset_state: CityPage['dataset_state'] = 'active'): CityPage => ({
  dataset_id: datasetId, dataset_state, rule: 'normal', coverage: coverage('ready', revision), items: ids.map((id) => ({ id, name: id, admin_level: '8', visited_nodes: 1, eligible_nodes: 2, completed_streets: 0, manual_completed_streets: 0, effective_completed_streets: 0, eligible_streets: 1 })), page, page_size: 50, total: ids.length,
});
const streets = (datasetId: string, cityId = 'c1', ids = ['s1'], revision = '5'): StreetPage => ({
  dataset_id: datasetId, city_id: cityId, dataset_state: 'active', rule: 'normal', coverage: coverage('ready', revision), filter: 'all', filter_applied: true, sort: 'name', sort_applied: true,
  items: ids.map((id) => ({ id, dataset_id: datasetId, city_id: cityId, name: id, visited_nodes: 1, eligible_nodes: 2, threshold: 2, state: 'partial', manual_completed: false, manual_reason: null, effective_state: 'partial' })), page: 1, page_size: 50, total: ids.length,
});
const streetDetail = (datasetId: string, id: string, page = 1, revision = '5', remaining: StreetDetail['remaining_nodes'] = [{ id: '9007199254740993', longitude: 3.7, latitude: 51.0 }]): StreetDetail => ({
  id, dataset_id: datasetId, city_id: 'c1', name: id, dataset_state: 'active', rule: 'normal', coverage: coverage('ready', revision),
  visited_nodes: 1, eligible_nodes: 2, threshold: 2, state: 'partial', manual_completed: false, manual_reason: null, effective_state: 'partial', remaining_nodes: remaining, remaining_nodes_page: { page, page_size: 50, total: 2 },
});
const contributions = (datasetId: string, streetId: string, page = 1, revision = '5'): ContributionPage => ({
  dataset_id: datasetId, street_id: streetId, dataset_state: 'active', coverage: coverage('ready', revision), activities_available: true,
  activities: [{ id: '9007199254740997', name: 'Morning run', date: '2026-10-05', type: 'Run', supported_nodes: 1 }], page, page_size: 50, total: 2,
});
function api(overrides: Partial<CityExplorerApi> = {}): CityExplorerApi {
  return { async getProgress() { return progress(); }, async getCities(id, query) { return cities(id, query.page); },
    async getStreets(city, query) { return streets(query.datasetId, city); }, async getStreet(id, query) { return streetDetail(query.datasetId, id, query.page); },
    async getContributions(id, query) { return contributions(query.datasetId, id, query.page); }, ...overrides };
}
function sessions(initial: string | null): SessionStore {
  let token = initial;
  return { async getToken() { return token; }, async setToken(value) { token = value; }, async clear() { token = null; },
    async clearIfCurrent(value) { if (token !== value) return false; token = null; return true; } };
}
async function selectCity(store: CityExplorerStore) {
  await store.refreshProgress();
  store.selectDataset('d1'); await tick();
  store.selectCity('c1'); await tick();
}

test('city API encodes paging/filter queries and uses the opaque bearer with string IDs', async () => {
  const urls: string[] = [];
  let auth = '';
  const apiClient = createCityExplorerApi({ baseUrl: 'https://api.test', sessionStore: sessions('opaque'), fetchImpl: async (url, init) => {
    urls.push(String(url)); auth = new Headers(init?.headers).get('Authorization') ?? ''; return Response.json({});
  } });
  await apiClient.getCities('9007199254740993', { rule: 'strict', q: 'City_% ', page: 2, pageSize: 50 });
  await apiClient.getStreets('9007199254740995', { datasetId: '9007199254740993', rule: 'strict', filter: 'nearly-complete', sort: 'remaining-asc', q: 'Main', page: 3, pageSize: 50 });
  await apiClient.getStreet('9007199254740997', { datasetId: '9007199254740993', rule: 'strict', page: 4, pageSize: 50 });
  await apiClient.getContributions('9007199254740997', { datasetId: '9007199254740993', page: 2, pageSize: 50 });
  assert.equal(auth, 'Bearer opaque');
  assert.equal(new URL(urls[0]!).searchParams.get('dataset_id'), '9007199254740993');
  assert.equal(new URL(urls[0]!).searchParams.get('q'), 'City_%');
  assert.equal(new URL(urls[1]!).searchParams.get('filter'), 'nearly-complete');
  assert.equal(new URL(urls[1]!).searchParams.get('sort'), 'remaining-asc');
  assert.ok(urls[2]!.includes('/api/streets/9007199254740997?'));
  assert.ok(urls[3]!.includes('/api/streets/9007199254740997/contributions?'));
});

test('dataset choice is explicit and old dataset results cannot replace a new selection', async () => {
  const old = deferred<CityPage>();
  const store = new CityExplorerStore(api({ async getProgress() { return progress([dataset('d1'), dataset('d2')]); },
    getCities(id) { return id === 'd1' ? old.promise : Promise.resolve(cities(id, 1, '5', ['city-d2'])); } }));
  await store.refreshProgress();
  assert.equal(store.getState().selectedDatasetId, null);
  store.selectDataset('d1'); store.selectDataset('d2'); await tick();
  old.resolve(cities('d1', 1, '5', ['city-d1'])); await tick();
  assert.equal(store.getState().selectedDatasetId, 'd2');
  assert.equal(store.getState().cities[0]?.id, 'city-d2');
});

test('selected dataset survives a truncated progress list and refresh validates active dataset state', async () => {
  let progressReads = 0;
  const store = new CityExplorerStore(api({ async getProgress() { return ++progressReads === 1 ? progress([dataset('keep')]) : progress([dataset('new')], true); },
    async getCities(id) { return cities(id); } }));
  await store.refreshProgress(); store.selectDataset('keep'); await tick(); await store.refreshProgress();
  assert.equal(store.getState().selectedDatasetId, 'keep');
  assert.ok(store.getState().datasets.some((item) => item.dataset_id === 'keep'));
  const retired = new CityExplorerStore(api({ async getCities(id) { return cities(id, 1, '5', ['old'], 'retired'); } }));
  await retired.refreshProgress(); retired.selectDataset('d1'); await tick();
  assert.equal(retired.getState().cities.length, 0);
  assert.equal(retired.getState().selectedDatasetId, null);
});

test('city search ignores an older search response', async () => {
  const old = deferred<CityPage>();
  const store = new CityExplorerStore(api({ getCities(_id, query) { return query.q ? Promise.resolve(cities('d1', 1, '5', ['Gent'])) : old.promise; } }));
  await store.refreshProgress(); store.selectDataset('d1'); store.setCityQuery('gent'); await tick();
  old.resolve(cities('d1', 1, '5', ['Old result'])); await tick();
  assert.equal(store.getState().cities[0]?.id, 'Gent');
});

test('city and street search/filter pages follow the currently selected rule', async () => {
  const calls: string[] = [];
  const store = new CityExplorerStore(api({
    async getCities(id, query) { calls.push(`city:${query.q}:${query.page}`); return { ...cities(id, query.page, '5', query.page === 1 ? ['c1'] : ['c2']), total: 100 }; },
    async getStreets(city, query) { calls.push(`street:${query.q}:${query.filter}:${query.rule}`); return { ...streets(query.datasetId, city), rule: query.rule, filter: query.filter }; },
  }));
  await store.refreshProgress(); store.selectDataset('d1'); await tick(); store.setCityQuery('gent'); await tick(); await store.loadMoreCities();
  assert.deepEqual(store.getState().cities.map((item) => item.id), ['c1', 'c2']);
  store.selectCity('c1'); await tick(); store.setStreetQuery('main'); store.setStreetFilter('partial'); await tick(); store.setRule('strict'); await tick();
  assert.ok(calls.includes('street:main:partial:normal'));
  assert.ok(calls.includes('street:main:partial:strict'));
});

test('street sort is sent globally before paging and stale sort responses cannot win', async () => {
  const stale = deferred<StreetPage>(); const queries: string[] = [];
  const store = new CityExplorerStore(api({
    async getStreets(city, query) {
      queries.push(`${query.sort}/${query.filter}/${query.page}`);
      if (query.sort === 'name') return stale.promise;
      return { ...streets(query.datasetId, city, ['remaining-first']), filter: query.filter, filter_applied: false,
        sort: query.sort, sort_applied: false, page: query.page, total: 51 };
    },
  }));
  await selectCity(store); store.setStreetFilter('nearly-complete'); store.setStreetSort('remaining-asc'); await tick();
  assert.ok(queries.includes('remaining-asc/nearly-complete/1'));
  assert.equal(store.getState().streetSort, 'remaining-asc'); assert.equal(store.getState().streetSortApplied, false);
  assert.equal(store.getState().streetFilterApplied, false); assert.equal(store.getState().streets[0]?.id, 'remaining-first');
  stale.resolve({ ...streets('d1', 'c1', ['stale-name']), page: 1 }); await tick();
  assert.equal(store.getState().streets[0]?.id, 'remaining-first');
});

test('sorting during a pending page fences the old page and restarts at page one', async () => {
  const pendingPage = deferred<StreetPage>(); const calls: string[] = [];
  const store = new CityExplorerStore(api({
    async getStreets(city, query) {
      calls.push(`${query.sort}:${query.page}`);
      if (query.page === 2) return pendingPage.promise;
      return { ...streets(query.datasetId, city, [query.sort === 'name' ? 'name-first' : 'completion-first']), total: 100,
        sort: query.sort, page: query.page };
    },
  }));
  await selectCity(store); const loadingMore = store.loadMoreStreets(); await tick();
  store.setStreetSort('completion-desc'); await tick();
  pendingPage.resolve({ ...streets('d1', 'c1', ['old-page-two']), page: 2, sort: 'name', total: 100 }); await loadingMore;
  assert.deepEqual(calls.slice(-2), ['name:2', 'completion-desc:1']);
  assert.deepEqual(store.getState().streets.map((street) => street.id), ['completion-first']);
  assert.equal(store.getState().streetPage, 1);
});

test('account reset fences a pending sorted street request', async () => {
  const pending = deferred<StreetPage>();
  const store = new CityExplorerStore(api({ getStreets: () => pending.promise }));
  await selectCity(store); store.setStreetSort('completion-asc'); await tick(); store.reset();
  pending.resolve({ ...streets('d1', 'c1', ['private-sorted']), sort: 'completion-asc' }); await tick();
  assert.equal(store.getState().streetSort, 'name'); assert.equal(store.getState().streets.length, 0);
});

test('opening a city does not cancel an unrelated active-dataset refresh', async () => {
  const slow = deferred<ProgressResponse>();
  let progressCalls = 0;
  const store = new CityExplorerStore(api({ getProgress() { return ++progressCalls === 1 ? Promise.resolve(progress()) : slow.promise; } }));
  await store.refreshProgress(); store.selectDataset('d1'); await tick();
  const refreshing = store.refreshProgress();
  store.selectCity('c1');
  slow.resolve(progress()); await refreshing; await tick();
  assert.equal(store.getState().progressStatus, 'ready');
});

test('a progress revision change while paging restarts city, missing-node, and contribution lists at page one', async () => {
  const cityPages: number[] = [];
  const nodePages: number[] = [];
  const activityPages: number[] = [];
  const store = new CityExplorerStore(api({
    async getCities(id, query) { cityPages.push(query.page); return { ...cities(id, query.page, query.page === 2 ? '6' : query.page === 1 && cityPages.length > 1 ? '6' : '5', [query.page === 2 ? 'old-page-two' : `city-r${cityPages.length}`]), total: 100 }; },
    async getStreets(id, query) { return streets(query.datasetId, id, ['s1']); },
    async getStreet(id, query) { nodePages.push(query.page); return { ...streetDetail(query.datasetId, id, query.page, query.page === 2 ? '6' : query.page === 1 && nodePages.length > 1 ? '6' : '5', [{ id: `node-${nodePages.length}`, longitude: 3.7, latitude: 51 }]), city_id: 'city-r3' }; },
    async getContributions(id, query) { activityPages.push(query.page); return { ...contributions(query.datasetId, id, query.page, query.page === 2 ? '6' : query.page === 1 && activityPages.length > 1 ? '6' : '5'), activities: [{ id: `activity-${activityPages.length}`, name: 'Run', date: '2026-10-05', type: 'Run', supported_nodes: 1 }] }; },
  }));
  await store.refreshProgress(); store.selectDataset('d1'); await tick();
  const cityPageCount = cityPages.length;
  await store.loadMoreCities();
  assert.deepEqual(cityPages.slice(cityPageCount), [2, 1]);
  assert.equal(store.getState().cities[0]?.id, `city-r${cityPages.length}`);
  store.selectCity(store.getState().cities[0]!.id); await tick(); store.selectStreet('s1'); await tick();
  const nodePageCount = nodePages.length;
  const activityPageCount = activityPages.length;
  await store.loadMoreNodes(); await store.loadMoreContributions();
  assert.deepEqual(nodePages.slice(nodePageCount), [2, 1]);
  assert.deepEqual(activityPages.slice(activityPageCount), [2, 1]);
  assert.deepEqual(store.getState().remainingNodes?.map((node) => node.id), [`node-${nodePages.length}`]);
  assert.deepEqual(store.getState().contributions.map((activity) => activity.id), [`activity-${activityPages.length}`]);
});

test('rapid street selection ignores delayed details and contributions from the previous street', async () => {
  const oldDetail = deferred<StreetDetail>();
  const oldContribution = deferred<ContributionPage>();
  const store = new CityExplorerStore(api({
    async getStreets(_city, query) { return streets(query.datasetId, 'c1', ['s-old', 's-new']); },
    getStreet(id, query) { return id === 's-old' ? oldDetail.promise : Promise.resolve(streetDetail(query.datasetId, id)); },
    getContributions(id, query) { return id === 's-old' ? oldContribution.promise : Promise.resolve(contributions(query.datasetId, id)); },
  }));
  await selectCity(store); store.selectStreet('s-old'); store.selectStreet('s-new'); await tick();
  oldDetail.resolve(streetDetail('d1', 's-old')); oldContribution.resolve(contributions('d1', 's-old')); await tick();
  assert.equal(store.getState().detail?.id, 's-new');
  assert.equal(store.getState().contributionAvailable, true);
});

test('map street opens by exact BIGINT IDs outside the current page and back reloads that city page', async () => {
  const datasetId = '9007199254740999'; const cityId = '9007199254740993'; const streetId = '9007199254740997';
  const streetCalls: Array<{ datasetId: string; cityId: string; page: number }> = []; const cityCalls: string[] = [];
  const store = new CityExplorerStore(api({
    async getProgress() { return progress([dataset('listed-region')], true); },
    async getCities(id, query) { cityCalls.push(id); return cities(id, query.page); },
    async getStreet(id, query) { assert.equal(id, streetId); return { ...streetDetail(query.datasetId, id), city_id: cityId, name: 'Map street' }; },
    async getContributions(id, query) { return { ...contributions(query.datasetId, id), street_id: id }; },
    async getStreets(id, query) { streetCalls.push({ datasetId: query.datasetId, cityId: id, page: query.page }); return streets(query.datasetId, id, ['page-street']); },
  }));
  await store.refreshProgress(); await store.openMapStreet(datasetId, cityId, streetId, 'Map city label');
  assert.equal(store.getState().selectedDatasetId, datasetId);
  assert.equal(store.getState().selectedCityId, cityId); assert.equal(store.getState().selectedCityName, 'Map city label');
  assert.equal(store.getState().selectedStreetId, streetId); assert.equal(store.getState().detail?.id, streetId);
  assert.deepEqual(store.getState().datasets.map((item) => item.dataset_id), ['listed-region']);
  assert.deepEqual(store.getState().cities, []);
  store.backToStreets(); await tick();
  assert.deepEqual(streetCalls, [{ datasetId, cityId, page: 1 }]);
  assert.deepEqual(store.getState().streets.map((item) => item.id), ['page-street']);
  store.backToCities(); await tick();
  assert.deepEqual(cityCalls, [datasetId]);
  assert.deepEqual(store.getState().cities.map((item) => item.id), ['c1']);
});

test('map street rejects invalid or wrong-city BIGINT IDs without selecting unrelated detail', async () => {
  let streetReads = 0;
  const store = new CityExplorerStore(api({ async getStreet(id, query) { streetReads++; return { ...streetDetail(query.datasetId, id), city_id: '2' }; } }));
  await store.openMapStreet('1', '01', '2'); await store.openMapStreet('1', '1', '9223372036854775808');
  assert.equal(streetReads, 0); assert.equal(store.getState().selectedStreetId, null);
  await store.openMapStreet('1', '9007199254740993', '9007199254740997');
  assert.equal(streetReads, 1); assert.equal(store.getState().detail, null);
  assert.equal(store.getState().detailStatus, 'error');
  assert.match(store.getState().detailError ?? '', /selected city/);
});

test('an old street detail cannot replace a newer map street selection', async () => {
  const oldDetail = deferred<StreetDetail>();
  const mapDataset = '9007199254740991'; const mapCity = '9007199254740993'; const mapStreet = '9007199254740997';
  const store = new CityExplorerStore(api({
    async getStreets(_city, query) { return streets(query.datasetId, 'c1', ['s1']); },
    getStreet(id, query) { return id === 's1' ? oldDetail.promise : Promise.resolve({ ...streetDetail(query.datasetId, id), city_id: mapCity, name: 'Map result' }); },
  }));
  await selectCity(store); store.selectStreet('s1');
  await store.openMapStreet(mapDataset, mapCity, mapStreet);
  oldDetail.resolve(streetDetail('d1', 's1')); await tick();
  assert.equal(store.getState().selectedStreetId, mapStreet);
  assert.equal(store.getState().detail?.id, mapStreet); assert.equal(store.getState().detail?.name, 'Map result');
});

test('account reset fences a delayed map street response', async () => {
  const pending = deferred<StreetDetail>();
  const store = new CityExplorerStore(api({ getStreet: () => pending.promise }));
  const datasetId = '9007199254740991';
  const opening = store.openMapStreet(datasetId, '9007199254740993', '9007199254740997');
  store.reset(); pending.resolve(streetDetail(datasetId, '9007199254740997')); await opening;
  assert.equal(store.getState().selectedDatasetId, null); assert.equal(store.getState().selectedStreetId, null);
  assert.equal(store.getState().detail, null);
});

test('pending coverage leaves missing nodes and contribution totals unavailable', async () => {
  const store = new CityExplorerStore(api({
    async getStreet(id, query) { return { ...streetDetail(query.datasetId, id), coverage: coverage('pending'), remaining_nodes: null,
      remaining_nodes_page: { page: 1, page_size: 50, total: null }, visited_nodes: null, eligible_nodes: null, state: null }; },
    async getContributions(id, query) { return { ...contributions(query.datasetId, id), coverage: coverage('pending'), activities_available: false, activities: [], total: null }; },
  }));
  await selectCity(store); store.selectStreet('s1'); await tick();
  assert.equal(store.getState().detail?.remaining_nodes, null);
  assert.equal(store.getState().remainingNodes, null);
  assert.equal(store.getState().contributionTotal, null);
  assert.equal(store.getState().contributionAvailable, false);
});

test('account reset fences delayed city results and 401 clears private selection', async () => {
  const pending = deferred<CityPage>();
  const store = new CityExplorerStore(api({ getCities: () => pending.promise }));
  await store.refreshProgress(); store.selectDataset('d1'); store.reset(); pending.resolve(cities('d1')); await tick();
  assert.equal(store.getState().selectedDatasetId, null);
  const unauthorized = new CityExplorerStore(api({ async getCities() { throw new ApiError('sign-in-required', 'expired', 401); } }));
  await unauthorized.refreshProgress(); unauthorized.selectDataset('d1'); await tick();
  assert.equal(unauthorized.getState().cityStatus, 'sign-in-required');
  assert.equal(unauthorized.getState().cities.length, 0);
});

test('fixture city API rejects locally without invoking a real transport', async () => {
  const fixture = createFixtureCityExplorerApi();
  await assert.rejects(fixture.getProgress('normal'), /fixture mode/);
  await assert.rejects(fixture.getCities('d1', { rule: 'normal', q: '', page: 1, pageSize: 50 }), /fixture mode/);
});

test('manual correction refresh reloads effective totals and selected detail without changing GPS progress', async () => {
  let manual = false;
  let progressReads = 0;
  let detailReads = 0;
  let contributionReads = 0;
  const store = new CityExplorerStore(api({
    async getProgress() {
      progressReads++;
      return progress([{ ...dataset('d1'), manual_completed_streets: manual ? 1 : 0, effective_completed_streets: manual ? 1 : 0 }]);
    },
    async getStreets(id, query) { return streets(query.datasetId, id); },
    async getStreet(id, query) {
      detailReads++;
      return { ...streetDetail(query.datasetId, id, query.page), manual_completed: manual, manual_reason: manual ? 'walked and checked' : null,
        effective_state: manual ? 'complete' : 'partial' };
    },
    async getContributions(id, query) { contributionReads++; return contributions(query.datasetId, id, query.page); },
  }));
  await selectCity(store); store.selectStreet('s1'); await tick();
  manual = true;
  await store.refreshAfterCorrection();
  assert.ok(progressReads >= 2);
  assert.ok(detailReads >= 2);
  assert.ok(contributionReads >= 2);
  assert.equal(store.getState().detail?.manual_completed, true);
  assert.equal(store.getState().detail?.manual_reason, 'walked and checked');
  assert.equal(store.getState().detail?.state, 'partial');
  assert.equal(store.getState().detail?.visited_nodes, 1);
  assert.equal(store.getState().remainingNodes?.[0]?.id, '9007199254740993');
});

test('account reset during correction refresh prevents later detail/contribution reads', async () => {
  const progressResponse = deferred<ProgressResponse>();
  let detailReads = 0;
  let contributionReads = 0;
  let progressReads = 0;
  const store = new CityExplorerStore(api({
    getProgress() { return ++progressReads === 1 ? Promise.resolve(progress()) : progressResponse.promise; },
    async getStreet(id, query) { detailReads++; return streetDetail(query.datasetId, id, query.page); },
    async getContributions(id, query) { contributionReads++; return contributions(query.datasetId, id, query.page); },
  }));
  await selectCity(store); store.selectStreet('s1'); await tick();
  const beforeDetailReads = detailReads;
  const beforeContributionReads = contributionReads;
  const refreshing = store.refreshAfterCorrection();
  store.reset();
  progressResponse.resolve(progress());
  await refreshing;
  assert.equal(detailReads, beforeDetailReads);
  assert.equal(contributionReads, beforeContributionReads);
});
