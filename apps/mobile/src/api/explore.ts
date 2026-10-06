import type { MapLimits, MapResponse, ProgressResponse, UploadResponse, UploadStatusResponse } from './generated';
import { createApiRequest } from './client';
import type { SessionStore } from './client';

export interface GpxFile {
  uri: string;
  name: string;
  mimeType?: string | null;
  size?: number | null;
}

type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export interface ExploreApi {
  getMap(bbox: [number, number, number, number], zoom: number, rule: 'normal' | 'strict'): Promise<MapResponse>;
  getProgress(rule: 'normal' | 'strict'): Promise<ProgressResponse>;
  upload(file: GpxFile): Promise<UploadResponse>;
  getUpload(id: string): Promise<UploadStatusResponse>;
}

export function createFixtureExploreApi(): ExploreApi {
  const emptyLimit = { returned: 0, limit: 0, truncated: false };
  const limits: MapLimits = { tracks: emptyLimit, streets: emptyLimit, missing_nodes: emptyLimit,
    cities: emptyLimit, points: emptyLimit, geometry_bytes: emptyLimit };
  return {
    async getMap(bbox, zoom) {
      return { bbox, zoom, geography_state: 'geography_pending', pending_imports: 0, dataset_truncated: false,
        datasets: [], cities: [], tracks: [], streets: [], missing_nodes: [], node_state: 'geography_pending',
        limits };
    },
    async getProgress(rule) {
      return { state: 'unsupported-geography', rule, datasets: [], datasets_truncated: false, unmapped_points: 0, pending_imports: 0 };
    },
    async upload() { throw new Error('Uploads are disabled in fixture mode.'); },
    async getUpload() { throw new Error('Uploads are disabled in fixture mode.'); },
  };
}

export function createExploreApi(options: {
  baseUrl: string;
  sessionStore: SessionStore;
  fetchImpl?: FetchLike;
}): ExploreApi {
  const baseUrl = options.baseUrl.replace(/\/+$/, '');
  const sessionStore = options.sessionStore;
  const request = createApiRequest({ baseUrl, sessionStore, fetchImpl: options.fetchImpl });
  const uploadRequest = createApiRequest({ baseUrl, sessionStore, fetchImpl: options.fetchImpl, timeoutMs: 120_000 });

  return {
    getMap(bbox, zoom, rule) {
      const query = new URLSearchParams({ bbox: bbox.join(','), zoom: String(zoom), rule });
      return request<MapResponse>(`/api/map?${query}`);
    },
    getProgress(rule) {
      return request<ProgressResponse>(`/api/progress?${new URLSearchParams({ rule })}`);
    },
    upload(file) {
      const form = new FormData();
      form.append('file', { uri: file.uri, name: file.name, type: file.mimeType ?? 'application/gpx+xml' } as unknown as Blob);
      return uploadRequest<UploadResponse>('/api/uploads', { method: 'POST', body: form });
    },
    getUpload(id) {
      return request<UploadStatusResponse>(`/api/uploads/${encodeURIComponent(id)}`);
    },
  };
}
