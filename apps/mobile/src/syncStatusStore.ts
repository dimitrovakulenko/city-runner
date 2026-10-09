import { ApiError } from './api/client';
import type { ApiErrorKind } from './api/client';
import type { SyncFailure, SyncFailurePage, SyncStatusResponse } from './api/generated';
import type { SyncStatusApi } from './api/syncStatus';

export type SyncStatusLoad = 'idle' | 'loading' | 'ready' | 'offline' | 'error' | 'sign-in-required';

export interface SyncStatusState {
  data: SyncStatusResponse | null;
  status: SyncStatusLoad;
  error: string | null;
  failures: SyncFailurePage | null;
  failuresLoading: boolean;
  failureError: string | null;
  retrying: string | null;
  eligible: boolean;
  refreshing: boolean;
  pollingStopped: boolean;
  lastCheckedAt: number | null;
}

const INITIAL: SyncStatusState = { data: null, status: 'idle', error: null, failures: null, failuresLoading: false,
  failureError: null, retrying: null, eligible: false, refreshing: false, pollingStopped: false, lastCheckedAt: null };
const CADENCE_MS = 2_500;
const FAILURE_LIMIT = 5;
const MAX_BACKOFF_MS = 60_000;

function kind(error: unknown): ApiErrorKind | 'http' { return error instanceof ApiError ? error.kind : 'http'; }
function message(error: unknown): string { return error instanceof Error ? error.message : 'Could not refresh source status. Retry when connected.'; }

export class SyncStatusStore {
  private state: SyncStatusState = INITIAL;
  private accountGeneration = 0;
  private operation = 0;
  private failureRequest = 0;
  private controller: AbortController | null = null;
  private failureController: AbortController | null = null;
  private owner: number | null = null;
  private lastRequestAt = Number.NEGATIVE_INFINITY;
  private nextAllowedAt = 0;
  private failures = 0;
  private acknowledgedToken: string | null = null;
  private forceFullRead = true;
  private fullReadRevision = 0;
  private listeners = new Set<(state: SyncStatusState) => void>();

  constructor(private readonly api: SyncStatusApi,
    private readonly onChange: (data: SyncStatusResponse, accountGeneration: number) => Promise<boolean>) {}

  getState(): SyncStatusState { return this.state; }
  getAccountGeneration(): number { return this.accountGeneration; }
  subscribe(listener: (state: SyncStatusState) => void): () => void { this.listeners.add(listener); return () => this.listeners.delete(listener); }
  private update(patch: Partial<SyncStatusState>): void { this.state = { ...this.state, ...patch }; for (const listener of this.listeners) listener(this.state); }

  reset(): void {
    this.accountGeneration++;
    this.operation++;
    this.failureRequest++;
    this.controller?.abort(); this.controller = null;
    this.failureController?.abort(); this.failureController = null;
    this.owner = null;
    this.lastRequestAt = Number.NEGATIVE_INFINITY;
    this.nextAllowedAt = 0;
    this.failures = 0;
    this.acknowledgedToken = null;
    this.forceFullRead = true;
    this.fullReadRevision++;
    this.update({ ...INITIAL });
  }

  setEligible(eligible: boolean): void {
    if (eligible === this.state.eligible) return;
    this.operation++;
    this.controller?.abort();
    this.failureRequest++;
    this.failureController?.abort(); this.failureController = null;
    this.nextAllowedAt = 0;
    this.failures = 0;
    this.forceFullRead = true;
    if (eligible) this.fullReadRevision++;
    this.update({ eligible, status: eligible ? this.state.data ? 'ready' : 'loading' : 'offline',
      error: null, failuresLoading: false, failureError: null, retrying: eligible ? this.state.retrying : null,
      refreshing: eligible ? this.state.refreshing : false, pollingStopped: false });
  }

  async pollIfDue(now = Date.now()): Promise<boolean> {
    if (!this.state.eligible || this.owner !== null || this.state.pollingStopped || now < this.nextAllowedAt || now - this.lastRequestAt < CADENCE_MS) return false;
    const account = this.accountGeneration;
    const owner = ++this.operation;
    const controller = new AbortController();
    this.controller = controller;
    this.owner = owner;
    this.lastRequestAt = now;
    const current = () => account === this.accountGeneration && this.owner === owner && this.operation === owner && this.state.eligible && !controller.signal.aborted;
    const fullReadRevision = this.fullReadRevision;
    const fullReadAtStart = this.forceFullRead;
    const since = fullReadAtStart ? null : this.acknowledgedToken;
    this.update({ status: this.state.data ? 'ready' : 'loading', error: null, refreshing: true });
    try {
      const response = await this.api.getStatus(since, controller.signal);
      if (!current()) return false;
      if (response === null) {
        if (since === null) throw new Error('The server returned unchanged status without a baseline token.');
        this.finishSuccess(now);
        return true;
      }
      if (!response.change_token || response.change_token.length > 200) throw new Error('The server returned an invalid source change token.');
      const changed = fullReadAtStart || response.change_token !== this.acknowledgedToken;
      this.update({ data: response, status: 'ready', error: null, lastCheckedAt: now });
      let refreshedFailures: SyncFailurePage | null = null;
      if (changed) {
        const refreshed = await this.onChange(response, account);
        if (!current()) return false;
        if (!refreshed) {
          this.nextAllowedAt = Math.max(this.lastRequestAt + CADENCE_MS, now + CADENCE_MS);
          this.update({ status: this.state.data ? 'ready' : 'loading', error: null, pollingStopped: false });
          return false;
        }
        if (this.state.failures) {
          const previous = this.state.failures;
          this.update({ failuresLoading: false });
          const failureGeneration = ++this.failureRequest;
          const failureController = new AbortController();
          this.failureController?.abort(); this.failureController = failureController;
          refreshedFailures = await this.api.listFailures(previous.page, failureController.signal);
          if (!current() || failureGeneration !== this.failureRequest || failureController.signal.aborted) return false;
          if (!matchesPage(refreshedFailures, previous.page) || refreshedFailures.change_token !== response.change_token) {
            this.nextAllowedAt = Math.max(this.lastRequestAt + CADENCE_MS, now + CADENCE_MS);
            this.update({ failuresLoading: false, failureError: null });
            return false;
          }
          if (previous.page_size !== refreshedFailures.page_size && previous.page_size > 0) {
            throw new Error('The failure list page size changed during refresh.');
          }
        }
      }
      if (!current()) return false;
      this.acknowledgedToken = response.change_token;
      if (fullReadRevision === this.fullReadRevision) this.forceFullRead = false;
      this.update({ data: response, status: 'ready', error: null, lastCheckedAt: now,
        ...(refreshedFailures ? { failures: refreshedFailures, failureError: null } : {}) });
      this.finishSuccess(now);
      return true;
    } catch (error) {
      if (!current()) return false;
      this.finishFailure(error, now);
      return false;
    } finally {
      if (this.owner === owner) {
        this.owner = null; this.update({ refreshing: false });
        if (account === this.accountGeneration && this.state.eligible && !this.state.pollingStopped && fullReadRevision !== this.fullReadRevision) void this.pollIfDue();
      }
      if (this.controller === controller) this.controller = null;
    }
  }

  async retryRefresh(): Promise<boolean> {
    if (!this.state.eligible) return false;
    this.failures = 0; this.nextAllowedAt = 0;
    this.forceFullRead = true;
    this.fullReadRevision++;
    this.update({ pollingStopped: false, error: null });
    if (this.owner !== null) return false;
    return this.pollIfDue();
  }

  async loadFailures(page = 1): Promise<boolean> {
    if (!this.state.eligible || !this.state.data || !Number.isInteger(page) || page < 1) return false;
    const account = this.accountGeneration;
    const token = this.state.data.change_token;
    const request = ++this.failureRequest;
    const controller = new AbortController();
    this.failureController?.abort(); this.failureController = controller;
    this.update({ failuresLoading: true, failureError: null });
    try {
      const result = await this.api.listFailures(page, controller.signal);
      if (account !== this.accountGeneration || request !== this.failureRequest || !this.state.eligible || controller.signal.aborted) return false;
      if (token !== this.state.data?.change_token) { this.update({ failuresLoading: false, failureError: null }); return false; }
      if (!matchesPage(result, page)) throw new Error('The failure list page did not match the request.');
      if (result.change_token !== token) { this.update({ failuresLoading: false, failureError: null }); return false; }
      this.update({ failures: result, failuresLoading: false, failureError: null });
      return true;
    } catch (error) {
      if (account !== this.accountGeneration || request !== this.failureRequest || !this.state.eligible || controller.signal.aborted) return false;
      if (kind(error) === 'sign-in-required') {
        this.clearPrivateForUnauthorized(message(error));
      } else {
        this.update({ failuresLoading: false, failureError: message(error) });
      }
      return false;
    } finally {
      if (this.failureController === controller) {
        this.failureController = null;
        if (request === this.failureRequest && this.state.failuresLoading) this.update({ failuresLoading: false });
      }
    }
  }

  async retryFailure(item: SyncFailure): Promise<boolean> {
    if (!this.state.eligible || this.state.retrying !== null || this.owner !== null) return false;
    const account = this.accountGeneration;
    const owner = ++this.operation;
    const controller = new AbortController();
    this.controller?.abort(); this.controller = controller; this.owner = owner;
    this.failures = 0; this.nextAllowedAt = 0; this.forceFullRead = true; this.fullReadRevision++;
    this.update({ pollingStopped: false });
    this.failureRequest++;
    this.failureController?.abort(); this.failureController = null;
    this.update({ retrying: item.source_id, error: null });
    try {
      if (item.retry_action === 'retry-import') await this.api.retryImport(item.source_id, controller.signal);
      else await this.api.retryCoverage(item.source_id, controller.signal);
      if (account !== this.accountGeneration || this.owner !== owner || controller.signal.aborted || !this.state.eligible) return false;
      this.nextAllowedAt = 0;
      this.update({ retrying: null });
      this.owner = null; this.controller = null;
      return await this.pollIfDue();
    } catch (error) {
      if (account !== this.accountGeneration || this.owner !== owner || controller.signal.aborted || !this.state.eligible) return false;
      if (kind(error) === 'sign-in-required') this.clearPrivateForUnauthorized(message(error));
      else this.update({ error: message(error) });
      return false;
    } finally {
      if (this.owner === owner) this.owner = null;
      if (this.controller === controller) this.controller = null;
      if (account === this.accountGeneration && this.operation === owner && this.state.retrying === item.source_id) this.update({ retrying: null });
    }
  }

  private finishSuccess(now: number): void {
    this.failures = 0;
    this.nextAllowedAt = Math.max(this.lastRequestAt + CADENCE_MS, now + CADENCE_MS);
    this.update({ status: 'ready', error: null, pollingStopped: false, lastCheckedAt: now });
  }

  private finishFailure(error: unknown, now: number): void {
    if (kind(error) === 'sign-in-required') {
      this.clearPrivateForUnauthorized(message(error));
      this.nextAllowedAt = now + CADENCE_MS;
      return;
    }
    this.failures++;
    const pollingStopped = this.failures >= FAILURE_LIMIT;
    const delay = Math.min(MAX_BACKOFF_MS, CADENCE_MS * 2 ** (this.failures - 1));
    this.nextAllowedAt = now + delay;
    const failureMessage = message(error);
    this.update({ status: kind(error) === 'sign-in-required' ? 'sign-in-required' : 'error', error: failureMessage, pollingStopped,
      ...(this.state.failuresLoading ? { failuresLoading: false, failureError: failureMessage } : {}) });
  }

  private clearPrivateForUnauthorized(error: string): void {
    this.operation++;
    this.owner = null;
    this.controller?.abort(); this.controller = null;
    this.failureRequest++;
    this.failureController?.abort(); this.failureController = null;
    this.acknowledgedToken = null;
    this.forceFullRead = true;
    this.update({ data: null, failures: null, failuresLoading: false, failureError: null,
      status: 'sign-in-required', error, retrying: null, refreshing: false, lastCheckedAt: null });
  }
}

function matchesPage(page: SyncFailurePage, requested: number): boolean {
  return Number.isInteger(page.page) && page.page === requested && Number.isInteger(page.page_size) && page.page_size > 0 &&
    page.page_size <= 20 && Number.isInteger(page.total) && page.total >= 0 && page.items.length <= page.page_size;
}
