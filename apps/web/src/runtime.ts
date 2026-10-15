import { ActivityStore } from '../../mobile/src/activityStore';
import { ActivitySourceStore } from '../../mobile/src/activitySourceStore';
import { createActivitySourceApi } from '../../mobile/src/api/activitySources';
import { ExploreStore } from '../../mobile/src/exploreStore';
import { CityExplorerStore } from '../../mobile/src/cityExplorerStore';
import { ImportStore } from '../../mobile/src/importStore';
import { CorrectionStore } from '../../mobile/src/correctionStore';
import { RoutePlannerStore } from '../../mobile/src/routePlannerStore';
import { AccountStore } from '../../mobile/src/accountStore';
import { SyncStatusStore } from '../../mobile/src/syncStatusStore';
import { CoveragePreparationStore } from '../../mobile/src/coveragePreparationStore';
import { createCoveragePreparationApi } from '../../mobile/src/api/coveragePreparation';
import { createSyncStatusApi } from '../../mobile/src/api/syncStatus';
import { createAccountApi } from '../../mobile/src/api/account';
import { createRouteApi } from '../../mobile/src/api/routes';
import { createActivityApi, createApiRequest } from '../../mobile/src/api/client';
import { createAnalyticsApi } from '../../mobile/src/api/analytics';
import { createExploreApi } from '../../mobile/src/api/explore';
import { createCityExplorerApi } from '../../mobile/src/api/cities';
import { createDatasetApi } from '../../mobile/src/api/datasets';
import { createImportBatchApi } from '../../mobile/src/api/importBatches';
import { createHistoryImportApi } from '../../mobile/src/api/importHistories';
import { createCorrectionApi } from '../../mobile/src/api/corrections';
import type { ActivityFilters, ImportBatchItemResponse } from '../../mobile/src/api/generated';
import { BrowserFiles } from './files';
import { BrowserSessions } from './session';

export function createRuntime(sessions: BrowserSessions, baseUrl = '', fetchImpl = globalThis.fetch) {
  const options = { baseUrl, sessionStore: sessions, fetchImpl };
  const files = new BrowserFiles();
  const api = createActivityApi(options);
  const analytics = createAnalyticsApi(options);
  const activities = new ActivityStore(api);
  const activitySources = new ActivitySourceStore(createActivitySourceApi(options), (activityId) => {
    const state = activitySources.getState();
    if (state.activityMissing && state.activityId === activityId && activities.getState().selectedId === activityId) activities.activityDeleted(activityId);
    void sync.retryRefresh();
  });
  let sourceSelection: string | null = null;
  activities.subscribe((state) => {
    if (state.selectedId === sourceSelection) return;
    sourceSelection = state.selectedId;
    if (sourceSelection === null) activitySources.clearSelection();
    else void activitySources.load(sourceSelection);
  });
  const account = new AccountStore(createAccountApi(options), sessions);
  const explore = new ExploreStore(createExploreApi(options));
  const cities = new CityExplorerStore(createCityExplorerApi(options), createDatasetApi(options));
  const preparation = new CoveragePreparationStore(createCoveragePreparationApi(options));
  const planner = new RoutePlannerStore(createRouteApi(options));
  const imports = new ImportStore(createBrowserImportApi(options, files, false), () => crypto.randomUUID(), () => {
    void sync.retryRefresh();
  }, createHistoryImportApi(options), (uri) => files.release(uri));
  const corrections = new CorrectionStore(createCorrectionApi(options), (operation, id) => {
    if (operation === 'activity-delete') activities.activityDeleted(id);
    void sync.retryRefresh();
  });
  const sync = new SyncStatusStore(createSyncStatusApi(options), async (_status, generation) => {
    const [activityReady, mapReady, sourceReady] = await Promise.all([
      activities.refreshAfterSync(), explore.refreshAfterSync(), activitySources.refresh(), activities.loadFilterOptions(),
      cities.refreshAfterCorrection(), imports.refresh(), preparation.refresh(),
    ]);
    if (generation !== sync.getAccountGeneration()) return false;
    const city = cities.getState();
    const activity = activities.getState(); const map = explore.getState();
    if (activity.listError || activity.detailError || activity.impactError || activity.filterOptionsError ||
      map.progressStatus === 'error' || map.progressStatus === 'offline' || map.mapStatus === 'error' || map.mapStatus === 'offline' ||
      activitySources.getState().activityId !== null && !activitySources.getState().activityMissing && activitySources.getState().error ||
      city.progressError || city.datasetError || city.selectedDatasetId && city.cityError || city.selectedCityId && city.streetError ||
      city.selectedStreetId && (city.detailError || city.contributionError) || imports.getState().error || preparation.getState().error) {
      throw new Error('Some views could not refresh. Retry refresh when connected.');
    }
    return activityReady && mapReady && (sourceReady || activitySources.getState().activityId === null) && activity.filterOptionsStatus === 'ready' &&
      city.progressStatus === 'ready' && !city.datasetError && (!city.selectedDatasetId || !city.cityError) &&
      (!city.selectedCityId || !city.streetError) && (!city.selectedStreetId || !city.detailError && !city.contributionError) &&
      !imports.getState().error && !imports.getState().historyError && preparation.getState().status === 'ready';
  });
  const reset = () => { sync.reset(); preparation.reset(); files.clear(); account.reset(); activitySources.reset(); activities.reset(); explore.reset(); cities.reset(); imports.reset(); corrections.reset(); planner.reset(); };
  sessions.subscribe(reset);
  const applyFilters = (filters: ActivityFilters) => { activities.setFilters(filters); explore.setFilters(filters); };
  return { sessions, files, api, analytics, account, activities, activitySources, explore, cities, preparation, imports, corrections, planner, sync, reset, applyFilters };
}

export type Runtime = ReturnType<typeof createRuntime>;

export function createBrowserImportApi(options: Parameters<typeof createImportBatchApi>[0], files: BrowserFiles, releaseOnUpload = true) {
  const upload = createApiRequest({ ...options, timeoutMs: 120_000 });
  return {
    ...createImportBatchApi(options),
    async uploadItem(batchId: string, itemId: string, selection: Parameters<BrowserFiles['form']>[0], signal?: AbortSignal) {
      const result = await upload<ImportBatchItemResponse>(`/api/import-batches/${encodeURIComponent(batchId)}/items/${encodeURIComponent(itemId)}/upload`,
        { method: 'POST', body: files.form(selection), signal });
      if (releaseOnUpload) files.release(selection.uri); return result;
    },
  };
}
