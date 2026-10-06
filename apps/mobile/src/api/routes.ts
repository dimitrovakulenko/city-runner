import type {
  RouteCreateRequest,
  RouteDetail,
  RoutePage,
  RoutePreviewRequest,
  RoutePreviewResponse,
  RouteUpdateRequest,
} from './generated';
import { createApiRequest } from './client';
import type { SessionStore } from './client';

type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export interface RouteApi {
  preview(body: RoutePreviewRequest, signal?: AbortSignal): Promise<RoutePreviewResponse>;
  list(input: { limit: number; offset: number }, signal?: AbortSignal): Promise<RoutePage>;
  get(id: string, signal?: AbortSignal): Promise<RouteDetail>;
  create(body: RouteCreateRequest, signal?: AbortSignal): Promise<RouteDetail>;
  update(id: string, body: RouteUpdateRequest, signal?: AbortSignal): Promise<RouteDetail>;
  delete(id: string, expectedRevision: number, signal?: AbortSignal): Promise<void>;
  exportGpx(id: string, signal?: AbortSignal): Promise<string>;
}

export function createRouteApi(options: { baseUrl: string; sessionStore: SessionStore; fetchImpl?: FetchLike }): RouteApi {
  const baseUrl = options.baseUrl.replace(/\/+$/, '');
  const request = createApiRequest({ ...options, baseUrl });
  return {
    preview(body, signal) { return request<RoutePreviewResponse>('/api/routes/preview', { method: 'POST', body: JSON.stringify(body), signal }); },
    list({ limit, offset }, signal) { return request<RoutePage>(`/api/routes?${new URLSearchParams({ limit: String(limit), offset: String(offset) })}`, { signal }); },
    get(id, signal) { return request<RouteDetail>(`/api/routes/${encodeURIComponent(id)}`, { signal }); },
    create(body, signal) { return request<RouteDetail>('/api/routes', { method: 'POST', body: JSON.stringify(body), signal }); },
    update(id, body, signal) { return request<RouteDetail>(`/api/routes/${encodeURIComponent(id)}`, { method: 'PUT', body: JSON.stringify(body), signal }); },
    delete(id, expectedRevision, signal) {
      return request<void>(`/api/routes/${encodeURIComponent(id)}?${new URLSearchParams({ expected_revision: String(expectedRevision) })}`, { method: 'DELETE', signal });
    },
    exportGpx(id, signal) { return request<string>(`/api/routes/${encodeURIComponent(id)}/gpx`, { signal }, true, undefined, 'text'); },
  };
}
