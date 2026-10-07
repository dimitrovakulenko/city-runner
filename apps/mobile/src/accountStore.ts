import type { BinaryDownload } from './api/client';
import type { SessionStore } from './api/client';
import type { AccountApi } from './api/account';
import type { AccountDeletionResponse } from './api/generated';

export interface AccountState {
  exportStatus: 'idle' | 'loading' | 'ready' | 'error';
  exportError: string | null;
  deleteStatus: 'idle' | 'loading' | 'accepted' | 'error';
  deleteError: string | null;
  deletionReceipt: AccountDeletionResponse | null;
}

const INITIAL_STATE: AccountState = {
  exportStatus: 'idle', exportError: null, deleteStatus: 'idle', deleteError: null, deletionReceipt: null,
};

function message(error: unknown): string {
  return error instanceof Error ? error.message : 'Account request failed. Retry when connected.';
}

export class AccountStore {
  private state: AccountState = INITIAL_STATE;
  private accountGeneration = 0;
  private operationGeneration = 0;
  private activeOperation: number | null = null;
  private operationController: AbortController | null = null;
  private listeners = new Set<(state: AccountState) => void>();

  constructor(private readonly api: AccountApi, private readonly sessions: SessionStore) {}

  getState(): AccountState { return this.state; }
  getAccountGeneration(): number { return this.accountGeneration; }
  subscribe(listener: (state: AccountState) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private update(patch: Partial<AccountState>): void {
    this.state = { ...this.state, ...patch };
    for (const listener of this.listeners) listener(this.state);
  }

  reset(): void {
    this.accountGeneration++;
    this.operationGeneration++;
    this.operationController?.abort();
    this.operationController = null;
    this.activeOperation = null;
    this.update({ ...INITIAL_STATE });
  }

  async exportAccount(): Promise<BinaryDownload | null> {
    if (this.activeOperation !== null) return null;
    const generation = this.accountGeneration;
    const owner = ++this.operationGeneration;
    const controller = new AbortController();
    this.operationController = controller;
    this.activeOperation = owner;
    this.update({ exportStatus: 'loading', exportError: null, deleteStatus: 'idle', deleteError: null, deletionReceipt: null });
    try {
      const result = await this.api.exportData(controller.signal);
      if (!this.current(generation, owner)) return null;
      this.update({ exportStatus: 'ready' });
      return result;
    } catch (error) {
      if (!this.current(generation, owner)) return null;
      this.update({ exportStatus: 'error', exportError: message(error) });
      return null;
    } finally {
      if (this.activeOperation === owner) this.activeOperation = null;
      if (this.operationController === controller) this.operationController = null;
    }
  }

  async deleteAccount(confirmation: string): Promise<AccountDeletionResponse | null> {
    if (this.activeOperation !== null) return null;
    if (confirmation !== 'DELETE') {
      this.update({ deleteStatus: 'error', deleteError: 'Type DELETE to confirm account removal.' });
      return null;
    }
    const generation = this.accountGeneration;
    const owner = ++this.operationGeneration;
    const controller = new AbortController();
    this.operationController = controller;
    this.activeOperation = owner;
    this.update({ deleteStatus: 'loading', deleteError: null, deletionReceipt: null, exportStatus: 'idle', exportError: null });
    try {
      const token = await this.sessions.getToken();
      if (!this.current(generation, owner)) return null;
      if (!token) { this.update({ deleteStatus: 'error', deleteError: 'Sign in before deleting this account.' }); return null; }
      const receipt = await this.api.deleteAccount({ confirmation }, token, controller.signal);
      if (!this.current(generation, owner)) return null;
      let currentToken: string | null;
      try { currentToken = await this.sessions.getToken(); }
      catch {
        if (this.current(generation, owner)) this.update({ deleteStatus: 'error', deleteError: 'Account deletion was accepted, but this device could not verify the captured session. Check account status before retrying.' });
        return null;
      }
      if (currentToken !== token || !this.current(generation, owner)) return null;
      let cleared = false;
      try { cleared = await this.sessions.clearIfCurrent(token); }
      catch {
        if (this.current(generation, owner)) this.update({ deleteStatus: 'error', deleteError: 'Account deletion was accepted, but this device could not clear the captured session. Sign out before continuing.' });
        return null;
      }
      if (!cleared) {
        if (this.current(generation, owner)) this.update({ deleteStatus: 'error', deleteError: 'Account deletion was accepted, but the session changed. The replacement session was preserved.' });
        return null;
      }
      let tokenAfter: string | null;
      try { tokenAfter = await this.sessions.getToken(); }
      catch { return null; }
      const resetCount = this.accountGeneration - generation;
      if (tokenAfter !== null || resetCount < 0 || resetCount > 1) return null;
      if (resetCount === 0 && this.current(generation, owner)) this.update({ deleteStatus: 'accepted', deletionReceipt: receipt });
      return receipt;
    } catch (error) {
      if (!this.current(generation, owner)) return null;
      this.update({ deleteStatus: 'error', deleteError: message(error) });
      return null;
    } finally {
      if (this.activeOperation === owner) this.activeOperation = null;
      if (this.operationController === controller) this.operationController = null;
    }
  }

  private current(generation: number, owner: number): boolean {
    return generation === this.accountGeneration && owner === this.operationGeneration && this.activeOperation === owner;
  }
}
