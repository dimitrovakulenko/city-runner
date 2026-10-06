import { ApiError } from './api/client';
import type { ApiErrorKind } from './api/client';
import type { GpxFile, ExploreApi } from './api/explore';
import type { MapResponse, ProgressResponse, UploadResponse, UploadStatusResponse } from './api/generated';

export type LoadState = 'idle' | 'loading' | 'ready' | 'empty' | 'offline' | 'sign-in-required' | 'error';
export type Rule = 'normal' | 'strict';
export interface Viewport { bbox: [number, number, number, number]; zoom: number }
export interface UploadItem extends UploadStatusResponse {
  fileName: string;
  duplicate: boolean;
  polls: number;
  polling: 'active' | 'stopped';
}

export interface ExploreState {
  rule: Rule;
  viewport: Viewport | null;
  map: MapResponse | null;
  progress: ProgressResponse | null;
  mapStatus: LoadState;
  progressStatus: LoadState;
  mapError: string | null;
  progressError: string | null;
  uploads: UploadItem[];
  uploading: boolean;
  uploadError: string | null;
  foregroundPollingStopped: boolean;
}

const INITIAL_STATE: ExploreState = {
  rule: 'normal', viewport: null, map: null, progress: null,
  mapStatus: 'idle', progressStatus: 'idle', mapError: null, progressError: null,
  uploads: [], uploading: false, uploadError: null, foregroundPollingStopped: false,
};

function errorKind(error: unknown): ApiErrorKind | 'http' { return error instanceof ApiError ? error.kind : 'http'; }
function errorMessage(error: unknown): string { return error instanceof Error ? error.message : 'Request failed. Retry when connected.'; }
function errorState(error: unknown): 'offline' | 'sign-in-required' | 'error' {
  const kind = errorKind(error);
  return kind === 'offline' || kind === 'sign-in-required' ? kind : 'error';
}
function hasPendingWork(progress: ProgressResponse | null): boolean {
  return Boolean(progress && (progress.state === 'pending' || progress.pending_imports > 0 ||
    progress.datasets.some((dataset) => dataset.state === 'pending' || (dataset.pending_sources ?? 0) > 0)));
}

export class ExploreStore {
  private state = INITIAL_STATE;
  private mapRequest = 0;
  private progressRequest = 0;
  private accountRevision = 0;
  private accountController = new AbortController();
  private foregroundPolls = 0;
  private operationGeneration = 0;
  private pollOwner: number | null = null;
  private foregroundOwner: number | null = null;
  private viewportChangePending = false;
  private listeners = new Set<(state: ExploreState) => void>();

  constructor(private readonly api: ExploreApi) {}

  getState(): ExploreState { return this.state; }
  getAccountGeneration(): number { return this.accountRevision; }
  subscribe(listener: (state: ExploreState) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
  private update(patch: Partial<ExploreState>): void {
    this.state = { ...this.state, ...patch };
    for (const listener of this.listeners) listener(this.state);
  }
  private clearForExpiredSession(): void {
    this.reset();
    this.update({ mapStatus: 'sign-in-required', progressStatus: 'sign-in-required',
      mapError: 'Your session expired. Sign in again.', progressError: 'Your session expired. Sign in again.' });
  }

  reset(): void {
    this.mapRequest++;
    this.progressRequest++;
    this.accountRevision++;
    this.accountController.abort();
    this.accountController = new AbortController();
    this.operationGeneration++;
    this.pollOwner = null;
    this.foregroundOwner = null;
    this.foregroundPolls = 0;
    this.viewportChangePending = false;
    this.update({ ...INITIAL_STATE });
  }

  invalidateViewport(): void {
    this.mapRequest++;
    if (this.viewportChangePending) return;
    this.viewportChangePending = true;
    this.update({ map: null, mapStatus: 'loading', mapError: null });
  }

  focusCoordinates(longitude: number, latitude: number, zoom = 18): void {
    const span = 0.0015;
    this.mapRequest++;
    this.progressRequest++;
    this.viewportChangePending = false;
    this.update({ viewport: { bbox: [longitude - span, latitude - span, longitude + span, latitude + span], zoom },
      map: null, mapStatus: 'loading', mapError: null });
  }

  async retry(): Promise<void> {
    this.foregroundPolls = 0;
    const uploads = this.state.uploads.map((item) => item.status === 'queued' || item.status === 'processing'
      ? { ...item, polls: 0, polling: 'active' as const } : item);
    this.update({ foregroundPollingStopped: false, uploads });
    if (this.state.viewport) await this.refreshViewport(this.state.viewport);
    else await this.refreshProgress();
    await this.pollUploads();
  }

  async refreshAfterCorrection(): Promise<void> {
    if (this.state.viewport) await this.refreshViewport(this.state.viewport);
    else await this.refreshProgress();
  }

  async refreshViewport(viewport: Viewport): Promise<void> {
    this.viewportChangePending = false;
    this.update({ viewport, map: null, mapStatus: 'loading', progressStatus: 'loading', mapError: null, progressError: null });
    const mapRequest = ++this.mapRequest;
    const progressRequest = ++this.progressRequest;
    const accountRevision = this.accountRevision;
    const { rule } = this.state;
    await Promise.all([
      this.api.getMap(viewport.bbox, viewport.zoom, rule, this.accountController.signal).then((map) => {
        if (mapRequest !== this.mapRequest || accountRevision !== this.accountRevision) return;
        this.update({ map, mapStatus: map.cities.length || map.tracks.length || map.streets.length || map.missing_nodes.length ? 'ready' : 'empty' });
      }).catch((error: unknown) => {
        if (mapRequest !== this.mapRequest || accountRevision !== this.accountRevision) return;
        if (errorKind(error) === 'sign-in-required') return this.clearForExpiredSession();
        this.update({ mapStatus: errorState(error), mapError: errorMessage(error) });
      }),
      this.api.getProgress(rule, this.accountController.signal).then((progress) => {
        if (progressRequest !== this.progressRequest || accountRevision !== this.accountRevision) return;
        this.update({ progress, progressStatus: 'ready' });
        if (!hasPendingWork(progress)) {
          this.foregroundPolls = 0;
          this.update({ foregroundPollingStopped: false });
        }
      }).catch((error: unknown) => {
        if (progressRequest !== this.progressRequest || accountRevision !== this.accountRevision) return;
        if (errorKind(error) === 'sign-in-required') return this.clearForExpiredSession();
        this.update({ progressStatus: errorState(error), progressError: errorMessage(error) });
      }),
    ]);
  }

  async refreshProgress(): Promise<void> {
    const request = ++this.progressRequest;
    const accountRevision = this.accountRevision;
    const rule = this.state.rule;
    this.update({ progressStatus: 'loading', progressError: null });
    try {
      const progress = await this.api.getProgress(rule, this.accountController.signal);
      if (request !== this.progressRequest || accountRevision !== this.accountRevision) return;
      this.update({ progress, progressStatus: 'ready' });
      if (!hasPendingWork(progress)) {
        this.foregroundPolls = 0;
        this.update({ foregroundPollingStopped: false });
      }
    } catch (error) {
      if (request !== this.progressRequest || accountRevision !== this.accountRevision) return;
      if (errorKind(error) === 'sign-in-required') return this.clearForExpiredSession();
      this.update({ progressStatus: errorState(error), progressError: errorMessage(error) });
    }
  }

  setRule(rule: Rule): void {
    if (rule === this.state.rule) return;
    this.update({ rule, map: null, progress: null, mapStatus: 'loading', progressStatus: 'loading', mapError: null, progressError: null });
    if (this.state.viewport) void this.refreshViewport(this.state.viewport);
    else void this.refreshProgress();
  }

  async uploadFile(file: GpxFile): Promise<void> {
    const revision = this.accountRevision;
    this.foregroundPolls = 0;
    this.update({ uploading: true, uploadError: null, foregroundPollingStopped: false });
    try {
      const result: UploadResponse = await this.api.upload(file, this.accountController.signal);
      if (revision !== this.accountRevision) return;
      const status: UploadStatusResponse = { id: result.id, status: result.status, job_id: result.job_id, activity_id: null, error: null };
      const item: UploadItem = { ...status, fileName: file.name, duplicate: result.duplicate, polls: 0,
        polling: result.status === 'queued' || result.status === 'processing' ? 'active' : 'stopped' };
      this.update({ uploads: [item, ...this.state.uploads.filter((upload) => upload.id !== item.id)], uploading: false });
      if (item.polling === 'stopped') await this.refreshAfterUpload();
    } catch (error) {
      if (revision !== this.accountRevision) return;
      this.update({ uploading: false, uploadError: errorMessage(error) });
      if (errorKind(error) === 'sign-in-required') this.clearForExpiredSession();
    }
  }

  async pollUploads(): Promise<void> {
    const active = this.state.uploads.filter((item) => item.polling === 'active');
    if (!active.length || this.pollOwner !== null) return;
    const owner = ++this.operationGeneration;
    this.pollOwner = owner;
    const revision = this.accountRevision;
    try {
      await Promise.all(active.map(async (item) => {
        if (item.polls >= 30) {
          this.replaceUpload({ ...item, polling: 'stopped' });
          return;
        }
        try {
          const status = await this.api.getUpload(item.id, this.accountController.signal);
          if (revision !== this.accountRevision) return;
          const polls = item.polls + 1;
          const activeStatus = status.status === 'queued' || status.status === 'processing';
          this.replaceUpload({ ...item, ...status, polls, polling: activeStatus && polls < 30 ? 'active' : 'stopped' });
          if (!activeStatus) await this.refreshAfterUpload();
        } catch (error) {
          if (revision !== this.accountRevision) return;
          const polls = item.polls + 1;
          if (errorKind(error) === 'sign-in-required') this.clearForExpiredSession();
          else this.replaceUpload({ ...item, polls, polling: polls < 30 ? 'active' : 'stopped', error: errorMessage(error) });
        }
      }));
    } finally {
      if (this.pollOwner === owner) this.pollOwner = null;
    }
  }

  async refreshForeground(): Promise<void> {
    if (this.foregroundOwner !== null) return;
    const hasActiveUploads = this.state.uploads.some((item) => item.polling === 'active');
    const progressPending = hasPendingWork(this.state.progress);
    if ((!hasActiveUploads && !progressPending) || this.state.foregroundPollingStopped) return;
    if (this.foregroundPolls >= 30) {
      this.update({ foregroundPollingStopped: true });
      return;
    }
    this.foregroundPolls++;
    const owner = ++this.operationGeneration;
    this.foregroundOwner = owner;
    const revision = this.accountRevision;
    try {
      await Promise.all([this.pollUploads(), this.refreshProgress()]);
      if (revision === this.accountRevision && !this.viewportChangePending && this.state.viewport &&
        (hasActiveUploads || progressPending || hasPendingWork(this.state.progress))) {
        await this.refreshMapOnly();
      }
      if (revision === this.accountRevision && this.foregroundPolls >= 30) this.update({ foregroundPollingStopped: true });
    } finally {
      if (this.foregroundOwner === owner) this.foregroundOwner = null;
    }
  }

  private async refreshAfterUpload(): Promise<void> {
    if (this.state.viewport) await this.refreshViewport(this.state.viewport);
    else await this.refreshProgress();
  }

  private async refreshMapOnly(): Promise<void> {
    const viewport = this.state.viewport;
    if (!viewport || this.viewportChangePending) return;
    const request = ++this.mapRequest;
    const revision = this.accountRevision;
    try {
      const map = await this.api.getMap(viewport.bbox, viewport.zoom, this.state.rule, this.accountController.signal);
      if (request !== this.mapRequest || revision !== this.accountRevision || this.viewportChangePending) return;
      this.update({ map, mapStatus: map.cities.length || map.tracks.length || map.streets.length || map.missing_nodes.length ? 'ready' : 'empty' });
    } catch (error) {
      if (request !== this.mapRequest || revision !== this.accountRevision) return;
      if (errorKind(error) === 'sign-in-required') return this.clearForExpiredSession();
      this.update({ mapStatus: errorState(error), mapError: errorMessage(error) });
    }
  }

  private replaceUpload(upload: UploadItem): void {
    this.update({ uploads: this.state.uploads.map((item) => item.id === upload.id ? upload : item) });
  }
}
