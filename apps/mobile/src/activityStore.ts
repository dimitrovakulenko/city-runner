import { ApiError } from './api/client';
import type { ActivityApi, ApiErrorKind } from './api/client';
import type { ActivityDetail, ActivitySummary } from './api/generated';

export type ListStatus = 'loading' | 'loading-more' | 'ready' | 'empty' | 'offline' | 'sign-in-required' | 'error';
export type DetailStatus = 'idle' | 'loading' | 'ready' | 'offline' | 'sign-in-required' | 'error';

export interface ActivityExplorerState {
  items: ActivitySummary[];
  page: number;
  pageSize: number;
  total: number;
  query: string;
  listStatus: ListStatus;
  listError: string | null;
  selectedId: string | null;
  detail: ActivityDetail | null;
  detailStatus: DetailStatus;
  detailError: string | null;
}

const INITIAL_STATE: ActivityExplorerState = {
  items: [], page: 1, pageSize: 20, total: 0, query: '',
  listStatus: 'loading', listError: null, selectedId: null,
  detail: null, detailStatus: 'idle', detailError: null,
};

function errorStatus(error: unknown): ApiErrorKind | 'http' {
  return error instanceof ApiError ? error.kind : 'http';
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Something went wrong. Please retry.';
}

function listFailureStatus(error: unknown): 'offline' | 'sign-in-required' | 'error' {
  const kind = errorStatus(error);
  return kind === 'offline' || kind === 'sign-in-required' ? kind : 'error';
}

export class ActivityStore {
  private readonly api: ActivityApi;
  private state = INITIAL_STATE;
  private listRequest = 0;
  private detailRequest = 0;
  private listeners = new Set<(state: ActivityExplorerState) => void>();

  constructor(api: ActivityApi) { this.api = api; }

  getState(): ActivityExplorerState {
    return this.state;
  }

  subscribe(listener: (state: ActivityExplorerState) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private update(patch: Partial<ActivityExplorerState>): void {
    this.state = { ...this.state, ...patch };
    for (const listener of this.listeners) listener(this.state);
  }

  private expireSession(): void {
    this.listRequest += 1;
    this.detailRequest += 1;
    this.update({
      items: [], page: 1, total: 0, listStatus: 'sign-in-required',
      listError: 'Your session expired. Sign in again.', selectedId: null,
      detail: null, detailStatus: 'idle', detailError: null,
    });
  }

  reset(): void {
    this.listRequest += 1;
    this.detailRequest += 1;
    this.update({ ...INITIAL_STATE });
  }

  async loadPage(query = this.state.query, page = 1): Promise<void> {
    const requestId = ++this.listRequest;
    const normalizedQuery = query.trim();
    this.update({
      query: normalizedQuery,
      page: page === 1 ? 1 : this.state.page,
      listStatus: page === 1 ? 'loading' : 'loading-more',
      listError: null,
      ...(page === 1 ? { items: [] } : {}),
    });
    try {
      const result = await this.api.listActivities({ page, pageSize: this.state.pageSize, query: normalizedQuery });
      if (requestId !== this.listRequest) return;
      const items = page === 1 ? result.items : [...this.state.items, ...result.items];
      this.update({
        items,
        page: result.page,
        pageSize: result.page_size,
        total: result.total,
        listStatus: items.length ? 'ready' : 'empty',
      });
    } catch (error) {
      if (requestId !== this.listRequest) return;
      if (errorStatus(error) === 'sign-in-required') return this.expireSession();
      this.update({ listStatus: listFailureStatus(error), listError: errorMessage(error) });
    }
  }

  async retryList(): Promise<void> {
    return this.loadPage(this.state.query, 1);
  }

  async loadMore(): Promise<void> {
    if (this.state.listStatus !== 'ready' || this.state.items.length >= this.state.total) return;
    return this.loadPage(this.state.query, this.state.page + 1);
  }

  async selectActivity(id: string): Promise<void> {
    const requestId = ++this.detailRequest;
    this.update({ selectedId: id, detail: null, detailStatus: 'loading', detailError: null });
    try {
      const detail = await this.api.getActivity(id);
      if (requestId !== this.detailRequest || this.state.selectedId !== id) return;
      this.update({ detail, detailStatus: 'ready' });
    } catch (error) {
      if (requestId !== this.detailRequest || this.state.selectedId !== id) return;
      if (errorStatus(error) === 'sign-in-required') return this.expireSession();
      this.update({ detailStatus: listFailureStatus(error), detailError: errorMessage(error) });
    }
  }

  async retryDetail(): Promise<void> {
    const id = this.state.selectedId;
    if (id !== null) return this.selectActivity(id);
  }

  clearSelection(): void {
    this.detailRequest += 1;
    this.update({ selectedId: null, detail: null, detailStatus: 'idle', detailError: null });
  }
}
