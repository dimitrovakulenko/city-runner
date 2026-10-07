import type {
  ActivityDetail,
  ActivityFilters,
  ActivityFilterOptions,
  ActivityImpactPage,
  ActivityPage,
  ChallengeRequest,
  ChallengeResponse,
  ExchangeRequest,
  ExchangeResponse,
  MeResponse,
} from './generated';

export type ApiErrorKind = 'sign-in-required' | 'stale-session' | 'offline' | 'http';
export interface BinaryDownload { blob: Blob; fileName: string }

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status?: number;

  constructor(
    kind: ApiErrorKind,
    message: string,
    status?: number,
  ) {
    super(message);
    this.name = 'ApiError';
    this.kind = kind;
    this.status = status;
  }
}

export interface SessionStore {
  getToken(): Promise<string | null>;
  setToken(token: string): Promise<void>;
  clear(): Promise<void>;
  clearIfCurrent(token: string): Promise<boolean>;
}

// Default for callers without a configured session store; the app injects SecureStore.
export const noSessionStore: SessionStore = {
  async getToken() { return null; },
  async setToken() { throw new Error('Secure session storage is not configured.'); },
  async clear() {},
  async clearIfCurrent() { return false; },
};

export interface ActivityApi {
  listActivities(input?: { page?: number; pageSize?: number; query?: string; filters?: ActivityFilters }, signal?: AbortSignal): Promise<ActivityPage>;
  getActivityFilters(signal?: AbortSignal): Promise<ActivityFilterOptions>;
  getActivity(id: string, signal?: AbortSignal): Promise<ActivityDetail>;
  getActivityImpact(id: string, input: { datasetId: string; rule: 'normal' | 'strict'; page: number; pageSize: number }, signal?: AbortSignal): Promise<ActivityImpactPage>;
  getMe(): Promise<MeResponse>;
  createChallenge(body: ChallengeRequest): Promise<ChallengeResponse>;
  exchange(body: ExchangeRequest, options?: { persist?: boolean }): Promise<ExchangeResponse>;
  logout(): Promise<void>;
}

type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export type CanonicalActivityFilters = Required<ActivityFilters>;

export function canonicalizeActivityFilters(filters?: ActivityFilters): CanonicalActivityFilters {
  const activityType = filters?.activity_type?.trim().toLowerCase() ?? '';
  return {
    date_from: filters?.date_from?.trim() || null,
    date_to: filters?.date_to?.trim() || null,
    activity_type: activityType || null,
    source: filters?.source ?? 'all',
  };
}

export function activityFiltersMatch(actual: ActivityFilters | undefined, expected: CanonicalActivityFilters): boolean {
  if (!actual) return expected.date_from === null && expected.date_to === null && expected.activity_type === null && expected.source === 'all';
  const echoed = canonicalizeActivityFilters(actual);
  return echoed.date_from === expected.date_from && echoed.date_to === expected.date_to &&
    echoed.activity_type === expected.activity_type && echoed.source === expected.source;
}

export function createApiRequest(options: {
  baseUrl: string;
  sessionStore?: SessionStore;
  fetchImpl?: FetchLike;
  timeoutMs?: number;
}) {
  const baseUrl = options.baseUrl.replace(/\/+$/, '');
  const sessionStore = options.sessionStore ?? noSessionStore;
  const fetchImpl = options.fetchImpl ?? globalThis.fetch;

  async function request<T>(
    path: string,
    init: RequestInit = {},
    authenticated = true,
    tokenOverride?: string,
    responseMode: 'json' | 'text' | 'download' | 'accepted' = 'json',
  ): Promise<T> {
    const headers = new Headers(init.headers);
    headers.set('Accept', responseMode === 'text' ? 'application/gpx+xml, text/plain' : responseMode === 'download' ? 'application/zip' : 'application/json');
    if (typeof init.body === 'string') headers.set('Content-Type', 'application/json');
    let usedToken: string | null = null;
    if (authenticated) {
      usedToken = tokenOverride ?? await sessionStore.getToken();
      if (!usedToken) throw new ApiError('sign-in-required', 'Sign in to load your activities.', 401);
      headers.set('Authorization', `Bearer ${usedToken}`);
    }

    if (init.signal?.aborted) throw new ApiError('stale-session', 'Request was cancelled before it could be sent.');
    const controller = new AbortController();
    const abort = () => controller.abort();
    if (init.signal?.aborted) abort();
    else init.signal?.addEventListener('abort', abort, { once: true });
    const timer = setTimeout(abort, options.timeoutMs ?? 30_000);
    try {
      let response: Response;
      try {
        response = await fetchImpl(`${baseUrl}${path}`, { ...init, headers, signal: controller.signal });
      } catch {
        throw new ApiError('offline', 'Could not reach the server. Check your connection and retry.');
      }
      if (response.status === 401 && authenticated) {
        const cleared = usedToken ? await sessionStore.clearIfCurrent(usedToken) : false;
        if (!cleared) throw new ApiError('stale-session', 'Your session changed while this request was running.');
        throw new ApiError('sign-in-required', 'Your session expired. Sign in again.', 401);
      }
      if (!response.ok) {
        let message = `Request failed (${response.status}).`;
        try {
          const body = await response.json() as { detail?: unknown };
          if (typeof body.detail === 'string') message = body.detail;
        } catch {
          // Keep the status-based message when the server returns no JSON body.
        }
        throw new ApiError('http', message, response.status);
      }
      if (responseMode === 'accepted' && response.status !== 202) {
        throw new ApiError('http', 'The server did not return the required account deletion acceptance receipt. Check account status before retrying.', response.status);
      }
      if (responseMode === 'download') {
        if (response.headers.get('Content-Type')?.split(';', 1)[0]?.trim().toLowerCase() !== 'application/zip') {
          throw new ApiError('http', 'The server returned an invalid account archive type.', response.status);
        }
        const contentLength = Number(response.headers.get('Content-Length'));
        if (Number.isFinite(contentLength) && contentLength > 512 * 1024 * 1024) {
          throw new ApiError('http', 'The account archive exceeds the 512 MiB download limit.', response.status);
        }
      }
      if (response.status === 204) {
        if (authenticated && await sessionStore.getToken() !== usedToken) {
          throw new ApiError('stale-session', 'Your session changed while this request was running.');
        }
        return undefined as T;
      }
      let result: T;
      try {
        if (responseMode === 'download') {
          const blob = await response.blob();
          if (blob.size > 512 * 1024 * 1024) {
            throw new ApiError('http', 'The account archive exceeds the 512 MiB download limit.', response.status);
          }
          result = { blob, fileName: safeDownloadFilename(response.headers.get('Content-Disposition')) } as T;
        } else {
          result = (responseMode === 'text' ? await response.text() : await response.json()) as T;
        }
      } catch (error) {
        if (responseMode === 'accepted') {
          throw new ApiError('http', 'Account deletion was accepted but returned an invalid receipt. Check account status before retrying.', response.status);
        }
        if (error instanceof ApiError) throw error;
        if (responseMode === 'download') throw new ApiError('http', 'The server returned an invalid account archive.', response.status);
        throw new ApiError('http', 'The server returned an invalid response.', response.status);
      }
      if (authenticated && await sessionStore.getToken() !== usedToken) {
        throw new ApiError('stale-session', 'Your session changed while this request was running.');
      }
      return result;
    } finally {
      clearTimeout(timer);
      init.signal?.removeEventListener('abort', abort);
    }
  }

  return request;
}

export function safeDownloadFilename(contentDisposition: string | null, fallback = 'city-runner-account.zip'): string {
  const encoded = contentDisposition?.match(/(?:^|;)\s*filename\*\s*=\s*UTF-8''([^;]+)/i)?.[1];
  const quoted = contentDisposition?.match(/(?:^|;)\s*filename\s*=\s*(?:"([^"]*)"|([^;]*))/i);
  let filename = '';
  try { filename = encoded ? decodeURIComponent(encoded.trim()) : (quoted?.[1] ?? quoted?.[2] ?? '').trim(); }
  catch { filename = ''; }
  const basename = filename.replace(/\\/g, '/').split('/').pop()?.replace(/[\u0000-\u001f\u007f]/g, '').trim() ?? '';
  return basename && basename !== '.' && basename !== '..' ? basename.slice(0, 255) : fallback;
}

export function createActivityApi(options: Parameters<typeof createApiRequest>[0]): ActivityApi {
  const request = createApiRequest(options);
  const sessionStore = options.sessionStore ?? noSessionStore;
  return {
    listActivities({ page = 1, pageSize = 20, query = '', filters } = {}, signal) {
      const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
      if (query.trim()) params.set('q', query.trim());
      appendActivityFilters(params, filters);
      return request<ActivityPage>(`/api/activities?${params.toString()}`, { signal });
    },
    getActivityFilters(signal) {
      return request<ActivityFilterOptions>('/api/activities/filters', { signal });
    },
    getActivity(id, signal) {
      return request<ActivityDetail>(`/api/activities/${encodeURIComponent(id)}`, { signal });
    },
    getActivityImpact(id, input, signal) {
      const params = new URLSearchParams({ dataset_id: input.datasetId, rule: input.rule, page: String(input.page), page_size: String(input.pageSize) });
      return request<ActivityImpactPage>(`/api/activities/${encodeURIComponent(id)}/impact?${params}`, { signal });
    },
    getMe() {
      return request<MeResponse>('/api/me');
    },
    createChallenge(body) {
      return request<ChallengeResponse>('/api/auth/challenges', {
        method: 'POST', body: JSON.stringify(body),
      }, false);
    },
    exchange(body, options) {
      return request<ExchangeResponse>('/api/auth/exchange', {
        method: 'POST', body: JSON.stringify(body),
      }, false).then(async (result) => {
        if (options?.persist !== false) await sessionStore.setToken(result.token);
        return result;
      });
    },
    async logout() {
      const token = await sessionStore.getToken();
      if (!token) throw new ApiError('sign-in-required', 'Sign in to log out.', 401);
      await request<void>('/api/auth/session', { method: 'DELETE' }, true, token);
      await sessionStore.clearIfCurrent(token);
    },
  };
}

export function appendActivityFilters(params: URLSearchParams, filters?: ActivityFilters): void {
  if (!filters) return;
  const canonical = canonicalizeActivityFilters(filters);
  if (canonical.date_from) params.set('date_from', canonical.date_from);
  if (canonical.date_to) params.set('date_to', canonical.date_to);
  if (canonical.activity_type) params.set('activity_type', canonical.activity_type);
  if (canonical.source !== 'all') params.set('source', canonical.source);
}
