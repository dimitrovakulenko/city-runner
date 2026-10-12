import { createApiRequest } from './client';
import type { SessionStore } from './client';
import type { ActivitySourcePage } from './generated';

export interface ActivitySourceApi {
  listActivitySources(activityId: string, input: { page: number; pageSize: number }, signal?: AbortSignal): Promise<ActivitySourcePage>;
  deleteActivitySource(activityId: string, sourceId: string, signal: AbortSignal): Promise<void>;
}

export function createActivitySourceApi(options: { baseUrl: string; sessionStore: SessionStore; fetchImpl?: typeof fetch }): ActivitySourceApi {
  const request = createApiRequest(options);
  return {
    listActivitySources(activityId, input, signal) {
      const query = new URLSearchParams({ page: String(input.page), page_size: String(input.pageSize) });
      return request<ActivitySourcePage>(`/api/activities/${encodeURIComponent(activityId)}/sources?${query}`, { signal });
    },
    deleteActivitySource(activityId, sourceId, signal) {
      return request<void>(`/api/activities/${encodeURIComponent(activityId)}/sources/${encodeURIComponent(sourceId)}`, { method: 'DELETE', signal });
    },
  };
}
