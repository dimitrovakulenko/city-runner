import { ApiError } from './api/client';
import type { ApiErrorKind } from './api/client';
import type { CoveragePreparationCity, CoveragePreparationResponse } from './api/generated';
import type { CoveragePreparationApi } from './api/coveragePreparation';

export type CoveragePreparationLoad = 'idle' | 'loading' | 'ready' | 'offline' | 'error' | 'sign-in-required';

export interface CoveragePreparationState {
  data: CoveragePreparationResponse | null;
  status: CoveragePreparationLoad;
  error: string | null;
  retrying: boolean;
}

const INITIAL: CoveragePreparationState = { data: null, status: 'idle', error: null, retrying: false };

function kind(error: unknown): ApiErrorKind | 'http' { return error instanceof ApiError ? error.kind : 'http'; }
function message(error: unknown): string { return error instanceof Error ? error.message : 'Could not refresh city preparation. Retry when connected.'; }
function isCity(value: unknown): value is CoveragePreparationCity {
  if (!value || typeof value !== 'object') return false;
  const city = value as CoveragePreparationCity;
  return typeof city.osm_relation_id === 'string' && !!city.osm_relation_id && typeof city.name === 'string' &&
    (city.state === 'queued' || city.state === 'fetching' || city.state === 'importing' || city.state === 'ready' || city.state === 'failed') &&
    (city.error === null || typeof city.error === 'string');
}
function isResponse(value: unknown): value is CoveragePreparationResponse {
  if (!value || typeof value !== 'object') return false;
  const response = value as CoveragePreparationResponse;
  return typeof response.enabled === 'boolean' &&
    (response.state === 'ready' || response.state === 'pending' || response.state === 'failed' || response.state === 'unsupported') &&
    [response.pending_sources, response.failed_sources, response.unsupported_sources].every((count) => Number.isInteger(count) && count >= 0) &&
    Array.isArray(response.cities) && response.cities.length <= 20 && response.cities.every(isCity) &&
    new Set(response.cities.map((city) => city.osm_relation_id)).size === response.cities.length && typeof response.cities_truncated === 'boolean';
}

export class CoveragePreparationStore {
  private state: CoveragePreparationState = INITIAL;
  private accountGeneration = 0;
  private requestGeneration = 0;
  private retryGeneration = 0;
  private requestController: AbortController | null = null;
  private retryController: AbortController | null = null;
  private listeners = new Set<(state: CoveragePreparationState) => void>();

  constructor(private readonly api: CoveragePreparationApi) {}

  getState(): CoveragePreparationState { return this.state; }
  getAccountGeneration(): number { return this.accountGeneration; }
  subscribe(listener: (state: CoveragePreparationState) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
  private update(patch: Partial<CoveragePreparationState>): void {
    this.state = { ...this.state, ...patch };
    for (const listener of this.listeners) listener(this.state);
  }

  reset(): void {
    this.accountGeneration++;
    this.requestGeneration++;
    this.retryGeneration++;
    this.requestController?.abort(); this.requestController = null;
    this.retryController?.abort(); this.retryController = null;
    this.update({ ...INITIAL });
  }

  async refresh(): Promise<boolean> {
    const account = this.accountGeneration;
    const request = ++this.requestGeneration;
    const controller = new AbortController();
    this.requestController?.abort();
    this.requestController = controller;
    const current = () => account === this.accountGeneration && request === this.requestGeneration && !controller.signal.aborted;
    this.update({ status: 'loading', error: null });
    try {
      const data = await this.api.getStatus(controller.signal);
      if (!current()) return false;
      if (!isResponse(data)) throw new Error('The city preparation response was invalid. Retry the status check.');
      this.update({ data, status: 'ready', error: null });
      return true;
    } catch (error) {
      if (!current()) return false;
      if (kind(error) === 'sign-in-required') {
        this.expireSession(message(error));
      } else if (kind(error) === 'stale-session') {
        this.reset();
      } else {
        this.update({ status: kind(error) === 'offline' ? 'offline' : 'error', error: message(error) });
      }
      return false;
    } finally {
      if (this.requestController === controller) this.requestController = null;
    }
  }

  async retry(): Promise<boolean> {
    if (!this.state.data?.enabled || this.state.retrying) return false;
    const account = this.accountGeneration;
    const operation = ++this.retryGeneration;
    const controller = new AbortController();
    this.retryController?.abort(); this.retryController = controller;
    const current = () => account === this.accountGeneration && operation === this.retryGeneration && !controller.signal.aborted;
    this.update({ retrying: true, error: null });
    try {
      await this.api.retry(controller.signal);
      if (!current()) return false;
      await this.refresh();
      return current();
    } catch (error) {
      if (!current()) return false;
      if (kind(error) === 'sign-in-required') this.expireSession(message(error));
      else if (kind(error) === 'stale-session') this.reset();
      else this.update({ error: message(error) });
      return false;
    } finally {
      if (this.retryController === controller) this.retryController = null;
      if (current()) this.update({ retrying: false });
    }
  }

  private expireSession(error: string): void {
    this.reset();
    this.update({ status: 'sign-in-required', error });
  }
}
