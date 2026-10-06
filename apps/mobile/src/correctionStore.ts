import type { CorrectionApi } from './api/corrections';

export type CorrectionOperation = 'activity-delete' | 'manual-complete' | 'manual-undo';
export interface CorrectionState { pending: CorrectionOperation | null; error: string | null; message: string | null }

export class CorrectionStore {
  private state: CorrectionState = { pending: null, error: null, message: null };
  private listeners = new Set<(state: CorrectionState) => void>();
  private revision = 0;
  private accountRevision = 0;
  private controller: AbortController | null = null;

  constructor(private readonly api: CorrectionApi, private readonly onSuccess: (operation: CorrectionOperation, id: string) => void) {}
  getState(): CorrectionState { return this.state; }
  subscribe(listener: (state: CorrectionState) => void): () => void { this.listeners.add(listener); return () => this.listeners.delete(listener); }
  private update(state: CorrectionState): void { this.state = state; for (const listener of this.listeners) listener(state); }

  clearFeedback(): void {
    this.revision++;
    this.update({ ...this.state, error: null, message: null });
  }

  reset(): void {
    this.revision++;
    this.accountRevision++;
    this.controller?.abort();
    this.controller = null;
    this.update({ pending: null, error: null, message: null });
  }

  deleteActivity(id: string): Promise<void> { return this.run('activity-delete', id, (signal) => this.api.deleteActivity(id, signal)); }
  markComplete(streetId: string, datasetId: string, reason: string): Promise<void> {
    const normalized = reason.trim();
    if (normalized.length < 1 || normalized.length > 500) {
      this.update({ pending: null, error: 'Enter a reason between 1 and 500 characters.', message: null });
      return Promise.resolve();
    }
    return this.run('manual-complete', streetId, (signal) => this.api.setManualCompletion(streetId, datasetId, normalized, signal));
  }
  undoManualCompletion(streetId: string, datasetId: string): Promise<void> {
    return this.run('manual-undo', streetId, (signal) => this.api.clearManualCompletion(streetId, datasetId, signal));
  }

  private async run(operation: CorrectionOperation, id: string, action: (signal: AbortSignal) => Promise<void>): Promise<void> {
    if (this.controller) return;
    const revision = ++this.revision;
    const accountRevision = this.accountRevision;
    const controller = new AbortController();
    this.controller = controller;
    this.update({ pending: operation, error: null, message: null });
    try {
      await action(controller.signal);
      if (accountRevision !== this.accountRevision || controller.signal.aborted) return;
      this.onSuccess(operation, id);
      this.update({ pending: null, error: null, message: revision === this.revision ? operation === 'activity-delete'
        ? 'Activity deleted. Its private source file cleanup is queued.' : operation === 'manual-complete'
          ? 'Manual completion saved. GPS visits and remaining nodes are unchanged.' : 'Manual completion removed.' : null });
    } catch (error) {
      if (accountRevision !== this.accountRevision || controller.signal.aborted) return;
      this.update({ pending: null, error: revision === this.revision ? error instanceof Error ? error.message : 'Could not save this change. Retry.' : null, message: null });
    } finally {
      if (this.controller === controller) this.controller = null;
    }
  }
}
