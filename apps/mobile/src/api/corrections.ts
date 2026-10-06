import { createApiRequest } from './client';
import type { SessionStore } from './client';

export interface CorrectionApi {
  deleteActivity(id: string, signal: AbortSignal): Promise<void>;
  setManualCompletion(streetId: string, datasetId: string, reason: string, signal: AbortSignal): Promise<void>;
  clearManualCompletion(streetId: string, datasetId: string, signal: AbortSignal): Promise<void>;
}

export function createCorrectionApi(options: { baseUrl: string; sessionStore: SessionStore; fetchImpl?: typeof fetch }): CorrectionApi {
  const request = createApiRequest(options);
  return {
    deleteActivity(id, signal) {
      return request<void>(`/api/activities/${encodeURIComponent(id)}`, { method: 'DELETE', signal });
    },
    setManualCompletion(streetId, datasetId, reason, signal) {
      const query = new URLSearchParams({ dataset_id: datasetId });
      return request<void>(`/api/streets/${encodeURIComponent(streetId)}/manual-completion?${query}`, {
        method: 'PUT', body: JSON.stringify({ reason: reason.trim() }), signal,
      });
    },
    clearManualCompletion(streetId, datasetId, signal) {
      const query = new URLSearchParams({ dataset_id: datasetId });
      return request<void>(`/api/streets/${encodeURIComponent(streetId)}/manual-completion?${query}`, { method: 'DELETE', signal });
    },
  };
}
