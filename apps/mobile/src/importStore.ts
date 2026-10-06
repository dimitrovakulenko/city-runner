import type { ImportBatchCreate, ImportBatchItemResponse, ImportBatchPage, ImportBatchResponse } from './api/generated';
import type { ImportBatchApi } from './api/importBatches';
import type { GpxFile } from './api/explore';
import { uploadSequentially } from './importQueue';
import { prepareImportSelection } from './importSelection';

export interface ImportState {
  batches: ImportBatchResponse[];
  page: number;
  total: number;
  loading: boolean;
  uploadingBatchId: string | null;
  uploadingItemId: string | null;
  stoppedLocally: boolean;
  polls: number;
  pollingStopped: boolean;
  error: string | null;
  rejected: Array<{ name: string; reason: string }>;
}

const INITIAL: ImportState = { batches: [], page: 0, total: 0, loading: false, uploadingBatchId: null, uploadingItemId: null,
  stoppedLocally: false, polls: 0, pollingStopped: false, error: null, rejected: [] };
const ACTIVE = new Set(['queued', 'processing']);
const basename = (name: string) => (name.replace(/\\/g, '/').split('/').pop() ?? '').replace(/[\p{C}\p{Z}]/gu, (character) => character === ' ' ? character : '').trim();

export class ImportStore {
  private state = INITIAL;
  private revision = 0;
  private controller = new AbortController();
  private uploadOwner: { batchId: string; owner: number } | null = null;
  private uploadGeneration = 0;
  private stopRequested = new Set<string>();
  private resumeRequested = new Set<string>();
  private batchMutationOwners = new Set<string>();
  private files = new Map<string, Map<string, GpxFile>>();
  private polling = false;
  private pollingOwner: number | null = null;
  private pollingGeneration = 0;
  private batchRequestGeneration = new Map<string, number>();
  private listRequestGeneration = 0;
  private pollCursor = 0;
  private listeners = new Set<(state: ImportState) => void>();

  constructor(private readonly api: ImportBatchApi, private readonly randomUUID: () => string,
    private readonly onSucceeded: () => void | Promise<void> = () => {}) {}

  getState(): ImportState { return this.state; }
  getAccountGeneration(): number { return this.revision; }
  hasSelectedFile(batchId: string, itemId: string): boolean { return this.files.get(batchId)?.has(itemId) ?? false; }
  setError(error: string): void { this.update({ error }); }
  subscribe(listener: (state: ImportState) => void): () => void { this.listeners.add(listener); return () => this.listeners.delete(listener); }
  private update(patch: Partial<ImportState>): void { this.state = { ...this.state, ...patch }; for (const listener of this.listeners) listener(this.state); }

  reset(): void {
    this.revision++;
    this.controller.abort(); this.controller = new AbortController();
    this.uploadOwner = null; this.stopRequested.clear(); this.resumeRequested.clear(); this.batchMutationOwners.clear(); this.files.clear(); this.polling = false; this.pollingOwner = null;
    this.batchRequestGeneration.clear();
    this.listRequestGeneration++;
    this.update({ ...INITIAL });
  }

  async loadPage(page = 1): Promise<void> {
    const revision = this.revision; const generation = ++this.listRequestGeneration;
    const batchIdsAtStart = new Set(this.state.batches.map((batch) => batch.id));
    const detailGenerations = new Map<string, number>();
    for (const batch of this.state.batches) {
      const next = (this.batchRequestGeneration.get(batch.id) ?? 0) + 1;
      this.batchRequestGeneration.set(batch.id, next);
      detailGenerations.set(batch.id, next);
    }
    this.update({ loading: true, error: null });
    try {
      const result: ImportBatchPage = await this.api.listBatches(page, this.controller.signal);
      if (revision !== this.revision || generation !== this.listRequestGeneration) return;
      const currentById = new Map(this.state.batches.map((batch) => [batch.id, batch]));
      const incoming = result.items.map((batch) => {
        const current = currentById.get(batch.id);
        if (!current) return batch;
        if ((this.batchRequestGeneration.get(batch.id) ?? 0) > (detailGenerations.get(batch.id) ?? 0)) return current;
        return mergeBatch(current, batch);
      });
      const arrivedWhileLoading = this.state.batches.filter((batch) => !batchIdsAtStart.has(batch.id) && !incoming.some((listed) => listed.id === batch.id));
      const batches = page === 1
        ? [...arrivedWhileLoading, ...incoming]
        : [...this.state.batches, ...incoming.filter((batch) => !this.state.batches.some((current) => current.id === batch.id))];
      const total = result.total + arrivedWhileLoading.length;
      this.update({ batches, page: result.page, total: Math.max(total, batches.length), loading: false });
      if (incoming.some((batch) => this.newlySucceeded(currentById.get(batch.id), batch))) void this.onSucceeded();
    } catch (error) { if (revision === this.revision && generation === this.listRequestGeneration) this.update({ loading: false, error: message(error) }); }
  }

  async loadMore(): Promise<void> { if (!this.state.loading && this.state.batches.length < this.state.total) await this.loadPage(this.state.page + 1); }

  async createFromSelection(files: GpxFile[]): Promise<void> {
    const selection = prepareImportSelection(files);
    this.update({ rejected: selection.rejected, error: null });
    if (!selection.accepted.length) return;
    const revision = this.revision;
    try {
      const body: ImportBatchCreate = { request_id: this.randomUUID(), files: selection.accepted.map((file) => ({ name: basename(file.name), format: file.kind })) };
      const batch = await this.api.createBatch(body, this.controller.signal);
      if (revision !== this.revision) return;
      const local = new Map<string, GpxFile>();
      batch.items.forEach((item, index) => { const file = selection.accepted[index]; if (file && item.status === 'awaiting_upload') local.set(item.id, file); });
      this.files.set(batch.id, local);
      this.upsertBatch(batch);
      if (this.uploadOwner) this.resumeRequested.add(batch.id);
      else await this.uploadAwaiting(batch.id);
    } catch (error) { if (revision === this.revision) this.update({ error: message(error) }); }
  }

  async refreshBatch(id: string): Promise<void> {
    const revision = this.revision; const generation = (this.batchRequestGeneration.get(id) ?? 0) + 1;
    this.batchRequestGeneration.set(id, generation);
    try { const batch = await this.api.getBatch(id, this.controller.signal); if (revision === this.revision && generation === this.batchRequestGeneration.get(id)) this.upsertBatch(batch); }
    catch (error) { if (revision === this.revision && generation === this.batchRequestGeneration.get(id)) this.update({ error: message(error) }); }
  }

  async stop(id: string): Promise<void> {
    if (this.batchMutationOwners.has(id)) return;
    this.batchMutationOwners.add(id);
    this.stopRequested.add(id); this.update({ stoppedLocally: true, error: null });
    const revision = this.revision;
    try { await this.api.stopBatch(id, this.controller.signal); if (revision === this.revision) await this.refreshBatch(id); }
    catch (error) { if (revision === this.revision) this.update({ error: message(error) }); }
    finally { if (revision === this.revision) this.batchMutationOwners.delete(id); }
  }

  async resume(id: string): Promise<void> {
    if (this.batchMutationOwners.has(id)) return;
    this.batchMutationOwners.add(id);
    const revision = this.revision; this.update({ stoppedLocally: false, error: null });
    let shouldUpload = false;
    try {
      await this.api.resumeBatch(id, this.controller.signal);
      if (revision !== this.revision) return;
      this.stopRequested.delete(id); await this.refreshBatch(id);
      if (this.uploadOwner) this.resumeRequested.add(id); else shouldUpload = true;
    }
    catch (error) { if (revision === this.revision) this.update({ error: message(error) }); }
    finally { if (revision === this.revision) this.batchMutationOwners.delete(id); }
    if (shouldUpload && revision === this.revision) await this.uploadAwaiting(id);
  }

  async reselect(batchId: string, itemId: string, file: GpxFile): Promise<boolean> {
    const item = this.findItem(batchId, itemId);
    const selected = prepareImportSelection([file]);
    const candidate = selected.accepted[0];
    if (!item || item.status !== 'awaiting_upload' || !candidate || basename(candidate.name) !== item.name || candidate.kind !== item.format) {
      this.update({ error: 'Choose the exact file name and format shown for this item.' }); return false;
    }
    const local = this.files.get(batchId) ?? new Map<string, GpxFile>();
    local.set(itemId, candidate); this.files.set(batchId, local);
    this.update({ error: null });
    await this.uploadAwaiting(batchId, itemId);
    return true;
  }

  async retry(batchId: string, itemId: string): Promise<void> {
    const item = this.findItem(batchId, itemId);
    if (!item || item.status !== 'failed' || !item.source_id) return;
    this.update({ error: null });
    const revision = this.revision;
    try { await this.api.retryUpload(item.source_id, this.controller.signal); if (revision === this.revision) await this.refreshBatch(batchId); }
    catch (error) { if (revision === this.revision) this.update({ error: message(error) }); }
  }

  async pollForeground(): Promise<void> {
    if (this.polling || this.state.pollingStopped || !this.state.batches.some(hasActive)) return;
    if (this.state.polls >= 30) { this.update({ pollingStopped: true }); return; }
    this.polling = true; const owner = ++this.pollingGeneration; this.pollingOwner = owner; const revision = this.revision;
    this.update({ polls: this.state.polls + 1 });
    try {
      const active = this.state.batches.filter(hasActive);
      const start = active.length ? this.pollCursor % active.length : 0;
      const selected = [...active.slice(start), ...active.slice(0, start)].slice(0, 20);
      this.pollCursor = (start + selected.length) % Math.max(1, active.length);
      for (const batch of selected) { await this.refreshBatch(batch.id); if (revision !== this.revision) return; }
      if (this.state.polls >= 30 && this.state.batches.some(hasActive)) this.update({ pollingStopped: true });
    } finally { if (this.pollingOwner === owner) { this.polling = false; this.pollingOwner = null; } }
  }

  async refresh(): Promise<void> { this.update({ polls: 0, pollingStopped: false }); await this.loadPage(1); }

  private async uploadAwaiting(batchId: string, onlyItemId?: string): Promise<void> {
    if (this.uploadOwner) return;
    const batch = this.state.batches.find((entry) => entry.id === batchId);
    const local = this.files.get(batchId);
    if (!batch || batch.state !== 'open' || !local) return;
    const candidates = batch.items.filter((item) => item.status === 'awaiting_upload' && local.has(item.id) && (!onlyItemId || item.id === onlyItemId));
    if (!candidates.length) return;
    const owner = ++this.uploadGeneration; this.uploadOwner = { batchId, owner }; this.update({ uploadingBatchId: batchId, error: null });
    const revision = this.revision; let failed = false;
    try {
      await uploadSequentially(candidates, {
        canContinue: () => revision === this.revision && !this.stopRequested.has(batchId) && !failed,
        submit: async (item) => {
          const file = local.get(item.id); if (!file) return;
          if (revision === this.revision) this.update({ uploadingItemId: item.id });
          try {
            await this.api.uploadItem(batchId, item.id, file, this.controller.signal);
            local.delete(item.id);
            if (revision === this.revision) await this.refreshBatch(batchId);
          } catch (error) { failed = true; if (revision === this.revision) this.update({ error: message(error) }); }
        },
      });
    } finally {
      if (this.uploadOwner?.owner === owner) this.uploadOwner = null;
      if (revision === this.revision && this.state.uploadingBatchId === batchId) this.update({ uploadingBatchId: null, uploadingItemId: null });
      while (revision === this.revision && this.resumeRequested.size) {
        const [nextBatchId] = this.resumeRequested;
        if (!nextBatchId) break;
        this.resumeRequested.delete(nextBatchId);
        await this.refreshBatch(nextBatchId);
        await this.uploadAwaiting(nextBatchId);
      }
    }
  }

  private findItem(batchId: string, itemId: string): ImportBatchItemResponse | undefined { return this.state.batches.find((batch) => batch.id === batchId)?.items.find((item) => item.id === itemId); }
  private upsertBatch(batch: ImportBatchResponse): void {
    const prior = this.state.batches.find((item) => item.id === batch.id);
    const merged = prior ? mergeBatch(prior, batch) : batch;
    const newlySucceeded = this.newlySucceeded(prior, merged);
    this.update({ batches: prior ? this.state.batches.map((item) => item.id === batch.id ? merged : item) : [merged, ...this.state.batches], total: prior ? this.state.total : this.state.total + 1 });
    if (newlySucceeded) void this.onSucceeded();
  }

  private newlySucceeded(prior: ImportBatchResponse | undefined, next: ImportBatchResponse): boolean {
    return Boolean(prior && next.items.some((item) => item.status === 'succeeded' && prior.items.find((old) => old.id === item.id)?.status !== 'succeeded'));
  }
}

function hasActive(batch: ImportBatchResponse): boolean { return batch.items.some((item) => ACTIVE.has(item.status)); }
function message(error: unknown): string { return error instanceof Error ? error.message : 'Import request failed. Retry when connected.'; }
function mergeBatch(current: ImportBatchResponse, incoming: ImportBatchResponse): ImportBatchResponse {
  let changed = false;
  const items = incoming.items.map((item) => {
    const prior = current.items.find((candidate) => candidate.id === item.id);
    if (!prior) return item;
    const staleAwaiting = item.status === 'awaiting_upload' && prior.status !== 'awaiting_upload';
    if (prior.status === 'deleted' || staleAwaiting) { changed = true; return prior; }
    return item;
  });
  if (!changed) return incoming;
  const counts = { awaiting_upload: 0, queued: 0, processing: 0, succeeded: 0, failed: 0, deleted: 0 };
  for (const item of items) counts[item.status]++;
  return { ...incoming, items, counts };
}
