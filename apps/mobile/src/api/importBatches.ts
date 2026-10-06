import type { ImportBatchCreate, ImportBatchItemResponse, ImportBatchPage, ImportBatchResponse } from './generated';
import { createApiRequest } from './client';
import type { SessionStore } from './client';
import type { GpxFile } from './explore';

type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export interface ImportBatchApi {
  createBatch(body: ImportBatchCreate, signal?: AbortSignal): Promise<ImportBatchResponse>;
  listBatches(page: number, signal?: AbortSignal): Promise<ImportBatchPage>;
  getBatch(id: string, signal?: AbortSignal): Promise<ImportBatchResponse>;
  uploadItem(batchId: string, itemId: string, file: GpxFile, signal?: AbortSignal): Promise<ImportBatchItemResponse>;
  stopBatch(id: string, signal?: AbortSignal): Promise<void>;
  resumeBatch(id: string, signal?: AbortSignal): Promise<void>;
  retryUpload(sourceId: string, signal?: AbortSignal): Promise<void>;
}

export function createImportBatchApi(options: { baseUrl: string; sessionStore: SessionStore; fetchImpl?: FetchLike }): ImportBatchApi {
  const baseUrl = options.baseUrl.replace(/\/+$/, '');
  const request = createApiRequest({ ...options, baseUrl });
  const uploadRequest = createApiRequest({ ...options, baseUrl, timeoutMs: 120_000 });
  return {
    createBatch(body, signal) { return request<ImportBatchResponse>('/api/import-batches', { method: 'POST', body: JSON.stringify(body), signal }); },
    listBatches(page, signal) { return request<ImportBatchPage>(`/api/import-batches?${new URLSearchParams({ page: String(page), page_size: '20' })}`, { signal }); },
    getBatch(id, signal) { return request<ImportBatchResponse>(`/api/import-batches/${encodeURIComponent(id)}`, { signal }); },
    uploadItem(batchId, itemId, file, signal) {
      const form = new FormData();
      form.append('file', { uri: file.uri, name: file.name, type: file.mimeType ?? 'application/octet-stream' } as unknown as Blob);
      return uploadRequest<ImportBatchItemResponse>(`/api/import-batches/${encodeURIComponent(batchId)}/items/${encodeURIComponent(itemId)}/upload`, { method: 'POST', body: form, signal });
    },
    stopBatch(id, signal) { return request<void>(`/api/import-batches/${encodeURIComponent(id)}/stop`, { method: 'POST', signal }); },
    resumeBatch(id, signal) { return request<void>(`/api/import-batches/${encodeURIComponent(id)}/resume`, { method: 'POST', signal }); },
    retryUpload(sourceId, signal) { return request<void>(`/api/uploads/${encodeURIComponent(sourceId)}/retry`, { method: 'POST', signal }); },
  };
}

export function createFixtureImportBatchApi(): ImportBatchApi {
  const disabled = async (): Promise<never> => { throw new Error('Imports are disabled in fixture mode.'); };
  return { createBatch: disabled, listBatches: disabled, getBatch: disabled, uploadItem: disabled,
    stopBatch: disabled, resumeBatch: disabled, retryUpload: disabled };
}
