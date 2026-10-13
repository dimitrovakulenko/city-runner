import type { CoveragePreparationResponse } from './generated';
import { createApiRequest } from './client';
import type { SessionStore } from './client';

type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export interface CoveragePreparationApi {
  getStatus(signal?: AbortSignal): Promise<CoveragePreparationResponse>;
  retry(signal?: AbortSignal): Promise<void>;
}

export function createCoveragePreparationApi(options: { baseUrl: string; sessionStore: SessionStore; fetchImpl?: FetchLike }): CoveragePreparationApi {
  const request = createApiRequest({ ...options, timeoutMs: 10_000 });
  return {
    getStatus(signal) { return request<CoveragePreparationResponse>('/api/coverage-preparation', { signal }); },
    retry(signal) { return request<void>('/api/coverage-preparation/retry', { method: 'POST', signal }); },
  };
}
