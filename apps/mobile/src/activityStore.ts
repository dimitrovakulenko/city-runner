import { ApiError } from './api/client';
import { canonicalizeActivityFilters } from './api/client';
import type { ActivityApi, ApiErrorKind } from './api/client';
import type { ActivityDetail, ActivityFilterOptions, ActivityFilters, ActivityImpactPage, ActivitySummary } from './api/generated';

export type ListStatus = 'loading' | 'loading-more' | 'ready' | 'empty' | 'offline' | 'sign-in-required' | 'error';
export type DetailStatus = 'idle' | 'loading' | 'ready' | 'offline' | 'sign-in-required' | 'error';
export type ImpactStatus = 'idle' | ListStatus;

export interface ActivityExplorerState {
  items: ActivitySummary[];
  page: number;
  pageSize: number;
  total: number;
  query: string;
  filters: Required<ActivityFilters>;
  filterOptions: ActivityFilterOptions | null;
  filterOptionsStatus: 'idle' | 'loading' | 'ready' | 'offline' | 'sign-in-required' | 'error';
  filterOptionsError: string | null;
  listStatus: ListStatus;
  listError: string | null;
  selectedId: string | null;
  detail: ActivityDetail | null;
  detailStatus: DetailStatus;
  detailError: string | null;
  impactDatasetId: string | null;
  impactRule: 'normal' | 'strict';
  impact: ActivityImpactPage | null;
  impactPage: number;
  impactStatus: ImpactStatus;
  impactError: string | null;
}

const INITIAL_STATE: ActivityExplorerState = {
  items: [], page: 1, pageSize: 20, total: 0, query: '', filters: canonicalizeActivityFilters(),
  filterOptions: null, filterOptionsStatus: 'idle', filterOptionsError: null,
  listStatus: 'loading', listError: null, selectedId: null,
  detail: null, detailStatus: 'idle', detailError: null,
  impactDatasetId: null, impactRule: 'normal', impact: null, impactPage: 1, impactStatus: 'idle', impactError: null,
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
  private impactRequest = 0;
  private filterOptionsRequest = 0;
  private accountGeneration = 0;
  private accountController = new AbortController();
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
    this.accountGeneration += 1;
    this.listRequest += 1;
    this.detailRequest += 1;
    this.impactRequest += 1;
    this.filterOptionsRequest += 1;
    this.accountController.abort();
    this.accountController = new AbortController();
    this.update({
      items: [], page: 1, total: 0, listStatus: 'sign-in-required',
      filters: canonicalizeActivityFilters(),
      filterOptions: null, filterOptionsStatus: 'sign-in-required', filterOptionsError: 'Your session expired. Sign in again.',
      listError: 'Your session expired. Sign in again.', selectedId: null,
      detail: null, detailStatus: 'idle', detailError: null,
      impactDatasetId: null, impactRule: 'normal', impact: null, impactPage: 1, impactStatus: 'idle', impactError: null,
    });
  }

  reset(): void {
    this.accountGeneration += 1;
    this.listRequest += 1;
    this.detailRequest += 1;
    this.impactRequest += 1;
    this.filterOptionsRequest += 1;
    this.accountController.abort();
    this.accountController = new AbortController();
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
      const result = await this.api.listActivities({ page, pageSize: this.state.pageSize, query: normalizedQuery, filters: this.state.filters }, this.accountController.signal);
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

  setFilters(filters: ActivityFilters): void {
    const canonical = canonicalizeActivityFilters(filters);
    if (JSON.stringify(canonical) === JSON.stringify(this.state.filters)) return;
    this.detailRequest += 1;
    this.impactRequest += 1;
    this.update({ filters: canonical, selectedId: null, detail: null, detailStatus: 'idle', detailError: null,
      impact: null, impactPage: 1, impactStatus: 'idle', impactError: null });
    void this.loadPage(this.state.query, 1);
  }

  async loadFilterOptions(): Promise<void> {
    const request = ++this.filterOptionsRequest;
    const account = this.accountGeneration;
    this.update({ filterOptionsStatus: 'loading', filterOptionsError: null });
    try {
      const result = await this.api.getActivityFilters(this.accountController.signal);
      if (request !== this.filterOptionsRequest || account !== this.accountGeneration) return;
      this.update({ filterOptions: result, filterOptionsStatus: 'ready' });
    } catch (error) {
      if (account !== this.accountGeneration || request !== this.filterOptionsRequest) return;
      if (errorStatus(error) === 'sign-in-required') return this.expireSession();
      this.update({ filterOptionsStatus: listFailureStatus(error), filterOptionsError: errorMessage(error) });
    }
  }

  async retryFilterOptions(): Promise<void> { return this.loadFilterOptions(); }

  async loadMore(): Promise<void> {
    if (this.state.listStatus !== 'ready' || this.state.items.length >= this.state.total) return;
    return this.loadPage(this.state.query, this.state.page + 1);
  }

  async selectActivity(id: string): Promise<void> {
    const requestId = ++this.detailRequest;
    const account = this.accountGeneration;
    this.impactRequest += 1;
    this.update({ selectedId: id, detail: null, detailStatus: 'loading', detailError: null,
      impact: null, impactPage: 1, impactStatus: this.state.impactDatasetId ? 'loading' : 'idle', impactError: null });
    try {
      const detail = await this.api.getActivity(id, this.accountController.signal);
      if (requestId !== this.detailRequest || this.state.selectedId !== id) return;
      this.update({ detail, detailStatus: 'ready' });
      if (this.state.impactDatasetId !== null) void this.loadActivityImpact(1, false);
    } catch (error) {
      if (account !== this.accountGeneration) return;
      if (errorStatus(error) === 'sign-in-required') return this.expireSession();
      if (requestId !== this.detailRequest || this.state.selectedId !== id) return;
      this.update({ detailStatus: listFailureStatus(error), detailError: errorMessage(error), impactStatus: 'idle', impactError: null });
    }
  }

  async retryDetail(): Promise<void> {
    const id = this.state.selectedId;
    if (id !== null) return this.selectActivity(id);
  }

  selectImpactDataset(datasetId: string | null, rule: 'normal' | 'strict' = this.state.impactRule): void {
    if (datasetId === '') datasetId = null;
    if (datasetId === this.state.impactDatasetId && rule === this.state.impactRule) return;
    this.impactRequest += 1;
    this.update({ impactDatasetId: datasetId, impactRule: rule, impact: null, impactPage: 1,
      impactStatus: datasetId && this.state.detailStatus === 'ready' && this.state.selectedId ? 'loading' : 'idle', impactError: null });
    if (datasetId && this.state.detailStatus === 'ready' && this.state.selectedId) void this.loadActivityImpact(1, false);
  }

  async loadMoreImpact(): Promise<void> {
    const impact = this.state.impact;
    if (!impact || this.state.impactStatus !== 'ready' || impact.total === null || impact.streets.length >= impact.total) return;
    await this.loadActivityImpact(this.state.impactPage + 1, true);
  }

  async retryImpact(): Promise<void> {
    if (this.state.impactDatasetId && this.state.detailStatus === 'ready' && this.state.selectedId) await this.loadActivityImpact(1, false);
  }

  async refreshImpact(): Promise<void> {
    this.impactRequest += 1;
    this.update({ impact: null, impactPage: 1, impactStatus: 'idle', impactError: null });
    if (this.state.impactDatasetId && this.state.detailStatus === 'ready' && this.state.selectedId) await this.loadActivityImpact(1, false);
  }

  private async loadActivityImpact(page: number, append: boolean): Promise<void> {
    const activityId = this.state.selectedId;
    const datasetId = this.state.impactDatasetId;
    if (!activityId || !datasetId || this.state.detailStatus !== 'ready') return;
    const requestId = ++this.impactRequest;
    const account = this.accountGeneration;
    const rule = this.state.impactRule;
    this.update({ impactStatus: append ? 'loading-more' : 'loading', impactError: null,
      ...(append ? {} : { impact: null, impactPage: 1 }) });
    try {
      const result = await this.api.getActivityImpact(activityId, { datasetId, rule, page, pageSize: this.state.impact?.page_size ?? 50 }, this.accountController.signal);
      if (requestId !== this.impactRequest || account !== this.accountGeneration || activityId !== this.state.selectedId ||
        datasetId !== this.state.impactDatasetId || rule !== this.state.impactRule) return;
      if (result.activity_id !== activityId || result.dataset_id !== datasetId || result.rule !== rule || result.page !== page) {
        this.update({ impact: null, impactPage: 1, impactStatus: 'error', impactError: 'The activity impact response did not match the selected activity and dataset.' }); return;
      }
      if (result.dataset_state !== 'active') {
        this.update({ impact: null, impactPage: 1, impactStatus: 'error', impactError: 'The selected dataset is no longer active. Choose another dataset and retry.' }); return;
      }
      const current = this.state.impact;
      if (append && current && (current.coverage.progress_revision !== result.coverage.progress_revision || current.coverage.status !== result.coverage.status)) {
        await this.loadActivityImpact(1, false);
        return;
      }
      const streets = append && current ? [...current.streets, ...result.streets] : result.streets;
      this.update({ impact: { ...result, streets }, impactPage: result.page,
        impactStatus: streets.length === 0 && result.total === 0 ? 'empty' : 'ready', impactError: null });
    } catch (error) {
      if (account !== this.accountGeneration) return;
      if (errorStatus(error) === 'sign-in-required') return this.expireSession();
      if (requestId !== this.impactRequest || activityId !== this.state.selectedId || datasetId !== this.state.impactDatasetId || rule !== this.state.impactRule) return;
      this.update({ impactStatus: listFailureStatus(error), impactError: errorMessage(error) });
    }
  }

  clearSelection(): void {
    this.detailRequest += 1;
    this.impactRequest += 1;
    this.update({ selectedId: null, detail: null, detailStatus: 'idle', detailError: null,
      impact: null, impactPage: 1, impactStatus: 'idle', impactError: null });
  }

  activityDeleted(id: string): void {
    if (this.state.selectedId === id) this.clearSelection();
    void this.loadPage(this.state.query, 1);
  }
}
