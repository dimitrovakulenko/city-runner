import { ActivityStore } from '../../mobile/src/activityStore';
import { ExploreStore } from '../../mobile/src/exploreStore';
import { CityExplorerStore } from '../../mobile/src/cityExplorerStore';
import { ImportStore } from '../../mobile/src/importStore';
import { CorrectionStore } from '../../mobile/src/correctionStore';
import { createActivityApi, createApiRequest } from '../../mobile/src/api/client';
import { createExploreApi } from '../../mobile/src/api/explore';
import { createCityExplorerApi } from '../../mobile/src/api/cities';
import { createImportBatchApi } from '../../mobile/src/api/importBatches';
import { createCorrectionApi } from '../../mobile/src/api/corrections';
import type { ImportBatchItemResponse } from '../../mobile/src/api/generated';
import { BrowserFiles } from './files';
import { BrowserSessions } from './session';

export function createRuntime(sessions: BrowserSessions, baseUrl = '', fetchImpl = globalThis.fetch) {
  const options = { baseUrl, sessionStore: sessions, fetchImpl };
  const files = new BrowserFiles();
  const api = createActivityApi(options);
  const activities = new ActivityStore(api);
  const explore = new ExploreStore(createExploreApi(options));
  const cities = new CityExplorerStore(createCityExplorerApi(options));
  const imports = new ImportStore(createBrowserImportApi(options, files), () => crypto.randomUUID(), () => {
    void activities.loadPage(); void explore.refreshAfterCorrection(); void cities.refreshAfterCorrection();
  });
  const corrections = new CorrectionStore(createCorrectionApi(options), (operation, id) => {
    if (operation === 'activity-delete') activities.activityDeleted(id);
    void explore.refreshAfterCorrection(); void cities.refreshAfterCorrection(); void imports.refresh();
  });
  const reset = () => { files.clear(); activities.reset(); explore.reset(); cities.reset(); imports.reset(); corrections.reset(); };
  sessions.subscribe(reset);
  return { sessions, files, api, activities, explore, cities, imports, corrections, reset };
}

export type Runtime = ReturnType<typeof createRuntime>;

export function createBrowserImportApi(options: Parameters<typeof createImportBatchApi>[0], files: BrowserFiles) {
  const upload = createApiRequest({ ...options, timeoutMs: 120_000 });
  return {
    ...createImportBatchApi(options),
    async uploadItem(batchId: string, itemId: string, selection: Parameters<BrowserFiles['form']>[0], signal?: AbortSignal) {
      const result = await upload<ImportBatchItemResponse>(`/api/import-batches/${encodeURIComponent(batchId)}/items/${encodeURIComponent(itemId)}/upload`,
        { method: 'POST', body: files.form(selection), signal });
      files.release(selection.uri); return result;
    },
  };
}
