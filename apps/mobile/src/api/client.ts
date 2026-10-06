import type {
  ActivityDetail,
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

// D07 replaces this adapter with the platform secure-storage implementation.
export const noSessionStore: SessionStore = {
  async getToken() { return null; },
  async setToken() { throw new Error('Secure session storage is not configured.'); },
  async clear() {},
  async clearIfCurrent() { return false; },
};

export interface ActivityApi {
  listActivities(input?: { page?: number; pageSize?: number; query?: string }): Promise<ActivityPage>;
  getActivity(id: string): Promise<ActivityDetail>;
  getMe(): Promise<MeResponse>;
  createChallenge(body: ChallengeRequest): Promise<ChallengeResponse>;
  exchange(body: ExchangeRequest): Promise<ExchangeResponse>;
  logout(): Promise<void>;
}

type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export function createActivityApi(options: {
  baseUrl: string;
  sessionStore?: SessionStore;
  fetchImpl?: FetchLike;
}): ActivityApi {
  const baseUrl = options.baseUrl.replace(/\/+$/, '');
  const sessionStore = options.sessionStore ?? noSessionStore;
  const fetchImpl = options.fetchImpl ?? globalThis.fetch;

  async function request<T>(
    path: string,
    init: RequestInit = {},
    authenticated = true,
    retryOnSessionChange = true,
    tokenOverride?: string,
  ): Promise<T> {
    const headers = new Headers(init.headers);
    headers.set('Accept', 'application/json');
    if (init.body !== undefined) headers.set('Content-Type', 'application/json');
    let usedToken: string | null = null;
    if (authenticated) {
      usedToken = tokenOverride ?? await sessionStore.getToken();
      if (!usedToken) throw new ApiError('sign-in-required', 'Sign in to load your activities.', 401);
      headers.set('Authorization', `Bearer ${usedToken}`);
    }

    let response: Response;
    try {
      response = await fetchImpl(`${baseUrl}${path}`, { ...init, headers });
    } catch {
      throw new ApiError('offline', 'Could not reach the server. Check your connection and retry.');
    }
    if (response.status === 401 && authenticated) {
      const cleared = usedToken ? await sessionStore.clearIfCurrent(usedToken) : false;
      if (!cleared && retryOnSessionChange) {
        return request<T>(path, init, authenticated, false);
      }
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
    if (response.status === 204) return undefined as T;
    try {
      return await response.json() as T;
    } catch {
      throw new ApiError('http', 'The server returned an invalid response.', response.status);
    }
  }

  return {
    listActivities({ page = 1, pageSize = 20, query = '' } = {}) {
      const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
      if (query.trim()) params.set('q', query.trim());
      return request<ActivityPage>(`/api/activities?${params.toString()}`);
    },
    getActivity(id) {
      return request<ActivityDetail>(`/api/activities/${encodeURIComponent(id)}`);
    },
    getMe() {
      return request<MeResponse>('/api/me');
    },
    createChallenge(body) {
      return request<ChallengeResponse>('/api/auth/challenges', {
        method: 'POST', body: JSON.stringify(body),
      }, false);
    },
    exchange(body) {
      return request<ExchangeResponse>('/api/auth/exchange', {
        method: 'POST', body: JSON.stringify(body),
      }, false).then(async (result) => {
        await sessionStore.setToken(result.token);
        return result;
      });
    },
    async logout() {
      const token = await sessionStore.getToken();
      if (!token) throw new ApiError('sign-in-required', 'Sign in to log out.', 401);
      await request<void>('/api/auth/session', { method: 'DELETE' }, true, false, token);
      await sessionStore.clearIfCurrent(token);
    },
  };
}
