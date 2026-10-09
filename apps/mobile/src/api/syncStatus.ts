import type { SyncFailurePage, SyncStatusResponse } from './generated';
import { createApiRequest } from './client';
import type { SessionStore } from './client';

type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export interface SyncStatusApi {
  getStatus(since: string | null, signal?: AbortSignal): Promise<SyncStatusResponse | null>;
  listFailures(page: number, signal?: AbortSignal): Promise<SyncFailurePage>;
  retryImport(sourceId: string, signal?: AbortSignal): Promise<void>;
  retryCoverage(sourceId: string, signal?: AbortSignal): Promise<void>;
}

export function createSyncStatusApi(options: { baseUrl: string; sessionStore: SessionStore; fetchImpl?: FetchLike }): SyncStatusApi {
  const request = createApiRequest({ ...options, timeoutMs: 10_000 });
  return {
    async getStatus(since, signal) {
      const query = since === null ? '' : `?${new URLSearchParams({ since })}`;
      return (await request<SyncStatusResponse>(`/api/sync/status${query}`, { signal })) ?? null;
    },
    listFailures(page, signal) {
      const query = new URLSearchParams({ page: String(page), page_size: '20' });
      return request<SyncFailurePage>(`/api/sync/failures?${query}`, { signal });
    },
    retryImport(sourceId, signal) {
      return request<void>(`/api/uploads/${encodeURIComponent(sourceId)}/retry`, { method: 'POST', signal });
    },
    retryCoverage(sourceId, signal) {
      return request<void>(`/api/uploads/${encodeURIComponent(sourceId)}/retry-coverage`, { method: 'POST', signal });
    },
  };
}
