import { ApiError } from './api/client';
import type { ApiErrorKind } from './api/client';
import type { CityExplorerApi, StreetFilter, StreetSort } from './api/cities';
import type { CityItem, ContributionPage, ContributingActivity, DatasetCoverage, ProgressDataset, ProgressResponse, RemainingNode, StreetDetail, StreetItem } from './api/generated';

export type CityLoadState = 'idle' | 'loading' | 'loading-more' | 'ready' | 'empty' | 'offline' | 'sign-in-required' | 'error';
const PAGE_SIZE = 50;

export interface CityExplorerState {
  rule: 'normal' | 'strict';
  progress: ProgressResponse | null;
  progressStatus: CityLoadState;
  progressError: string | null;
  datasets: ProgressDataset[];
  selectedDatasetId: string | null;
  cityQuery: string;
  cities: CityItem[];
  cityPage: number;
  cityTotal: number;
  cityCoverage: DatasetCoverage | null;
  cityStatus: CityLoadState;
  cityError: string | null;
  selectedCityId: string | null;
  selectedCityName: string | null;
  streetQuery: string;
  streetFilter: StreetFilter;
  streetSort: StreetSort;
  streets: StreetItem[];
  streetPage: number;
  streetTotal: number;
  streetCoverage: DatasetCoverage | null;
  streetFilterApplied: boolean;
  streetSortApplied: boolean;
  streetStatus: CityLoadState;
  streetError: string | null;
  selectedStreetId: string | null;
  detail: StreetDetail | null;
  remainingNodes: RemainingNode[] | null;
  detailPage: number;
  detailStatus: CityLoadState;
  detailError: string | null;
  contributions: ContributingActivity[];
  contributionPage: number;
  contributionTotal: number | null;
  contributionAvailable: boolean;
  contributionCoverage: DatasetCoverage | null;
  contributionStatus: CityLoadState;
  contributionError: string | null;
}

const INITIAL: CityExplorerState = {
  rule: 'normal', progress: null, progressStatus: 'idle', progressError: null, datasets: [], selectedDatasetId: null,
  cityQuery: '', cities: [], cityPage: 0, cityTotal: 0, cityCoverage: null, cityStatus: 'idle', cityError: null, selectedCityId: null, selectedCityName: null,
  streetQuery: '', streetFilter: 'all', streetSort: 'name', streets: [], streetPage: 0, streetTotal: 0, streetCoverage: null, streetFilterApplied: true, streetSortApplied: true, streetStatus: 'idle', streetError: null,
  selectedStreetId: null, detail: null, remainingNodes: null, detailPage: 0, detailStatus: 'idle', detailError: null,
  contributions: [], contributionPage: 0, contributionTotal: null, contributionAvailable: false, contributionCoverage: null,
  contributionStatus: 'idle', contributionError: null,
};

function errorKind(error: unknown): ApiErrorKind | 'http' { return error instanceof ApiError ? error.kind : 'http'; }
function errorMessage(error: unknown): string { return error instanceof Error ? error.message : 'Request failed. Retry when connected.'; }
function errorState(error: unknown): 'offline' | 'sign-in-required' | 'error' {
  const kind = errorKind(error);
  return kind === 'offline' || kind === 'sign-in-required' ? kind : 'error';
}
const MAX_BIGINT_ID = 9223372036854775807n;
function validBigintId(id: string): boolean { return /^[1-9]\d{0,18}$/.test(id) && BigInt(id) <= MAX_BIGINT_ID; }

export class CityExplorerStore {
  private state = INITIAL;
  private accountGeneration = 0;
  private accountController = new AbortController();
  private requests = { progress: 0, cities: 0, streets: 0, detail: 0, contributions: 0 };
  private mapStreetContext = false;
  private listeners = new Set<(state: CityExplorerState) => void>();

  constructor(private readonly api: CityExplorerApi) {}
  getState(): CityExplorerState { return this.state; }
  subscribe(listener: (state: CityExplorerState) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
  private update(patch: Partial<CityExplorerState>): void {
    this.state = { ...this.state, ...patch };
    for (const listener of this.listeners) listener(this.state);
  }
  private current(name: keyof CityExplorerStore['requests'], request: number, generation: number): boolean {
    return this.requests[name] === request && this.accountGeneration === generation;
  }
  private invalidateAll(): void {
    this.requests.progress++;
    this.invalidateSelectionRequests();
  }
  private invalidateSelectionRequests(): void {
    this.requests.cities++;
    this.requests.streets++;
    this.requests.detail++;
    this.requests.contributions++;
  }
  reset(): void {
    this.accountGeneration++;
    this.mapStreetContext = false;
    this.accountController.abort();
    this.accountController = new AbortController();
    this.invalidateSelectionRequests();
    this.update({ ...INITIAL });
  }
  private expireSession(): void {
    this.reset();
    const message = 'Your session expired. Sign in again.';
    this.update({ progressStatus: 'sign-in-required', progressError: message,
      cityStatus: 'sign-in-required', cityError: message, streetStatus: 'sign-in-required', streetError: message,
      detailStatus: 'sign-in-required', detailError: message, contributionStatus: 'sign-in-required', contributionError: message });
  }
  private datasetBecameInactive(): void {
    this.invalidateSelectionRequests();
    this.update({ datasets: [], selectedDatasetId: null, cities: [], cityPage: 0, cityTotal: 0, cityCoverage: null,
      cityStatus: 'error', cityError: 'This dataset is no longer active. Refresh the dataset list.',
      selectedCityId: null, selectedCityName: null, streets: [], streetPage: 0, streetTotal: 0, streetCoverage: null, streetFilterApplied: true, streetSortApplied: true, streetStatus: 'idle', streetError: null,
      selectedStreetId: null, detail: null, remainingNodes: null, detailStatus: 'idle', detailError: null,
      contributions: [], contributionTotal: null, contributionCoverage: null, contributionStatus: 'idle', contributionError: null });
  }

  async refreshProgress(): Promise<void> {
    const request = ++this.requests.progress;
    const generation = this.accountGeneration;
    const rule = this.state.rule;
    this.update({ progressStatus: 'loading', progressError: null });
    try {
      const progress = await this.api.getProgress(rule, this.accountController.signal);
      if (!this.current('progress', request, generation) || rule !== this.state.rule) return;
      const datasets = [...progress.datasets];
      const selectedDatasetId = this.state.selectedDatasetId;
      const listedSelected = selectedDatasetId === null || datasets.some((item) => item.dataset_id === selectedDatasetId);
      if (!listedSelected && progress.datasets_truncated) {
        const lastKnown = this.state.datasets.find((item) => item.dataset_id === selectedDatasetId);
        if (lastKnown) datasets.push(lastKnown);
      } else if (!listedSelected) this.datasetBecameInactive();
      this.update({ progress, datasets, progressStatus: 'ready' });
      if (this.state.selectedCityId) await Promise.all([this.loadCities(1, false), this.loadStreets(1, false)]);
      else if (this.state.selectedDatasetId) await this.loadCities(1, false);
    } catch (error) {
      if (!this.current('progress', request, generation)) return;
      if (errorKind(error) === 'sign-in-required') return this.expireSession();
      this.update({ progressStatus: errorState(error), progressError: errorMessage(error) });
    }
  }

  setRule(rule: 'normal' | 'strict'): void {
    if (rule === this.state.rule) return;
    this.invalidateAll();
    this.update({ rule, cities: [], cityPage: 0, cityTotal: 0, cityCoverage: null, cityStatus: 'idle', cityError: null,
      streets: [], streetPage: 0, streetTotal: 0, streetCoverage: null, streetFilterApplied: true, streetSortApplied: true, streetStatus: 'idle', streetError: null,
      selectedStreetId: null, detail: null, remainingNodes: null, detailPage: 0, detailStatus: 'idle', detailError: null,
      contributions: [], contributionPage: 0, contributionTotal: null, contributionAvailable: false, contributionCoverage: null,
      contributionStatus: 'idle', contributionError: null });
    void this.refreshProgress();
  }

  selectDataset(datasetId: string | null): void {
    if (datasetId === this.state.selectedDatasetId) return;
    if (datasetId !== null && !this.state.datasets.some((dataset) => dataset.dataset_id === datasetId)) return;
    this.mapStreetContext = false;
    this.invalidateSelectionRequests();
    this.update({ selectedDatasetId: datasetId, cities: [], cityPage: 0, cityTotal: 0, cityCoverage: null,
      cityStatus: datasetId ? 'loading' : 'idle', cityError: null, selectedCityId: null, selectedCityName: null,
      streets: [], streetPage: 0, streetTotal: 0, streetCoverage: null, streetFilterApplied: true, streetSortApplied: true, streetStatus: 'idle', streetError: null,
      selectedStreetId: null, detail: null, remainingNodes: null, detailStatus: 'idle', detailError: null,
      contributions: [], contributionPage: 0, contributionTotal: null, contributionAvailable: false, contributionCoverage: null, contributionStatus: 'idle', contributionError: null });
    if (datasetId) void this.loadCities(1, false);
  }

  setCityQuery(query: string): void {
    if (query === this.state.cityQuery) return;
    this.update({ cityQuery: query, cities: [], cityPage: 0, cityTotal: 0, cityCoverage: null, cityStatus: 'idle', cityError: null });
    if (this.state.selectedDatasetId) void this.loadCities(1, false);
  }
  async loadMoreCities(): Promise<void> {
    if (this.state.cityStatus === 'ready' && this.state.cities.length < this.state.cityTotal) await this.loadCities(this.state.cityPage + 1, true);
  }
  private async loadCities(page: number, append: boolean): Promise<void> {
    const datasetId = this.state.selectedDatasetId;
    if (!datasetId) return;
    const request = ++this.requests.cities;
    const generation = this.accountGeneration;
    const query = this.state.cityQuery;
    const rule = this.state.rule;
    this.update({ cityStatus: append ? 'loading-more' : 'loading', cityError: null });
    try {
      const response = await this.api.getCities(datasetId, { rule, q: query, page, pageSize: PAGE_SIZE }, this.accountController.signal);
      if (!this.current('cities', request, generation) || datasetId !== this.state.selectedDatasetId || query !== this.state.cityQuery || rule !== this.state.rule) return;
      if (response.dataset_id !== datasetId || response.dataset_state !== 'active') return this.datasetBecameInactive();
      if (append && this.state.cityCoverage && (this.state.cityCoverage.progress_revision !== response.coverage.progress_revision || this.state.cityCoverage.status !== response.coverage.status)) {
        await this.loadCities(1, false);
        return;
      }
      const cities = response.items;
      this.update({ cities: append ? [...this.state.cities, ...cities] : cities, cityPage: response.page,
        cityTotal: response.total, cityCoverage: response.coverage,
        cityStatus: cities.length || (append && this.state.cities.length) ? 'ready' : 'empty' });
    } catch (error) {
      if (!this.current('cities', request, generation)) return;
      if (errorKind(error) === 'sign-in-required') return this.expireSession();
      this.update({ cityStatus: errorState(error), cityError: errorMessage(error) });
    }
  }

  selectCity(cityId: string): void {
    if (!this.state.cities.some((city) => city.id === cityId)) return;
    this.invalidateSelectionRequests();
    this.mapStreetContext = false;
    this.update({ selectedCityId: cityId, selectedCityName: this.state.cities.find((city) => city.id === cityId)?.name ?? null, streetQuery: '', streetFilter: 'all', streetSort: 'name', streets: [], streetPage: 0,
      streetTotal: 0, streetCoverage: null, streetFilterApplied: true, streetSortApplied: true, streetStatus: 'loading', streetError: null, selectedStreetId: null,
      detail: null, remainingNodes: null, detailStatus: 'idle', detailError: null,
      contributions: [], contributionPage: 0, contributionTotal: null, contributionCoverage: null, contributionStatus: 'idle', contributionError: null });
    void this.loadStreets(1, false);
  }
  backToCities(): void {
    const reloadCities = this.mapStreetContext && this.state.selectedDatasetId !== null;
    this.mapStreetContext = false;
    this.invalidateSelectionRequests();
    this.update({ selectedCityId: null, selectedCityName: null, streets: [], streetPage: 0, streetTotal: 0, streetCoverage: null, streetFilterApplied: true, streetSortApplied: true, streetStatus: 'idle', streetError: null,
      selectedStreetId: null, detail: null, remainingNodes: null, detailStatus: 'idle', detailError: null,
      contributions: [], contributionPage: 0, contributionTotal: null, contributionCoverage: null, contributionStatus: 'idle', contributionError: null });
    if (reloadCities) void this.loadCities(1, false);
  }
  setStreetQuery(query: string): void {
    if (query === this.state.streetQuery) return;
    this.update({ streetQuery: query, streets: [], streetPage: 0, streetTotal: 0, streetCoverage: null, streetStatus: 'idle', streetError: null });
    if (this.state.selectedCityId) void this.loadStreets(1, false);
  }
  setStreetFilter(filter: StreetFilter): void {
    if (filter === this.state.streetFilter) return;
    this.update({ streetFilter: filter, streets: [], streetPage: 0, streetTotal: 0, streetCoverage: null, streetFilterApplied: true, streetStatus: 'idle', streetError: null });
    if (this.state.selectedCityId) void this.loadStreets(1, false);
  }
  setStreetSort(sort: StreetSort): void {
    if (sort === this.state.streetSort) return;
    this.update({ streetSort: sort, streets: [], streetPage: 0, streetTotal: 0, streetCoverage: null,
      streetSortApplied: sort === 'name', streetStatus: 'idle', streetError: null });
    if (this.state.selectedCityId) void this.loadStreets(1, false);
  }
  async loadMoreStreets(): Promise<void> {
    if (this.state.streetStatus === 'ready' && this.state.streets.length < this.state.streetTotal) await this.loadStreets(this.state.streetPage + 1, true);
  }
  private async loadStreets(page: number, append: boolean): Promise<void> {
    const datasetId = this.state.selectedDatasetId;
    const cityId = this.state.selectedCityId;
    if (!datasetId || !cityId) return;
    const request = ++this.requests.streets;
    const generation = this.accountGeneration;
    const { rule, streetFilter, streetSort } = this.state;
    const query = this.state.streetQuery;
    this.update({ streetStatus: append ? 'loading-more' : 'loading', streetError: null });
    try {
      const response = await this.api.getStreets(cityId, { datasetId, rule, filter: streetFilter, sort: streetSort, q: query, page, pageSize: PAGE_SIZE }, this.accountController.signal);
      if (!this.current('streets', request, generation) || datasetId !== this.state.selectedDatasetId || cityId !== this.state.selectedCityId ||
        rule !== this.state.rule || streetFilter !== this.state.streetFilter || streetSort !== this.state.streetSort || query !== this.state.streetQuery) return;
      if (response.dataset_id !== datasetId || response.city_id !== cityId || response.dataset_state !== 'active') return this.datasetBecameInactive();
      if (append && this.state.streetCoverage && (this.state.streetCoverage.progress_revision !== response.coverage.progress_revision || this.state.streetCoverage.status !== response.coverage.status)) {
        await this.loadStreets(1, false);
        return;
      }
      this.update({ streets: append ? [...this.state.streets, ...response.items] : response.items, streetPage: response.page,
        streetTotal: response.total, streetCoverage: response.coverage, streetFilterApplied: response.filter_applied, streetSortApplied: response.sort_applied,
        streetStatus: response.items.length || (append && this.state.streets.length) ? 'ready' : 'empty' });
    } catch (error) {
      if (!this.current('streets', request, generation)) return;
      if (errorKind(error) === 'sign-in-required') return this.expireSession();
      this.update({ streetStatus: errorState(error), streetError: errorMessage(error) });
    }
  }

  selectStreet(streetId: string): void {
    if (!this.state.streets.some((street) => street.id === streetId)) return;
    this.invalidateSelectionRequests();
    this.update({ selectedStreetId: streetId, detail: null, remainingNodes: null, detailPage: 0, detailStatus: 'loading', detailError: null,
      contributions: [], contributionPage: 0, contributionTotal: null, contributionAvailable: false, contributionCoverage: null, contributionStatus: 'loading', contributionError: null });
    void this.loadStreetDetail(1, false);
    void this.loadContributions(1, false);
  }
  async openMapStreet(datasetId: string, cityId: string, streetId: string, cityName?: string): Promise<void> {
    if (!validBigintId(datasetId) || !validBigintId(cityId) || !validBigintId(streetId)) return;
    this.invalidateSelectionRequests();
    this.mapStreetContext = true;
    const knownCityName = this.state.selectedDatasetId === datasetId ? this.state.cities.find((city) => city.id === cityId)?.name : undefined;
    this.update({ selectedDatasetId: datasetId, selectedCityId: cityId, selectedCityName: cityName?.trim() || knownCityName || null,
      cityQuery: '', cities: [], cityPage: 0, cityTotal: 0, cityCoverage: null, cityStatus: 'idle', cityError: null,
      streetQuery: '', streetFilter: 'all', streetSort: 'name', streets: [], streetPage: 0, streetTotal: 0, streetCoverage: null,
      streetFilterApplied: true, streetSortApplied: true, streetStatus: 'idle', streetError: null, selectedStreetId: streetId,
      detail: null, remainingNodes: null, detailPage: 0, detailStatus: 'loading', detailError: null,
      contributions: [], contributionPage: 0, contributionTotal: null, contributionAvailable: false, contributionCoverage: null,
      contributionStatus: 'loading', contributionError: null });
    await Promise.all([this.loadStreetDetail(1, false), this.loadContributions(1, false)]);
  }
  backToStreets(): void {
    this.invalidateSelectionRequests();
    this.update({ selectedStreetId: null, detail: null, remainingNodes: null, detailPage: 0, detailStatus: 'idle', detailError: null,
      contributions: [], contributionPage: 0, contributionTotal: null, contributionAvailable: false, contributionCoverage: null, contributionStatus: 'idle', contributionError: null });
    if (this.mapStreetContext && this.state.selectedDatasetId && this.state.selectedCityId) {
      void this.loadStreets(1, false);
    }
  }
  async loadMoreNodes(): Promise<void> {
    const total = this.state.detail?.remaining_nodes_page.total;
    if (total !== null && total !== undefined && this.state.remainingNodes && this.state.remainingNodes.length < total && this.state.detailStatus === 'ready') {
      await this.loadStreetDetail(this.state.detailPage + 1, true);
    }
  }
  private async loadStreetDetail(page: number, append: boolean): Promise<void> {
    const datasetId = this.state.selectedDatasetId;
    const streetId = this.state.selectedStreetId;
    if (!datasetId || !streetId) return;
    const request = ++this.requests.detail;
    const generation = this.accountGeneration;
    const rule = this.state.rule;
    this.update({ detailStatus: append ? 'loading-more' : 'loading', detailError: null });
    try {
      const detail = await this.api.getStreet(streetId, { datasetId, rule, page, pageSize: PAGE_SIZE }, this.accountController.signal);
      if (!this.current('detail', request, generation) || datasetId !== this.state.selectedDatasetId || streetId !== this.state.selectedStreetId || rule !== this.state.rule) return;
      if (detail.dataset_state !== 'active') return this.datasetBecameInactive();
      if (detail.dataset_id !== datasetId || detail.id !== streetId) {
        this.requests.contributions++;
        this.update({ detail: null, remainingNodes: null, detailStatus: 'error', detailError: 'The street response did not match the requested street.',
          contributions: [], contributionTotal: null, contributionAvailable: false, contributionStatus: 'idle', contributionError: null }); return;
      }
      if (detail.city_id !== this.state.selectedCityId) {
        this.requests.contributions++;
        this.update({ detail: null, remainingNodes: null, detailStatus: 'error', detailError: 'This street does not belong to the selected city.',
          contributions: [], contributionTotal: null, contributionAvailable: false, contributionStatus: 'idle', contributionError: null }); return;
      }
      if (append && this.state.detail && (this.state.detail.coverage.progress_revision !== detail.coverage.progress_revision || this.state.detail.coverage.status !== detail.coverage.status)) {
        await this.loadStreetDetail(1, false);
        return;
      }
      const nodes = detail.remaining_nodes;
      this.update({ detail, remainingNodes: nodes === null ? null : append ? [...(this.state.remainingNodes ?? []), ...nodes] : nodes,
        detailPage: page, detailStatus: 'ready' });
    } catch (error) {
      if (!this.current('detail', request, generation)) return;
      if (errorKind(error) === 'sign-in-required') return this.expireSession();
      this.update({ detailStatus: errorState(error), detailError: errorMessage(error) });
    }
  }
  async loadMoreContributions(): Promise<void> {
    if (this.state.contributionAvailable && this.state.contributionTotal !== null && this.state.contributions.length < this.state.contributionTotal && this.state.contributionStatus === 'ready') {
      await this.loadContributions(this.state.contributionPage + 1, true);
    }
  }
  private async loadContributions(page: number, append: boolean): Promise<void> {
    const datasetId = this.state.selectedDatasetId;
    const streetId = this.state.selectedStreetId;
    if (!datasetId || !streetId) return;
    const request = ++this.requests.contributions;
    const generation = this.accountGeneration;
    this.update({ contributionStatus: append ? 'loading-more' : 'loading', contributionError: null });
    try {
      const response: ContributionPage = await this.api.getContributions(streetId, { datasetId, page, pageSize: PAGE_SIZE }, this.accountController.signal);
      if (!this.current('contributions', request, generation) || datasetId !== this.state.selectedDatasetId || streetId !== this.state.selectedStreetId) return;
      if (response.dataset_id !== datasetId || response.street_id !== streetId || response.dataset_state !== 'active') return this.datasetBecameInactive();
      if (append && this.state.contributionCoverage && (this.state.contributionCoverage.progress_revision !== response.coverage.progress_revision || this.state.contributionCoverage.status !== response.coverage.status)) {
        await this.loadContributions(1, false);
        return;
      }
      this.update({ contributions: append ? [...this.state.contributions, ...response.activities] : response.activities,
        contributionPage: response.page, contributionTotal: response.total, contributionAvailable: response.activities_available,
        contributionCoverage: response.coverage,
        contributionStatus: response.activities_available ? response.activities.length || (append && this.state.contributions.length) ? 'ready' : 'empty' : 'ready' });
    } catch (error) {
      if (!this.current('contributions', request, generation)) return;
      if (errorKind(error) === 'sign-in-required') return this.expireSession();
      this.update({ contributionStatus: errorState(error), contributionError: errorMessage(error) });
    }
  }

  async retry(): Promise<void> {
    if (this.state.progressStatus === 'offline' || this.state.progressStatus === 'error' || this.state.progressStatus === 'sign-in-required') return this.refreshProgress();
    if (!this.state.selectedDatasetId) return this.refreshProgress();
    if (this.state.selectedStreetId) {
      await Promise.all([this.loadStreetDetail(1, false), this.loadContributions(1, false)]);
    } else if (this.state.selectedCityId) await this.loadStreets(1, false);
    else await this.loadCities(1, false);
  }

  async refreshAfterCorrection(): Promise<void> {
    const account = this.accountGeneration;
    this.invalidateAll();
    this.update({ progressStatus: this.state.progress ? 'loading' : 'idle', progressError: null,
      cities: [], cityPage: 0, cityTotal: 0, cityCoverage: null, cityStatus: this.state.selectedDatasetId ? 'loading' : 'idle', cityError: null,
      streets: [], streetPage: 0, streetTotal: 0, streetCoverage: null, streetStatus: this.state.selectedCityId ? 'loading' : 'idle', streetError: null,
      detail: null, remainingNodes: null, detailPage: 0, detailStatus: this.state.selectedStreetId ? 'loading' : 'idle', detailError: null,
      contributions: [], contributionPage: 0, contributionTotal: null, contributionCoverage: null,
      contributionStatus: this.state.selectedStreetId ? 'loading' : 'idle', contributionError: null });
    await this.refreshProgress();
    if (account !== this.accountGeneration) return;
    const datasetId = this.state.selectedDatasetId;
    const streetId = this.state.selectedStreetId;
    if (!datasetId || !streetId) return;
    await Promise.all([this.loadStreetDetail(1, false), this.loadContributions(1, false)]);
  }
  clearSelection(): void {
    this.mapStreetContext = false;
    this.invalidateSelectionRequests();
    this.update({ selectedDatasetId: null, cities: [], cityPage: 0, cityTotal: 0, cityCoverage: null, cityStatus: 'idle', cityError: null,
      selectedCityId: null, selectedCityName: null, streets: [], streetPage: 0, streetTotal: 0, streetCoverage: null, streetFilterApplied: true, streetSortApplied: true, streetStatus: 'idle', streetError: null,
      selectedStreetId: null, detail: null, remainingNodes: null, detailStatus: 'idle', detailError: null,
      contributions: [], contributionPage: 0, contributionTotal: null, contributionAvailable: false, contributionCoverage: null,
      contributionStatus: 'idle', contributionError: null });
  }
}
