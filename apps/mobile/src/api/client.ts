import type {
  ActivityDetail,
  ActivityImpactPage,
  ActivityPage,
  ChallengeRequest,
  ChallengeResponse,
  ExchangeRequest,
  ExchangeResponse,
  MeResponse,
} from './generated';

export type ApiErrorKind = 'sign-in-required' | 'stale-session' | 'offline' | 'http';

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
  listActivities(input?: { page?: number; pageSize?: number; query?: string }, signal?: AbortSignal): Promise<ActivityPage>;
  getActivity(id: string, signal?: AbortSignal): Promise<ActivityDetail>;
  getActivityImpact(id: string, input: { datasetId: string; rule: 'normal' | 'strict'; page: number; pageSize: number }, signal?: AbortSignal): Promise<ActivityImpactPage>;
  getMe(): Promise<MeResponse>;
  createChallenge(body: ChallengeRequest): Promise<ChallengeResponse>;
  exchange(body: ExchangeRequest, options?: { persist?: boolean }): Promise<ExchangeResponse>;
  logout(): Promise<void>;
}

type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

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
    responseMode: 'json' | 'text' = 'json',
  ): Promise<T> {
    const headers = new Headers(init.headers);
    headers.set('Accept', responseMode === 'text' ? 'application/gpx+xml, text/plain' : 'application/json');
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
      if (response.status === 204) {
        if (authenticated && await sessionStore.getToken() !== usedToken) {
          throw new ApiError('stale-session', 'Your session changed while this request was running.');
        }
        return undefined as T;
      }
      let result: T;
      try {
        result = (responseMode === 'text' ? await response.text() : await response.json()) as T;
      } catch {
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

export function createActivityApi(options: Parameters<typeof createApiRequest>[0]): ActivityApi {
  const request = createApiRequest(options);
  const sessionStore = options.sessionStore ?? noSessionStore;
  return {
    listActivities({ page = 1, pageSize = 20, query = '' } = {}, signal) {
      const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
      if (query.trim()) params.set('q', query.trim());
      return request<ActivityPage>(`/api/activities?${params.toString()}`, { signal });
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
