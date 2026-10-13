import { createApiRequest } from './client';
import type { SessionStore } from './client';
import type { DatasetItem, DatasetPage } from './generated';

export interface DatasetApi {
  listDatasets(input: { rule: 'normal' | 'strict'; query: string; page: number; pageSize: number }, signal?: AbortSignal): Promise<DatasetPage>;
  getDataset(datasetId: string, rule: 'normal' | 'strict', signal?: AbortSignal): Promise<DatasetItem>;
}

export function createDatasetApi(options: { baseUrl: string; sessionStore: SessionStore; fetchImpl?: typeof fetch }): DatasetApi {
  const request = createApiRequest(options);
  return {
    listDatasets(input, signal) {
      const query = new URLSearchParams({ rule: input.rule, page: String(input.page), page_size: String(input.pageSize) });
      if (input.query.trim()) query.set('q', input.query.trim());
      return request<DatasetPage>(`/api/datasets?${query}`, { signal });
    },
    getDataset(datasetId, rule, signal) {
      const query = new URLSearchParams({ rule });
      return request<DatasetItem>(`/api/datasets/${encodeURIComponent(datasetId)}?${query}`, { signal });
    },
  };
}
