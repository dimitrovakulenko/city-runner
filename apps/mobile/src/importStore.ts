import type { ImportBatchCreate, ImportBatchItemResponse, ImportBatchPage, ImportBatchResponse, ImportHistoryCreate, ImportHistoryState } from './api/generated';
import type { ImportBatchApi } from './api/importBatches';
import type { HistoryImportApi } from './api/importHistories';
import type { GpxFile } from './api/explore';
import { uploadSequentially } from './importQueue';
import { prepareImportSelection } from './importSelection';
import { historyBasename, prepareHistoryImportSelection } from './historyImportSelection';
import type { PreparedHistoryFile } from './historyImportSelection';

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
  histories: ImportHistoryState[];
  historyPage: number;
  historyTotal: number;
  historiesLoading: boolean;
  historyError: string | null;
  pendingHistoryCreation: boolean;
  historyBatches: Record<string, ImportBatchResponse[]>;
  historyBatchesLoading: Record<string, boolean>;
  historyBatchesErrors: Record<string, string | null>;
}

const INITIAL: ImportState = { batches: [], page: 0, total: 0, loading: false, uploadingBatchId: null, uploadingItemId: null,
  stoppedLocally: false, polls: 0, pollingStopped: false, error: null, rejected: [], histories: [], historyPage: 0,
  historyTotal: 0, historiesLoading: false, historyError: null, pendingHistoryCreation: false, historyBatches: {},
  historyBatchesLoading: {}, historyBatchesErrors: {} };
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
  private historyRequestGeneration = 0;
  private historyBatchRequests = new Map<string, number>();
  private historyMutationOwners = new Set<string>();
  private historyStopRequested = new Set<string>();
  private batchHistoryIds = new Map<string, string>();
  private pendingHistory: { body: ImportHistoryCreate; files: PreparedHistoryFile[]; revision: number } | null = null;
  private historyDetailRequests = new Map<string, number>();
  private historyPollCursor = 0;

  constructor(private readonly api: ImportBatchApi, private readonly randomUUID: () => string,
    private readonly onSucceeded: () => void | Promise<void> = () => {},
    private readonly historyApi?: HistoryImportApi,
    private readonly onFileReleased: (uri: string) => void = () => {}) {}

  getState(): ImportState { return this.state; }
  getAccountGeneration(): number { return this.revision; }
  hasSelectedFile(batchId: string, itemId: string): boolean { return this.files.get(batchId)?.has(itemId) ?? false; }
  hasPendingHistoryCreation(): boolean { return this.pendingHistory !== null && !this.state.pendingHistoryCreation; }
  retainsFile(uri: string): boolean {
    if (this.pendingHistory?.files.some((file) => file.uri === uri)) return true;
    for (const files of this.files.values()) for (const file of files.values()) if (file.uri === uri) return true;
    return false;
  }
  setError(error: string): void { this.update({ error }); }
  subscribe(listener: (state: ImportState) => void): () => void { this.listeners.add(listener); return () => this.listeners.delete(listener); }
  private update(patch: Partial<ImportState>): void { this.state = { ...this.state, ...patch }; for (const listener of this.listeners) listener(this.state); }

  reset(): void {
    this.revision++;
    this.controller.abort(); this.controller = new AbortController();
    this.uploadOwner = null; this.stopRequested.clear(); this.resumeRequested.clear(); this.batchMutationOwners.clear(); this.files.clear(); this.polling = false; this.pollingOwner = null;
    this.batchRequestGeneration.clear();
    this.historyRequestGeneration++;
    this.historyBatchRequests.clear(); this.historyDetailRequests.clear(); this.historyMutationOwners.clear(); this.historyStopRequested.clear(); this.batchHistoryIds.clear(); this.pendingHistory = null; this.historyPollCursor = 0;
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

  async loadHistories(page = 1): Promise<boolean> {
    if (!this.historyApi || !Number.isInteger(page) || page < 1) return false;
    const revision = this.revision; const request = ++this.historyRequestGeneration;
    const startedIds = new Set(this.state.histories.map((history) => history.id));
    const detailGenerations = new Map<string, number>();
    for (const history of this.state.histories) {
      const generation = (this.historyDetailRequests.get(history.id) ?? 0) + 1;
      this.historyDetailRequests.set(history.id, generation);
      detailGenerations.set(history.id, generation);
    }
    this.update({ historiesLoading: true, historyError: null });
    try {
      const result = await this.historyApi.listHistories(page, this.controller.signal);
      if (revision !== this.revision || request !== this.historyRequestGeneration) return false;
      if (result.page !== page || result.page_size !== 20 || !Number.isInteger(result.total) || result.total < 0) {
        throw new Error('The history list response was invalid. Retry when connected.');
      }
      const byId = new Map(this.state.histories.map((history) => [history.id, history]));
      for (const history of result.items) {
        if ((this.historyDetailRequests.get(history.id) ?? 0) > (detailGenerations.get(history.id) ?? 0)) continue;
        byId.set(history.id, history);
      }
      const arrivedWhileLoading = this.state.histories.filter((history) => !startedIds.has(history.id) && !result.items.some((item) => item.id === history.id));
      const histories = page === 1
        ? [...arrivedWhileLoading, ...result.items.map((history) => byId.get(history.id) ?? history)]
        : [...this.state.histories, ...result.items.filter((history) => !this.state.histories.some((current) => current.id === history.id))];
      const total = Math.max(result.total + arrivedWhileLoading.length, histories.length);
      this.update({ histories, historyPage: result.page, historyTotal: total, historiesLoading: false });
      return true;
    } catch (error) {
      if (revision !== this.revision || request !== this.historyRequestGeneration) return false;
      this.update({ historiesLoading: false, historyError: message(error) });
      return false;
    }
  }

  async loadMoreHistories(): Promise<boolean> {
    if (this.state.historiesLoading || this.state.histories.length >= this.state.historyTotal) return false;
    return this.loadHistories(this.state.historyPage + 1);
  }

  async loadHistoryBatches(id: string): Promise<boolean> {
    if (!this.historyApi) return false;
    const summary = this.state.histories.find((history) => history.id === id);
    if (!summary) { this.update({ historyBatchesErrors: { ...this.state.historyBatchesErrors, [id]: 'Import history not found. Refresh and retry.' } }); return false; }
    const revision = this.revision; const request = (this.historyBatchRequests.get(id) ?? 0) + 1;
    this.historyBatchRequests.set(id, request);
    this.update({ historyBatchesLoading: { ...this.state.historyBatchesLoading, [id]: true }, historyBatchesErrors: { ...this.state.historyBatchesErrors, [id]: null } });
    try {
      if (summary.batch_ids.length > 50) throw new Error('This history has too many child batches.');
      const pages = Math.ceil(summary.batch_ids.length / 20);
      const batches: ImportBatchResponse[] = [];
      for (let page = 1; page <= pages; page++) {
        const result = await this.historyApi.listHistoryBatches(id, page, this.controller.signal);
        if (revision !== this.revision || request !== this.historyBatchRequests.get(id)) return false;
        const expectedIds = summary.batch_ids.slice((page - 1) * 20, page * 20);
        if (result.page !== page || result.page_size !== 20 || result.total !== summary.batch_ids.length ||
            result.items.length !== expectedIds.length || result.items.some((batch, index) => batch.id !== expectedIds[index] || (batch.history_id != null && batch.history_id !== id))) {
          throw new Error('The history files response was invalid. Refresh and retry.');
        }
        batches.push(...result.items);
      }
      if (revision !== this.revision || request !== this.historyBatchRequests.get(id)) return false;
      const priorBatches = this.state.historyBatches[id] ?? [];
      const merged = batches.map((batch) => {
        this.batchHistoryIds.set(batch.id, id);
        const prior = priorBatches.find((item) => item.id === batch.id);
        const next = prior ? mergeBatch(prior, batch) : { ...batch, history_id: id };
        this.pruneResolvedFiles(batch.id, next);
        return next;
      });
      this.update({ historyBatches: { ...this.state.historyBatches, [id]: merged },
        historyBatchesLoading: { ...this.state.historyBatchesLoading, [id]: false } });
      if (merged.some((batch) => this.newlySucceeded(priorBatches.find((item) => item.id === batch.id), batch))) void this.onSucceeded();
      return true;
    } catch (error) {
      if (revision !== this.revision || request !== this.historyBatchRequests.get(id)) return false;
      this.update({ historyBatchesLoading: { ...this.state.historyBatchesLoading, [id]: false },
        historyBatchesErrors: { ...this.state.historyBatchesErrors, [id]: message(error) } });
      return false;
    }
  }

  async refreshHistory(id: string): Promise<boolean> {
    if (!this.historyApi) return false;
    const revision = this.revision; const request = (this.historyDetailRequests.get(id) ?? 0) + 1;
    this.historyDetailRequests.set(id, request);
    try {
      const history = await this.historyApi.getHistory(id, this.controller.signal);
      if (revision !== this.revision || request !== this.historyDetailRequests.get(id)) return false;
      this.upsertHistory(history);
      return true;
    } catch (error) { if (revision === this.revision && request === this.historyDetailRequests.get(id)) this.update({ historyError: message(error) }); return false; }
  }

  async createHistoryFromSelection(label: string, files: PreparedHistoryFile[]): Promise<void> {
    if (!this.historyApi || this.pendingHistory) { if (!this.historyApi) this.update({ historyError: 'Full-history imports are unavailable.' }); return; }
    const selection = prepareHistoryImportSelection(files);
    const normalizedLabel = label.trim();
    const rejected = [...selection.rejected];
    if (!normalizedLabel || Array.from(normalizedLabel).length > 200) {
      this.update({ rejected, historyError: 'History names must be between 1 and 200 characters.', pendingHistoryCreation: false }); return;
    }
    if (!selection.accepted.length) {
      this.update({ rejected, historyError: rejected[0]?.reason ?? 'Choose at least one GPX or FIT activity file.', pendingHistoryCreation: false }); return;
    }
    const pending = { revision: this.revision, files: selection.accepted, body: {
      request_id: this.randomUUID(), label: normalizedLabel,
      files: selection.accepted.map((file) => ({ name: historyBasename(file.name), format: file.kind, size: file.size, sha256: file.sha256 })),
    } satisfies ImportHistoryCreate };
    this.pendingHistory = pending;
    this.update({ rejected, historyError: null, pendingHistoryCreation: true });
    await this.submitPendingHistory(pending);
  }

  async retryHistoryCreation(): Promise<void> {
    const pending = this.pendingHistory;
    if (!pending || pending.revision !== this.revision || this.state.pendingHistoryCreation) return;
    this.update({ pendingHistoryCreation: true, historyError: null });
    await this.submitPendingHistory(pending);
  }

  private async submitPendingHistory(pending: NonNullable<ImportStore['pendingHistory']>): Promise<void> {
    if (!this.historyApi || this.pendingHistory !== pending || pending.revision !== this.revision) return;
    try {
      const history = await this.historyApi.createHistory(pending.body, this.controller.signal);
      if (pending.revision !== this.revision || this.pendingHistory !== pending) return;
      if (history.file_count !== pending.files.length || history.batch_ids.length > 50) throw new Error('The created history manifest did not match the selected files.');
      this.upsertHistory(history);
      if (!await this.loadHistoryBatches(history.id)) throw new Error(this.state.historyBatchesErrors[history.id] ?? 'Could not load the created history files. Retry the same request.');
      if (pending.revision !== this.revision || this.pendingHistory !== pending) return;
      const batches = this.state.historyBatches[history.id] ?? [];
      const items = batches.flatMap((batch) => batch.items);
      if (items.length !== pending.files.length) throw new Error('The created history manifest did not match the selected files.');
      const localByBatch = new Map<string, Map<string, GpxFile>>();
      let fileIndex = 0;
      for (const batch of batches) {
        for (const item of batch.items) {
          const file = pending.files[fileIndex++];
          if (!file || item.name !== historyBasename(file.name) || item.format !== file.kind || item.expected_size !== file.size || item.expected_sha256 !== file.sha256) {
            throw new Error('The created history files did not match the selected bytes. Retry this history creation.');
          }
          if (item.status === 'awaiting_upload') {
            const local = localByBatch.get(batch.id) ?? new Map<string, GpxFile>();
            local.set(item.id, file); localByBatch.set(batch.id, local);
          }
        }
      }
      for (const [batchId, local] of localByBatch) this.files.set(batchId, local);
      this.pendingHistory = null;
      this.update({ pendingHistoryCreation: false, historyError: null });
      const uploadable = batches.filter((batch) => localByBatch.has(batch.id));
      for (const batch of uploadable.slice(1)) this.resumeRequested.add(batch.id);
      if (uploadable.length) {
        if (this.uploadOwner) this.resumeRequested.add(uploadable[0]!.id);
        else await this.uploadAwaiting(uploadable[0]!.id);
      }
    } catch (error) {
      if (pending.revision === this.revision && this.pendingHistory === pending) this.update({ pendingHistoryCreation: false, historyError: message(error) });
    }
  }

  async reselectHistory(id: string, files: PreparedHistoryFile[]): Promise<boolean> {
    const history = this.state.histories.find((item) => item.id === id);
    if (!history || !this.historyApi) { this.update({ historyError: 'Import history not found. Refresh and retry.' }); return false; }
    const revision = this.revision;
    const selection = prepareHistoryImportSelection(files);
    if (!selection.accepted.length) { this.update({ rejected: selection.rejected, historyError: selection.rejected[0]?.reason ?? 'Choose at least one GPX or FIT activity file.' }); return false; }
    if (!await this.loadHistoryBatches(id) || revision !== this.revision) return false;
    const batches = this.state.historyBatches[id] ?? [];
    const selected = selection.accepted;
    const localByBatch = new Map<string, Map<string, GpxFile>>();
    let matched = 0;
    const rejected = [...selection.rejected];
    for (const batch of batches) {
      for (const item of batch.items) {
        if (item.status !== 'awaiting_upload' || item.expected_sha256 == null || item.expected_size == null) continue;
        const candidate = selected.find((file) => historyBasename(file.name) === item.name && file.kind === item.format &&
          file.size === item.expected_size && file.sha256 === item.expected_sha256);
        if (!candidate) continue;
        const local = localByBatch.get(batch.id) ?? new Map<string, GpxFile>();
        local.set(item.id, candidate); localByBatch.set(batch.id, local); matched++;
      }
    }
    for (const file of selected) {
      const sameName = batches.some((batch) => batch.items.some((item) => item.status === 'awaiting_upload' && item.name === historyBasename(file.name) && item.format === file.kind));
      const exact = batches.some((batch) => batch.items.some((item) => item.status === 'awaiting_upload' && item.name === historyBasename(file.name) && item.format === file.kind && item.expected_size === file.size && item.expected_sha256 === file.sha256));
      if (sameName && !exact) rejected.push({ name: historyBasename(file.name), reason: 'This file does not match the selected history item. Choose the original bytes.' });
    }
    if (revision !== this.revision) return false;
    const replacedUris: string[] = [];
    for (const [batchId, local] of localByBatch) {
      const retained = this.files.get(batchId) ?? new Map<string, GpxFile>();
      for (const [itemId, file] of local) {
        const previous = retained.get(itemId);
        if (previous && previous.uri !== file.uri) replacedUris.push(previous.uri);
        retained.set(itemId, file);
      }
      this.files.set(batchId, retained);
    }
    for (const uri of replacedUris) this.releaseIfUnused(uri);
    this.update({ rejected, historyError: matched ? null : 'No awaiting history items matched these file names, formats, sizes and hashes.' });
    if (history.state === 'open') {
      const uploadable = batches.filter((batch) => localByBatch.has(batch.id));
      for (const batch of uploadable.slice(1)) this.resumeRequested.add(batch.id);
      if (uploadable.length) {
        if (this.uploadOwner) this.resumeRequested.add(uploadable[0]!.id);
        else await this.uploadAwaiting(uploadable[0]!.id);
      }
    }
    return matched > 0;
  }

  async stopHistory(id: string): Promise<void> {
    if (!this.historyApi || this.historyMutationOwners.has(id)) return;
    const history = this.state.histories.find((item) => item.id === id);
    if (!history) return;
    const revision = this.revision;
    this.historyMutationOwners.add(id);
    this.historyDetailRequests.set(id, (this.historyDetailRequests.get(id) ?? 0) + 1);
    this.historyStopRequested.add(id);
    const childIds = new Set(history.batch_ids);
    for (const batchId of childIds) { this.stopRequested.add(batchId); this.resumeRequested.delete(batchId); }
    this.update({ histories: this.state.histories.map((item) => item.id === id ? { ...item, state: 'stopped' } : item), error: null, historyError: null });
    try {
      await this.historyApi.stopHistory(id, this.controller.signal);
      if (revision !== this.revision) return;
      await this.refreshHistory(id);
      await this.loadHistoryBatches(id);
      if (revision === this.revision) this.update({ error: null, historyError: null });
    } catch (error) { if (revision === this.revision) this.update({ historyError: message(error) }); }
    finally { if (revision === this.revision) this.historyMutationOwners.delete(id); }
  }

  async resumeHistory(id: string): Promise<void> {
    if (!this.historyApi || this.historyMutationOwners.has(id)) return;
    const history = this.state.histories.find((item) => item.id === id);
    if (!history) return;
    const revision = this.revision;
    this.historyMutationOwners.add(id);
    this.historyDetailRequests.set(id, (this.historyDetailRequests.get(id) ?? 0) + 1);
    let ownsMutation = true;
    try {
      await this.historyApi.resumeHistory(id, this.controller.signal);
      if (revision !== this.revision) return;
      this.historyStopRequested.delete(id);
      for (const batchId of history.batch_ids) this.stopRequested.delete(batchId);
      this.update({ histories: this.state.histories.map((item) => item.id === id ? { ...item, state: 'open' } : item) });
      await this.refreshHistory(id);
      if (!await this.loadHistoryBatches(id) || revision !== this.revision) return;
      this.historyMutationOwners.delete(id); ownsMutation = false;
      await this.queueHistoryUploads(id);
    } catch (error) { if (revision === this.revision) this.update({ historyError: message(error) }); }
    finally { if (revision === this.revision && ownsMutation) this.historyMutationOwners.delete(id); }
  }

  private async queueHistoryUploads(id: string): Promise<void> {
    if (this.historyStopRequested.has(id)) return;
    const batches = this.state.historyBatches[id] ?? [];
    const history = this.state.histories.find((item) => item.id === id);
    if (!history || history.state !== 'open') return;
    const uploadable = batches.filter((batch) => batch.items.some((item) => item.status === 'awaiting_upload' && this.files.get(batch.id)?.has(item.id)));
    for (const batch of uploadable.slice(1)) this.resumeRequested.add(batch.id);
    if (!uploadable.length) return;
    if (this.uploadOwner) this.resumeRequested.add(uploadable[0]!.id);
    else await this.uploadAwaiting(uploadable[0]!.id);
  }

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
    const activeHistories = this.state.histories.filter(historyHasQueuedWork);
    if (this.polling || this.state.pollingStopped || (!this.allBatches().some(hasActive) && !activeHistories.length)) return;
    if (this.state.polls >= 30) { this.update({ pollingStopped: true }); return; }
    this.polling = true; const owner = ++this.pollingGeneration; this.pollingOwner = owner; const revision = this.revision;
    this.update({ polls: this.state.polls + 1 });
    try {
      const active = this.allBatches().filter(hasActive);
      const start = active.length ? this.pollCursor % active.length : 0;
      const selected = [...active.slice(start), ...active.slice(0, start)].slice(0, 20);
      this.pollCursor = (start + selected.length) % Math.max(1, active.length);
      for (const batch of selected) { await this.refreshBatch(batch.id); if (revision !== this.revision) return; }
      const histories = this.state.histories.filter((history) => historyHasQueuedWork(history) || history.batch_ids.some((id) => selected.some((batch) => batch.id === id)));
      const historyStart = histories.length ? this.historyPollCursor % histories.length : 0;
      const selectedHistories = [...histories.slice(historyStart), ...histories.slice(0, historyStart)].slice(0, 20);
      this.historyPollCursor = (historyStart + selectedHistories.length) % Math.max(1, histories.length);
      for (const history of selectedHistories) { await this.refreshHistory(history.id); if (revision !== this.revision) return; }
      if (this.state.polls >= 30 && !this.allBatches().some(hasActive) && !this.state.histories.some(historyHasQueuedWork)) this.update({ pollingStopped: true });
    } finally { if (this.pollingOwner === owner) { this.polling = false; this.pollingOwner = null; } }
  }

  async refresh(): Promise<void> {
    this.update({ polls: 0, pollingStopped: false });
    await this.loadPage(1);
    if (this.historyApi) await this.loadHistories(1);
  }

  private async uploadAwaiting(batchId: string, onlyItemId?: string): Promise<void> {
    if (this.uploadOwner) return;
    const batch = this.getBatchCached(batchId);
    const local = this.files.get(batchId);
    const historyId = batch ? (batch.history_id ?? this.batchHistoryIds.get(batchId)) : undefined;
    if (!batch || batch.state !== 'open' || !local || (historyId !== undefined && this.historyStopRequested.has(historyId))) return;
    const candidates = batch.items.filter((item) => item.status === 'awaiting_upload' && local.has(item.id) && (!onlyItemId || item.id === onlyItemId));
    if (!candidates.length) return;
    const owner = ++this.uploadGeneration; this.uploadOwner = { batchId, owner }; this.update({ uploadingBatchId: batchId, error: null });
    const revision = this.revision; let failed = false;
    try {
      await uploadSequentially(candidates, {
        canContinue: () => revision === this.revision && !this.stopRequested.has(batchId) && !(historyId && this.historyStopRequested.has(historyId)) && !failed,
        submit: async (item) => {
          const file = local.get(item.id); if (!file) return;
          if (revision === this.revision) this.update({ uploadingItemId: item.id });
          try {
            await this.api.uploadItem(batchId, item.id, file, this.controller.signal);
            local.delete(item.id);
            this.releaseIfUnused(file.uri);
            if (revision === this.revision) await this.refreshBatch(batchId);
          } catch (error) { failed = true; if (revision === this.revision) this.update({ error: message(error) }); }
        },
      });
    } finally {
      if (this.uploadOwner?.owner === owner) this.uploadOwner = null;
      if (revision === this.revision && this.state.uploadingBatchId === batchId) this.update({ uploadingBatchId: null, uploadingItemId: null });
      if (failed && historyId) {
        if (revision === this.revision) {
          for (const childId of this.state.histories.find((history) => history.id === historyId)?.batch_ids ?? []) this.resumeRequested.delete(childId);
          this.update({ historyError: 'Upload paused after a file failed. Reselect the file to retry.' });
        }
      }
      if (historyId && revision === this.revision) await this.refreshHistory(historyId);
      while (revision === this.revision && this.resumeRequested.size) {
        const [nextBatchId] = this.resumeRequested;
        if (!nextBatchId) break;
        this.resumeRequested.delete(nextBatchId);
        await this.refreshBatch(nextBatchId);
        await this.uploadAwaiting(nextBatchId);
      }
    }
  }

  private findItem(batchId: string, itemId: string): ImportBatchItemResponse | undefined { return this.getBatchCached(batchId)?.items.find((item) => item.id === itemId); }
  private getBatchCached(batchId: string): ImportBatchResponse | undefined {
    return this.state.batches.find((batch) => batch.id === batchId) ?? Object.values(this.state.historyBatches).flat().find((batch) => batch.id === batchId);
  }
  private allBatches(): ImportBatchResponse[] {
    const byId = new Map<string, ImportBatchResponse>();
    for (const batch of this.state.batches) byId.set(batch.id, batch);
    for (const batches of Object.values(this.state.historyBatches)) for (const batch of batches) byId.set(batch.id, batch);
    return [...byId.values()];
  }
  private upsertHistory(history: ImportHistoryState): void {
    const existing = this.state.histories.some((item) => item.id === history.id);
    this.update({ histories: existing ? this.state.histories.map((item) => item.id === history.id ? history : item) : [history, ...this.state.histories],
      historyTotal: existing ? this.state.historyTotal : this.state.historyTotal + 1 });
  }
  private upsertBatch(batch: ImportBatchResponse): void {
    const historyId = batch.history_id ?? this.batchHistoryIds.get(batch.id);
    if (historyId) {
      this.batchHistoryIds.set(batch.id, historyId);
      const group = this.state.historyBatches[historyId] ?? [];
      const prior = group.find((item) => item.id === batch.id);
      const merged = prior ? mergeBatch(prior, batch) : { ...batch, history_id: historyId };
      this.pruneResolvedFiles(batch.id, merged);
      this.update({ historyBatches: { ...this.state.historyBatches,
        [historyId]: prior ? group.map((item) => item.id === batch.id ? merged : item) : [...group, merged] } });
      if (this.newlySucceeded(prior, merged)) void this.onSucceeded();
      return;
    }
    const prior = this.state.batches.find((item) => item.id === batch.id);
    const merged = prior ? mergeBatch(prior, batch) : batch;
    this.pruneResolvedFiles(batch.id, merged);
    const newlySucceeded = this.newlySucceeded(prior, merged);
    this.update({ batches: prior ? this.state.batches.map((item) => item.id === batch.id ? merged : item) : [merged, ...this.state.batches], total: prior ? this.state.total : this.state.total + 1 });
    if (newlySucceeded) void this.onSucceeded();
  }

  private newlySucceeded(prior: ImportBatchResponse | undefined, next: ImportBatchResponse): boolean {
    return Boolean(prior && next.items.some((item) => item.status === 'succeeded' && prior.items.find((old) => old.id === item.id)?.status !== 'succeeded'));
  }

  private pruneResolvedFiles(batchId: string, batch: ImportBatchResponse): void {
    const local = this.files.get(batchId);
    if (!local) return;
    const released: string[] = [];
    for (const item of batch.items) {
      if (item.status !== 'queued' && item.status !== 'processing' && item.status !== 'succeeded' && item.status !== 'deleted' && !(item.status === 'failed' && item.source_id !== null)) continue;
      const file = local.get(item.id);
      if (file) released.push(file.uri);
      local.delete(item.id);
    }
    if (local.size === 0) this.files.delete(batchId);
    for (const uri of released) this.releaseIfUnused(uri);
  }

  private releaseIfUnused(uri: string): void {
    if (!this.retainsFile(uri)) this.onFileReleased(uri);
  }
}

function hasActive(batch: ImportBatchResponse): boolean { return batch.items.some((item) => ACTIVE.has(item.status)); }
function historyHasQueuedWork(history: ImportHistoryState): boolean { return history.counts.queued > 0 || history.counts.processing > 0; }
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
