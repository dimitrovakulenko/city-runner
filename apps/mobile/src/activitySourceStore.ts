import { ApiError } from './api/client';
import type { ApiErrorKind } from './api/client';
import type { ActivitySourceApi } from './api/activitySources';
import type { ActivitySourceResponse } from './api/generated';

export type ActivitySourceStatus = 'idle' | 'loading' | 'loading-more' | 'ready' | 'empty' | 'offline' | 'sign-in-required' | 'error';
export interface ActivitySourceState {
  activityId: string | null;
  items: ActivitySourceResponse[];
  page: number;
  pageSize: number;
  total: number;
  status: ActivitySourceStatus;
  error: string | null;
  activityMissing: boolean;
  removingSourceId: string | null;
}

const PAGE_SIZE = 20;
const INITIAL: ActivitySourceState = {
  activityId: null, items: [], page: 0, pageSize: PAGE_SIZE, total: 0,
  status: 'idle', error: null, activityMissing: false, removingSourceId: null,
};

function errorKind(error: unknown): ApiErrorKind | 'http' { return error instanceof ApiError ? error.kind : 'http'; }
function errorMessage(error: unknown): string { return error instanceof Error ? error.message : 'Could not load imported files. Retry when connected.'; }
function errorStatus(error: unknown): 'offline' | 'sign-in-required' | 'error' {
  const kind = errorKind(error);
  return kind === 'offline' || kind === 'sign-in-required' ? kind : 'error';
}

export class ActivitySourceStore {
  private state = INITIAL;
  private accountGeneration = 0;
  private selectionGeneration = 0;
  private listRequest = 0;
  private listController: AbortController | null = null;
  private removalController: AbortController | null = null;
  private listeners = new Set<(state: ActivitySourceState) => void>();

  constructor(
    private readonly api: ActivitySourceApi,
    private readonly onAcceptedRemoval: (activityId: string) => void | Promise<void> = () => {},
  ) {}

  getState(): ActivitySourceState { return this.state; }
  getAccountGeneration(): number { return this.accountGeneration; }
  subscribe(listener: (state: ActivitySourceState) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
  private update(patch: Partial<ActivitySourceState>): void {
    this.state = { ...this.state, ...patch };
    for (const listener of this.listeners) listener(this.state);
  }
  private current(request: number, account: number, selection: number, activityId: string): boolean {
    return request === this.listRequest && account === this.accountGeneration && selection === this.selectionGeneration && activityId === this.state.activityId;
  }
  private expireSession(): void {
    this.reset();
    this.update({ status: 'sign-in-required', error: 'Your session expired. Sign in again.' });
  }
  reset(): void {
    this.accountGeneration++;
    this.selectionGeneration++;
    this.listRequest++;
    this.listController?.abort();
    this.removalController?.abort();
    this.listController = null;
    this.removalController = null;
    this.update({ ...INITIAL });
  }

  clearSelection(): void {
    this.selectionGeneration++;
    this.listRequest++;
    this.listController?.abort();
    this.listController = null;
    // Accepted same-account source removals remain fenced only by accountGeneration.
    this.update({ ...INITIAL, removingSourceId: this.state.removingSourceId });
  }

  async load(activityId: string, page = 1): Promise<boolean> {
    if (!activityId || !Number.isInteger(page) || page < 1) return false;
    if (activityId !== this.state.activityId) {
      this.selectionGeneration++;
      this.listRequest++;
      this.listController?.abort();
      this.update({ ...INITIAL, activityId, removingSourceId: this.state.removingSourceId });
    }
    return this.loadPage(page, page > 1);
  }

  async refresh(): Promise<boolean> {
    const activityId = this.state.activityId;
    if (activityId === null) return true;
    return this.loadPage(1, false);
  }

  async loadMore(): Promise<boolean> {
    if (this.state.status !== 'ready' || this.state.items.length >= this.state.total || this.state.activityId === null) return false;
    return this.loadPage(this.state.page + 1, true);
  }

  private async loadPage(page: number, append: boolean): Promise<boolean> {
    const activityId = this.state.activityId;
    if (activityId === null) return false;
    const request = ++this.listRequest;
    const account = this.accountGeneration;
    const selection = this.selectionGeneration;
    this.listController?.abort();
    const controller = new AbortController();
    this.listController = controller;
    this.update({ status: append ? 'loading-more' : 'loading', error: null, activityMissing: false });
    try {
      const result = await this.api.listActivitySources(activityId, { page, pageSize: PAGE_SIZE }, controller.signal);
      if (!this.current(request, account, selection, activityId)) return false;
      if (result.activity_id !== activityId || result.page !== page || result.page_size !== PAGE_SIZE ||
          !Number.isInteger(result.total) || result.total < 0 || result.items.some((item) => !item.id || !['gpx', 'fit'].includes(item.source_kind))) {
        throw new Error('The imported-file response was invalid. Retry when connected.');
      }
      if (append && result.total !== this.state.total) return this.loadPage(1, false);
      const items = append ? [...this.state.items, ...result.items] : result.items;
      this.update({ items, page, pageSize: PAGE_SIZE, total: result.total, status: items.length ? 'ready' : 'empty', error: null, activityMissing: false });
      return true;
    } catch (error) {
      if (!this.current(request, account, selection, activityId)) return false;
      if (errorKind(error) === 'sign-in-required') { this.expireSession(); return false; }
      if (error instanceof ApiError && error.status === 404) {
        this.update({ items: [], page: 0, total: 0, status: 'error', error: 'This activity no longer exists.', activityMissing: true });
        return false;
      }
      this.update({ status: errorStatus(error), error: errorMessage(error) });
      return false;
    } finally {
      if (this.listController === controller) this.listController = null;
    }
  }

  async remove(sourceId: string): Promise<boolean> {
    const activityId = this.state.activityId;
    if (!activityId || this.removalController || !this.state.items.some((item) => item.id === sourceId)) return false;
    const account = this.accountGeneration;
    const selection = this.selectionGeneration;
    const controller = new AbortController();
    this.removalController = controller;
    this.update({ removingSourceId: sourceId, error: null });
    try {
      await this.api.deleteActivitySource(activityId, sourceId, controller.signal);
      if (account !== this.accountGeneration || controller.signal.aborted) return false;
      await this.refresh();
      if (account === this.accountGeneration) await this.onAcceptedRemoval(activityId);
      return account === this.accountGeneration && selection === this.selectionGeneration && activityId === this.state.activityId;
    } catch (error) {
      if (account !== this.accountGeneration || controller.signal.aborted || selection !== this.selectionGeneration || activityId !== this.state.activityId) return false;
      if (errorKind(error) === 'sign-in-required') { this.expireSession(); return false; }
      this.update({ error: errorMessage(error) });
      return false;
    } finally {
      if (this.removalController === controller) {
        this.removalController = null;
        if (this.state.removingSourceId === sourceId) this.update({ removingSourceId: null });
      }
    }
  }
}
