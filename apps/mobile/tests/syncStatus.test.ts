import assert from 'node:assert/strict';
import test from 'node:test';
import { ApiError } from '../src/api/client';
import type { SessionStore } from '../src/api/client';
import type { SyncFailure, SyncFailurePage, SyncStatusResponse } from '../src/api/generated';
import { createSyncStatusApi } from '../src/api/syncStatus';
import type { SyncStatusApi } from '../src/api/syncStatus';
import { SyncStatusStore } from '../src/syncStatusStore';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

const status = (change_token: string): SyncStatusResponse => ({ change_token, activity_count: 0, oldest_activity_date: null, last_import_at: null,
  files: { gpx: { queued: 0, processing: 0, imported: 0, failed: 0, unavailable: 0 }, fit: { queued: 0, processing: 0, imported: 0, failed: 0, unavailable: 0 } },
  coverage: { ready: 0, pending: 0, failed: 0, unavailable: 0 }, batches: { waiting: 0, accepted: 0, duplicates: 0, deleted: 0, stopped: 0 },
  providers: [{ provider: 'garmin', available: false, reason: 'Unavailable.' }, { provider: 'strava', available: false, reason: 'Unavailable.' }] });
const failure = (source_id = '9007199254740993', retry_action: SyncFailure['retry_action'] = 'retry-coverage'): SyncFailure => ({
  source_id, file_kind: 'gpx', activity_id: '42', stage: retry_action === 'retry-import' ? 'import' : 'coverage',
  error: 'Safe error.', retry_action,
});
const failures = (token: string, page = 1, items: SyncFailure[] = [failure()]): SyncFailurePage => ({ items, page, page_size: 20, total: items.length, change_token: token });
function api(overrides: Partial<SyncStatusApi> = {}): SyncStatusApi {
  return { async getStatus() { return status('v1'); }, async listFailures() { return failures('v1'); }, async retryImport() {}, async retryCoverage() {}, ...overrides };
}
function store(apiImpl: SyncStatusApi, onChange: (data: SyncStatusResponse) => Promise<boolean> = async () => true) {
  const value = new SyncStatusStore(apiImpl, onChange);
  value.setEligible(true);
  return value;
}
function sessionStore(initial: string | null): SessionStore & { current: string | null } {
  return { current: initial, async getToken() { return this.current; }, async setToken(token) { this.current = token; }, async clear() { this.current = null; },
    async clearIfCurrent(token) { if (this.current !== token) return false; this.current = null; return true; } };
}

test('sync API sends auth, conditional token, bounded failure paging and the correct retry routes', async () => {
  const seen: Array<{ url: string; method: string; auth: string | null; signal?: AbortSignal }> = [];
  const client = createSyncStatusApi({ baseUrl: 'https://api.test/', sessionStore: sessionStore('opaque'), fetchImpl: async (url, init) => {
    seen.push({ url: String(url), method: init?.method ?? 'GET', auth: new Headers(init?.headers).get('Authorization'), signal: init?.signal as AbortSignal });
    if (String(url).includes('/status')) return seen.length === 1
      ? Response.json(status('token/opaque'))
      : new Response(null, { status: 204 });
    if (String(url).includes('/failures')) return Response.json(failures('token/opaque', 2));
    return new Response(null, { status: 204 });
  } });
  assert.equal((await client.getStatus(null))?.change_token, 'token/opaque');
  assert.equal(await client.getStatus('token/opaque'), null);
  assert.equal((await client.listFailures(2)).page, 2);
  await client.retryImport('9007199254740993'); await client.retryCoverage('9007199254740993');
  const statusUrl = new URL(seen[0]!.url); assert.equal(statusUrl.pathname, '/api/sync/status'); assert.equal(statusUrl.search, '');
  assert.equal(new URL(seen[1]!.url).searchParams.get('since'), 'token/opaque');
  const failuresUrl = new URL(seen[2]!.url); assert.equal(failuresUrl.searchParams.get('page'), '2'); assert.equal(failuresUrl.searchParams.get('page_size'), '20');
  assert.equal(seen.every((request) => request.auth === 'Bearer opaque'), true);
  assert.equal(seen[3]!.url, 'https://api.test/api/uploads/9007199254740993/retry');
  assert.equal(seen[4]!.url, 'https://api.test/api/uploads/9007199254740993/retry-coverage');
  assert.ok(seen[3]!.signal); assert.equal(seen[3]!.method, 'POST');
});

test('first and reconnect reads are unconditional; status requests never overlap and respect cadence', async () => {
  const pending = deferred<SyncStatusResponse | null>(); const since: Array<string | null> = []; let changes = 0;
  const state = store(api({ getStatus(value) { since.push(value); return pending.promise; } }), async () => { changes++; return true; });
  const first = state.pollIfDue(10_000);
  assert.equal(await state.pollIfDue(10_000), false);
  pending.resolve(status('v1')); assert.equal(await first, true);
  assert.equal(changes, 1); assert.deepEqual(since, [null]);
  assert.equal(await state.pollIfDue(12_499), false);
  assert.equal(await state.pollIfDue(12_500), true);
  assert.deepEqual(since, [null, 'v1']);
  state.setEligible(false); state.setEligible(true);
  assert.equal(await state.pollIfDue(20_000), true);
  assert.deepEqual(since, [null, 'v1', null]);
});

test('thrown dependent refresh error does not acknowledge the changed token and retries against the previous token', async () => {
  const since: Array<string | null> = []; let callback = 0;
  const state = store(api({ getStatus(value) { since.push(value); return Promise.resolve(status(since.length === 1 ? 'v1' : 'v2')); } }), async () => {
    if (++callback === 2) throw new Error('view refresh failed');
    return true;
  });
  assert.equal(await state.pollIfDue(10_000), true);
  assert.equal(await state.pollIfDue(12_500), false);
  assert.equal(state.getState().data?.change_token, 'v2');
  assert.equal(state.getState().error, 'view refresh failed');
  assert.equal(await state.pollIfDue(17_500), true);
  assert.deepEqual(since, [null, 'v1', 'v1']); assert.equal(state.getState().data?.change_token, 'v2');
});

test('benign superseded dependent refresh stays unacknowledged without incrementing the error circuit', async () => {
  const since: Array<string | null> = []; let callback = 0;
  const state = store(api({ getStatus(value) { since.push(value); return Promise.resolve(status(since.length === 1 ? 'v1' : 'v2')); } }), async () => ++callback !== 2);
  assert.equal(await state.pollIfDue(10_000), true);
  assert.equal(await state.pollIfDue(12_500), false);
  assert.equal(state.getState().status, 'ready');
  assert.equal(state.getState().error, null);
  assert.equal(state.getState().pollingStopped, false);
  assert.equal(state.getState().data?.change_token, 'v2');
  assert.equal(await state.pollIfDue(15_000), true);
  assert.deepEqual(since, [null, 'v1', 'v1']);
  assert.equal(state.getState().data?.change_token, 'v2');
});

test('unavailable eligibility aborts and fences a delayed status result; reconnect starts with a full read', async () => {
  const first = deferred<SyncStatusResponse | null>(); const since: Array<string | null> = []; let calls = 0;
  const state = store(api({ getStatus(value) { since.push(value); return ++calls === 1 ? first.promise : Promise.resolve(status('v2')); } }));
  const pending = state.pollIfDue(10_000);
  state.setEligible(false);
  first.resolve(status('old-account-view'));
  assert.equal(await pending, false); assert.equal(state.getState().data, null);
  state.setEligible(true); assert.equal(await state.pollIfDue(20_000), true);
  assert.deepEqual(since, [null, null]); assert.equal(state.getState().data?.change_token, 'v2');
});

test('only the current poll owner can clear refreshing after an account visibility restart', async () => {
  const first = deferred<SyncStatusResponse | null>(); const second = deferred<SyncStatusResponse | null>(); let calls = 0;
  const state = store(api({ getStatus() { return ++calls === 1 ? first.promise : second.promise; } }));
  const oldPoll = state.pollIfDue(10_000);
  assert.equal(state.getState().refreshing, true);
  state.setEligible(false); assert.equal(state.getState().refreshing, false);
  state.setEligible(true);
  assert.equal(await state.pollIfDue(20_000), false);
  first.resolve(status('old')); await oldPoll;
  const currentPoll = state.pollIfDue(20_000);
  assert.equal(state.getState().refreshing, true);
  second.resolve(status('new')); await currentPoll; await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(state.getState().refreshing, false);
});

test('hide and show do not overlap a pending dependent refresh; reconnect reads after it settles', async () => {
  const dependent = deferred<boolean>(); const since: Array<string | null> = []; let callbacks = 0;
  const state = store(api({ getStatus(value) { since.push(value); return Promise.resolve(status(`v${since.length}`)); } }), async () => {
    callbacks++;
    return callbacks === 1 ? dependent.promise : true;
  });
  const oldPoll = state.pollIfDue(10_000);
  await Promise.resolve();
  state.setEligible(false); state.setEligible(true);
  assert.equal(await state.pollIfDue(20_000), false);
  assert.equal(callbacks, 1);
  dependent.resolve(true); assert.equal(await oldPoll, false);
  assert.equal(state.getState().refreshing, true);
  assert.equal(await state.pollIfDue(20_000), false);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(callbacks, 2);
  assert.deepEqual(since, [null, null]);
});

test('manual refresh queued during a dependent refresh triggers an unconditional read after the current acknowledgement', async () => {
  const dependent = deferred<boolean>(); const secondStatus = deferred<SyncStatusResponse | null>();
  const since: Array<string | null> = []; let callbacks = 0;
  const state = store(api({ getStatus(value) {
    since.push(value);
    return since.length === 1 ? Promise.resolve(status('v1')) : secondStatus.promise;
  } }), async () => ++callbacks === 1 ? dependent.promise : true);
  const first = state.pollIfDue(10_000);
  await Promise.resolve();
  assert.equal(await state.retryRefresh(), false);
  dependent.resolve(true); assert.equal(await first, true);
  await Promise.resolve();
  assert.deepEqual(since, [null, null]);
  assert.equal(state.getState().refreshing, true);
  secondStatus.resolve(status('v2'));
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(state.getState().refreshing, false);
  assert.equal(state.getState().data?.change_token, 'v2');
});

test('manual refresh queued during an unchanged status request still forces a full read', async () => {
  const unchanged = deferred<SyncStatusResponse | null>(); const fullRead = deferred<SyncStatusResponse | null>();
  const since: Array<string | null> = [];
  const state = store(api({ getStatus(value) {
    since.push(value);
    return since.length === 1 ? Promise.resolve(status('v1')) : since.length === 2 ? unchanged.promise : fullRead.promise;
  } }));
  await state.pollIfDue(10_000);
  const conditional = state.pollIfDue(12_500);
  assert.equal(await state.retryRefresh(), false);
  unchanged.resolve(null); assert.equal(await conditional, true);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(since, [null, 'v1', null]);
  assert.equal(state.getState().refreshing, true);
  fullRead.resolve(status('v2'));
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(state.getState().refreshing, false);
  assert.equal(state.getState().data?.change_token, 'v2');
});

test('reconnect and manual retry force a full status read without bypassing the request cadence', async () => {
  const since: Array<string | null> = [];
  const state = store(api({ getStatus(value) { since.push(value); return Promise.resolve(status(`v${since.length}`)); } }));
  await state.pollIfDue(10_000);
  state.setEligible(false); state.setEligible(true);
  assert.equal(await state.pollIfDue(12_499), false);
  assert.equal(await state.pollIfDue(12_500), true);
  assert.deepEqual(since, [null, null]);
  assert.equal(await state.retryRefresh(), true);
  assert.deepEqual(since, [null, null, null]);
});

test('visible polling continues past thirty successes and stops after five consecutive failures until retry', async () => {
  let calls = 0;
  const state = store(api({ getStatus: async () => { calls++; return status(`v${calls}`); } }));
  let now = 10_000;
  for (let index = 0; index < 35; index++) { assert.equal(await state.pollIfDue(now), true); now += 2_500; }
  assert.equal(calls, 35); assert.equal(state.getState().pollingStopped, false);

  let failedCalls = 0;
  const failing = store(api({ getStatus: async () => {
    failedCalls++;
    if (failedCalls <= 5) throw new Error('temporary status error');
    return status(`retry-${failedCalls}`);
  } }));
  now = 100_000;
  for (let index = 0; index < 5; index++) { assert.equal(await failing.pollIfDue(now), false); now += 60_001; }
  assert.equal(failedCalls, 5); assert.equal(failing.getState().pollingStopped, true); assert.equal(failing.getState().status, 'error');
  assert.equal(await failing.pollIfDue(now), false);
  assert.equal(await failing.retryRefresh(), true); assert.equal(failing.getState().pollingStopped, false);
});

test('failure pages are token-bound, paginated and revalidated before a changed token is acknowledged', async () => {
  const requests: number[] = []; let currentToken = 'v1';
  const state = store(api({
    async getStatus() { return status(currentToken); },
    async listFailures(page) { requests.push(page); return failures(currentToken, page); },
  }));
  assert.equal(await state.pollIfDue(10_000), true);
  assert.equal(await state.loadFailures(2), true); assert.equal(state.getState().failures?.page, 2);
  currentToken = 'v2';
  assert.equal(await state.pollIfDue(12_500), true);
  assert.deepEqual(requests, [2, 2]); assert.equal(state.getState().failures?.change_token, 'v2');
  assert.equal(await state.loadFailures(3), true); assert.equal(state.getState().failures?.page, 3);
});

test('stale failure pages cannot replace newer account or status context', async () => {
  const old = deferred<SyncFailurePage>(); let token = 'v1';
  const state = store(api({ getStatus: async () => status(token), listFailures: (page) => page === 2 ? old.promise : Promise.resolve(failures(token, page)) }));
  await state.pollIfDue(10_000); await state.loadFailures();
  const pending = state.loadFailures(2);
  token = 'v2'; await state.pollIfDue(12_500);
  old.resolve(failures('v1', 2)); assert.equal(await pending, false);
  assert.equal(state.getState().data?.change_token, 'v2'); assert.equal(state.getState().failures?.change_token, 'v2');
});

test('failure page token races are benign and leave status polling able to retry the same change', async () => {
  let token = 'v1'; let calls = 0; const since: Array<string | null> = [];
  const state = store(api({
    async getStatus(value) { since.push(value); return status(token); },
    async listFailures(page) { calls++; return failures(calls === 2 ? 'v1' : token, page); },
  }));
  await state.pollIfDue(10_000); await state.loadFailures();
  token = 'v2';
  assert.equal(await state.pollIfDue(12_500), false);
  assert.equal(state.getState().data?.change_token, 'v2');
  assert.equal(state.getState().failures?.change_token, 'v1');
  assert.equal(state.getState().failureError, null);
  assert.equal(state.getState().pollingStopped, false);
  assert.equal(await state.pollIfDue(15_000), true);
  assert.deepEqual(since, [null, 'v1', 'v1']);
  assert.equal(state.getState().failures?.change_token, 'v2');
});

test('a failure request superseded by new status clears its own loading state', async () => {
  const old = deferred<SyncFailurePage>(); let token = 'v1'; let requests = 0;
  const state = store(api({
    async getStatus() { return status(token); },
    listFailures(page) { return ++requests === 2 ? old.promise : Promise.resolve(failures(token, page)); },
  }));
  await state.pollIfDue(10_000); await state.loadFailures();
  const pending = state.loadFailures(1);
  token = 'v2'; await state.pollIfDue(12_500);
  old.resolve(failures('v1'));
  assert.equal(await pending, false);
  assert.equal(state.getState().failuresLoading, false);
  assert.equal(state.getState().failures?.change_token, 'v2');
});

test('malformed failure paging metadata is rejected', async () => {
  const state = store(api({ listFailures: async () => ({ ...failures('v1'), total: -1 }) }));
  await state.pollIfDue(10_000);
  assert.equal(await state.loadFailures(), false);
  assert.match(state.getState().failureError ?? '', /page did not match/);
});

test('401 status or failure responses clear cached account-private data', async () => {
  const statusExpired = store(api({ getStatus: async () => { throw new ApiError('sign-in-required', 'Sign in again.'); } }));
  await statusExpired.pollIfDue(10_000);
  assert.equal(statusExpired.getState().data, null);
  assert.equal(statusExpired.getState().failures, null);
  assert.equal(statusExpired.getState().status, 'sign-in-required');

  let rejectFailures = false;
  const failureExpired = store(api({ listFailures: async () => {
    if (rejectFailures) throw new ApiError('sign-in-required', 'Sign in again.');
    return failures('v1');
  } }));
  await failureExpired.pollIfDue(10_000); await failureExpired.loadFailures();
  rejectFailures = true;
  await failureExpired.loadFailures();
  assert.equal(failureExpired.getState().data, null);
  assert.equal(failureExpired.getState().failures, null);
  assert.equal(failureExpired.getState().status, 'sign-in-required');
});

test('retry action uses the matching endpoint and refreshes status and a visible failure page', async () => {
  let imports = 0; let coverage = 0; let token = 'v1';
  const state = store(api({
    async getStatus() { return status(token); },
    async listFailures(page) { return failures(token, page, token === 'v1' ? [failure('11', 'retry-import'), failure('12')] : []); },
    async retryImport(id) { assert.equal(id, '11'); imports++; token = 'v2'; },
    async retryCoverage(id) { assert.equal(id, '12'); coverage++; token = 'v3'; },
  }));
  assert.equal(await state.pollIfDue(10_000), true); await state.loadFailures();
  assert.equal(await state.retryFailure(state.getState().failures!.items[0]!), true);
  assert.equal(imports, 1); assert.equal(state.getState().data?.change_token, 'v2'); assert.deepEqual(state.getState().failures?.items, []);
  assert.equal(await state.retryFailure(failure('12')), false);
  assert.equal(coverage, 1); assert.equal(state.getState().data?.change_token, 'v2');
  assert.equal(await state.pollIfDue(Date.now() + 2_500), true);
  assert.equal(state.getState().data?.change_token, 'v3');
});

test('retry errors remain visible without corrupting status', async () => {
  const state = store(api({ retryCoverage: async () => { throw new ApiError('http', 'Coverage retry failed.', 409); } }));
  await state.pollIfDue(10_000);
  assert.equal(await state.retryFailure(failure()), false);
  assert.equal(state.getState().retrying, null); assert.match(state.getState().error ?? '', /Coverage retry failed/);
  assert.equal(state.getState().data?.change_token, 'v1');
});

test('eligibility clears retry UI but holds the owner until the aborted mutation settles', async () => {
  const old = deferred<void>(); const current = deferred<void>(); let attempts = 0;
  const state = store(api({ retryCoverage: () => ++attempts === 1 ? old.promise : current.promise }));
  const oldRetry = state.retryFailure(failure('77'));
  state.setEligible(false);
  assert.equal(state.getState().retrying, null);
  state.setEligible(true);
  assert.equal(await state.retryFailure(failure('77')), false);
  assert.equal(attempts, 1);
  old.resolve(); await oldRetry;
  const newRetry = state.retryFailure(failure('77'));
  assert.equal(state.getState().retrying, '77');
  current.resolve();
  await newRetry;
  assert.equal(state.getState().retrying, null);
});
