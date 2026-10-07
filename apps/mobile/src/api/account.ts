import type { AccountDeletionRequest, AccountDeletionResponse } from './generated';
import { ApiError, createApiRequest, noSessionStore } from './client';
import type { BinaryDownload, SessionStore } from './client';

export interface AccountApi {
  exportData(signal?: AbortSignal): Promise<BinaryDownload>;
  deleteAccount(body: AccountDeletionRequest, capturedToken: string, signal?: AbortSignal): Promise<AccountDeletionResponse>;
}

export function createAccountApi(options: {
  baseUrl: string;
  sessionStore?: SessionStore;
  fetchImpl?: (input: string, init?: RequestInit) => Promise<Response>;
}): AccountApi {
  const sessionStore = options.sessionStore ?? noSessionStore;
  const request = createApiRequest({ ...options, timeoutMs: 180_000 });
  return {
    exportData(signal) {
      return request<BinaryDownload>('/api/account/export', { signal }, true, undefined, 'download');
    },
    async deleteAccount(body, capturedToken, signal) {
      if (!capturedToken) throw new ApiError('sign-in-required', 'Sign in before deleting this account.', 401);
      if (await sessionStore.getToken() !== capturedToken) {
        throw new ApiError('stale-session', 'Your session changed before account deletion could start.');
      }
      const receipt = await request<AccountDeletionResponse>('/api/account', {
        method: 'DELETE', body: JSON.stringify(body), signal,
      }, true, capturedToken, 'accepted');
      if (!receipt || typeof receipt.id !== 'string' || !receipt.id ||
        (receipt.status !== 'cleanup-pending' && receipt.status !== 'complete')) {
        throw new ApiError('http', 'Account deletion was accepted but returned an invalid receipt. Check account status before retrying.', 202);
      }
      return receipt;
    },
  };
}

export function createFixtureAccountApi(): AccountApi {
  return {
    async exportData() { throw new Error('Fixture mode does not provide account export.'); },
    async deleteAccount() { throw new Error('Fixture mode does not provide account deletion.'); },
  };
}
