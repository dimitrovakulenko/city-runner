import type { ImportHistoryCreate, ImportHistoryPage, ImportHistoryState, ImportBatchPage } from './generated';
import { createApiRequest } from './client';
import type { SessionStore } from './client';

export interface HistoryImportApi {
  createHistory(body: ImportHistoryCreate, signal?: AbortSignal): Promise<ImportHistoryState>;
  listHistories(page: number, signal?: AbortSignal): Promise<ImportHistoryPage>;
  getHistory(id: string, signal?: AbortSignal): Promise<ImportHistoryState>;
  listHistoryBatches(id: string, page: number, signal?: AbortSignal): Promise<ImportBatchPage>;
  stopHistory(id: string, signal?: AbortSignal): Promise<void>;
  resumeHistory(id: string, signal?: AbortSignal): Promise<void>;
}

export function createHistoryImportApi(options: { baseUrl: string; sessionStore: SessionStore; fetchImpl?: typeof fetch }): HistoryImportApi {
  const request = createApiRequest(options);
  return {
    createHistory(body, signal) { return request<ImportHistoryState>('/api/import-histories', { method: 'POST', body: JSON.stringify(body), signal }); },
    listHistories(page, signal) { return request<ImportHistoryPage>(`/api/import-histories?${new URLSearchParams({ page: String(page), page_size: '20' })}`, { signal }); },
    getHistory(id, signal) { return request<ImportHistoryState>(`/api/import-histories/${encodeURIComponent(id)}`, { signal }); },
    listHistoryBatches(id, page, signal) {
      return request<ImportBatchPage>(`/api/import-histories/${encodeURIComponent(id)}/batches?${new URLSearchParams({ page: String(page), page_size: '20' })}`, { signal });
    },
    stopHistory(id, signal) { return request<void>(`/api/import-histories/${encodeURIComponent(id)}/stop`, { method: 'POST', signal }); },
    resumeHistory(id, signal) { return request<void>(`/api/import-histories/${encodeURIComponent(id)}/resume`, { method: 'POST', signal }); },
  };
}

export function createFixtureHistoryImportApi(): HistoryImportApi {
  const disabled = async (): Promise<never> => { throw new Error('History imports are disabled in fixture mode.'); };
  return { createHistory: disabled, listHistories: disabled, getHistory: disabled, listHistoryBatches: disabled,
    stopHistory: disabled, resumeHistory: disabled };
}
