import assert from 'node:assert/strict';
import test from 'node:test';
import { AccountStore } from '../src/accountStore';
import { createAccountApi } from '../src/api/account';
import { ApiError, safeDownloadFilename } from '../src/api/client';
import type { SessionStore } from '../src/api/client';
import type { AccountDeletionResponse } from '../src/api/generated';
import { BrowserSessions } from '../../web/src/session';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function sessions(initial: string | null): SessionStore & { current: string | null } {
  return {
    current: initial,
    async getToken() { return this.current; },
    async setToken(value) { this.current = value; },
    async clear() { this.current = null; },
    async clearIfCurrent(value) {
      if (this.current !== value) return false;
      this.current = null; return true;
    },
  };
}

const receipt: AccountDeletionResponse = { id: 'deletion-1', status: 'cleanup-pending' };

test('account export returns a browser Blob with a path-safe Content-Disposition name', async () => {
  const store = sessions('owner-token'); let requestUrl = ''; let auth: string | null = null;
  const api = createAccountApi({ baseUrl: 'https://api.test', sessionStore: store, fetchImpl: async (url, init) => {
    requestUrl = String(url); auth = new Headers(init?.headers).get('Authorization');
    return new Response(new Uint8Array([0x50, 0x4b, 0x03, 0x04]), { headers: {
      'Content-Type': 'application/zip', 'Content-Disposition': "attachment; filename=\"city-runner-account.zip\"; filename*=UTF-8''..%2F..%2Fsafe%20export.zip",
      'Cache-Control': 'no-store', 'Content-Length': '4',
    } });
  } });
  const result = await api.exportData();
  assert.equal(requestUrl, 'https://api.test/api/account/export'); assert.equal(auth, 'Bearer owner-token');
  assert.equal(result.fileName, 'safe export.zip'); assert.deepEqual([...new Uint8Array(await result.blob.arrayBuffer())], [0x50, 0x4b, 0x03, 0x04]);
});

test('download filename parser rejects path components, control characters and malformed encoding', () => {
  assert.equal(safeDownloadFilename('attachment; filename="..\\private\\backup.zip"'), 'backup.zip');
  assert.equal(safeDownloadFilename("attachment; filename*=UTF-8''%E0%A4%A"), 'city-runner-account.zip');
  assert.equal(safeDownloadFilename('attachment; filename="../.."'), 'city-runner-account.zip');
  assert.equal(safeDownloadFilename('attachment; filename="bad\u0000name.zip"'), 'badname.zip');
});

test('export rejects declared archives above 512 MiB before reading their body', async () => {
  let bodyReads = 0;
  const api = createAccountApi({ baseUrl: 'https://api.test', sessionStore: sessions('owner'), fetchImpl: async () => ({
    status: 200, ok: true, headers: new Headers({ 'Content-Type': 'application/zip', 'Content-Length': String(512 * 1024 * 1024 + 1) }),
    async blob() { bodyReads++; return new Blob(); }, async json() { return {}; }, async text() { return ''; },
  } as Response) });
  await assert.rejects(api.exportData(), (error: unknown) => error instanceof ApiError && /512 MiB/.test(error.message));
  assert.equal(bodyReads, 0);
});

test('export rejects chunked archives above 512 MiB after reading their actual Blob size', async () => {
  let bodyReads = 0;
  const api = createAccountApi({ baseUrl: 'https://api.test', sessionStore: sessions('owner'), fetchImpl: async () => ({
    status: 200, ok: true, headers: new Headers({ 'Content-Type': 'application/zip' }),
    async blob() { bodyReads++; return { size: 512 * 1024 * 1024 + 1 } as Blob; }, async json() { return {}; }, async text() { return ''; },
  } as Response) });
  await assert.rejects(api.exportData(), (error: unknown) => error instanceof ApiError && /512 MiB/.test(error.message));
  assert.equal(bodyReads, 1);
});

test('account deletion sends the literal confirmation without clearing before store handoff', async () => {
  const store = sessions('owner-token'); let sentBody = ''; let auth: string | null = null;
  const api = createAccountApi({ baseUrl: 'https://api.test', sessionStore: store, fetchImpl: async (_url, init) => {
    sentBody = String(init?.body); auth = new Headers(init?.headers).get('Authorization');
    return Response.json(receipt, { status: 202 });
  } });
  assert.deepEqual(await api.deleteAccount({ confirmation: 'DELETE' }, 'owner-token'), receipt);
  assert.equal(sentBody, '{"confirmation":"DELETE"}'); assert.equal(auth, 'Bearer owner-token');
  assert.equal(store.current, 'owner-token');
});

test('replacement session survives delayed export and deletion responses', async () => {
  const store = sessions('account-a'); const pending = deferred<Response>();
  const api = createAccountApi({ baseUrl: 'https://api.test', sessionStore: store, fetchImpl: async () => pending.promise });
  const operation = api.deleteAccount({ confirmation: 'DELETE' }, 'account-a');
  store.current = 'account-b'; pending.resolve(Response.json(receipt, { status: 202 }));
  await assert.rejects(operation, (error: unknown) => error instanceof ApiError && error.kind === 'stale-session');
  assert.equal(store.current, 'account-b');

  const exportPending = deferred<Response>();
  const exportApi = createAccountApi({ baseUrl: 'https://api.test', sessionStore: store, fetchImpl: async () => exportPending.promise });
  const exportOperation = exportApi.exportData(); store.current = 'account-c';
  exportPending.resolve(new Response(new Uint8Array([1]), { headers: { 'Content-Type': 'application/zip', 'Content-Disposition': 'attachment; filename="private.zip"' } }));
  await assert.rejects(exportOperation, (error: unknown) => error instanceof ApiError && error.kind === 'stale-session');
  assert.equal(store.current, 'account-c');
});

test('deletion refuses a token captured before the replacement session', async () => {
  const store = sessions('account-b'); let calls = 0;
  const api = createAccountApi({ baseUrl: 'https://api.test', sessionStore: store, fetchImpl: async () => { calls++; return Response.json(receipt, { status: 202 }); } });
  await assert.rejects(api.deleteAccount({ confirmation: 'DELETE' }, 'account-a'), (error: unknown) => error instanceof ApiError && error.kind === 'stale-session');
  assert.equal(calls, 0); assert.equal(store.current, 'account-b');
});

test('malformed deletion receipt reports accepted state without clearing the session', async () => {
  const store = sessions('owner');
  const api = createAccountApi({ baseUrl: 'https://api.test', sessionStore: store, fetchImpl: async () => Response.json({ status: 'cleanup-pending' }, { status: 202 }) });
  await assert.rejects(api.deleteAccount({ confirmation: 'DELETE' }, 'owner'), (error: unknown) => error instanceof ApiError && /accepted/.test(error.message));
  assert.equal(store.current, 'owner');
});

test('account deletion requires the contract 202 response before clearing', async () => {
  const store = sessions('owner');
  const api = createAccountApi({ baseUrl: 'https://api.test', sessionStore: store, fetchImpl: async () => Response.json(receipt, { status: 200 }) });
  await assert.rejects(api.deleteAccount({ confirmation: 'DELETE' }, 'owner'), (error: unknown) => error instanceof ApiError && /required account deletion acceptance/.test(error.message));
  assert.equal(store.current, 'owner');
});

test('account store serializes repeated requests and fences old-account completions', async () => {
  const exportResult = deferred<{ blob: Blob; fileName: string }>(); let exportCalls = 0; let deleteCalls = 0;
  const store = new AccountStore({
    exportData() { exportCalls++; return exportResult.promise; },
    async deleteAccount() { deleteCalls++; return receipt; },
  }, sessions('owner'));
  const pendingExport = store.exportAccount();
  assert.equal(await store.exportAccount(), null); assert.equal(await store.deleteAccount('DELETE'), null);
  assert.equal(exportCalls, 1); assert.equal(deleteCalls, 0);
  store.reset(); exportResult.resolve({ blob: new Blob(['private']), fileName: 'city-runner-account.zip' });
  assert.equal(await pendingExport, null); assert.equal(store.getState().exportStatus, 'idle');
  assert.equal(await store.deleteAccount('DELETE'), receipt); assert.equal(store.getState().deleteStatus, 'accepted');
});

test('account store requires exact typed confirmation and reports 401 without deleting', async () => {
  let calls = 0;
  const store = new AccountStore({
    async exportData() { return { blob: new Blob(), fileName: 'city-runner-account.zip' }; },
    async deleteAccount() { calls++; throw new ApiError('sign-in-required', 'Sign in again.', 401); },
  }, sessions('owner'));
  assert.equal(await store.deleteAccount('delete'), null); assert.equal(calls, 0);
  assert.match(store.getState().deleteError ?? '', /Type DELETE/);
  await store.deleteAccount('DELETE');
  assert.equal(calls, 1); assert.equal(store.getState().deleteStatus, 'error');
  assert.equal(store.getState().deleteError, 'Sign in again.');
});

test('account store returns the accepted receipt when session clear resets it synchronously', async () => {
  const session = sessions('owner'); let store!: AccountStore;
  store = new AccountStore({
    async exportData() { return { blob: new Blob(), fileName: 'city-runner-account.zip' }; },
    async deleteAccount() { return receipt; },
  }, session);
  const resetOnClear = session.clearIfCurrent.bind(session);
  session.clearIfCurrent = async (token) => { const result = await resetOnClear(token); if (result) store.reset(); return result; };
  assert.deepEqual(await store.deleteAccount('DELETE'), receipt);
  assert.equal(store.getState().deleteStatus, 'idle');
});

test('BrowserSessions runtime reset does not hide an accepted deletion receipt', async () => {
  const values = new Map<string, string>();
  const storage = {
    getItem(key: string) { return values.get(key) ?? null; },
    setItem(key: string, value: string) { values.set(key, value); },
    removeItem(key: string) { values.delete(key); },
  };
  const sessions = new BrowserSessions(storage);
  await sessions.setToken('owner-token');
  const api = createAccountApi({ baseUrl: 'https://api.test', sessionStore: sessions,
    fetchImpl: async () => Response.json(receipt, { status: 202 }) });
  const store = new AccountStore(api, sessions);
  sessions.subscribe((token) => { if (token === null) store.reset(); });
  assert.deepEqual(await store.deleteAccount('DELETE'), receipt);
  assert.equal(await sessions.getToken(), null);
  assert.equal(store.getState().deleteStatus, 'idle');
});

test('BrowserSessions replacement during deletion preserves the new token and fences its receipt', async () => {
  const values = new Map<string, string>();
  const storage = {
    getItem(key: string) { return values.get(key) ?? null; },
    setItem(key: string, value: string) { values.set(key, value); },
    removeItem(key: string) { values.delete(key); },
  };
  const sessions = new BrowserSessions(storage);
  await sessions.setToken('account-a');
  const response = deferred<Response>();
  const api = createAccountApi({ baseUrl: 'https://api.test', sessionStore: sessions, fetchImpl: async () => response.promise });
  const store = new AccountStore(api, sessions);
  sessions.subscribe(() => store.reset());
  const pending = store.deleteAccount('DELETE');
  await sessions.setToken('account-b');
  response.resolve(Response.json(receipt, { status: 202 }));
  assert.equal(await pending, null);
  assert.equal(await sessions.getToken(), 'account-b');
});

test('replacement immediately after the deletion clear does not receive the old receipt', async () => {
  const values = new Map<string, string>();
  const storage = {
    getItem(key: string) { return values.get(key) ?? null; },
    setItem(key: string, value: string) { values.set(key, value); },
    removeItem(key: string) { values.delete(key); },
  };
  const sessions = new BrowserSessions(storage);
  await sessions.setToken('account-a');
  const api = createAccountApi({ baseUrl: 'https://api.test', sessionStore: sessions,
    fetchImpl: async () => Response.json(receipt, { status: 202 }) });
  const store = new AccountStore(api, sessions); let replaced = false;
  sessions.subscribe((token) => {
    store.reset();
    if (token === null && !replaced) { replaced = true; void sessions.setToken('account-b'); }
  });
  assert.equal(await store.deleteAccount('DELETE'), null);
  assert.equal(await sessions.getToken(), 'account-b');
});
