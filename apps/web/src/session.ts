import type { SessionStore } from '../../mobile/src/api/client';
import type { AuthSessionStore } from '../../mobile/src/auth/controller';

export const SESSION_KEY = 'city-runner.session';

/** Tab-scoped browser storage; all comparisons and writes happen synchronously. */
export class BrowserSessions implements SessionStore, AuthSessionStore {
  private listeners = new Set<(token: string | null) => void>();
  constructor(private readonly storage: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>) {}
  async getToken(): Promise<string | null> { return this.storage.getItem(SESSION_KEY); }
  async setToken(token: string): Promise<void> { this.write(token); }
  async setTokenIfEmpty(token: string): Promise<boolean> { if (this.storage.getItem(SESSION_KEY)) return false; this.write(token); return true; }
  async setTokenIfCurrent(token: string, isCurrent: () => boolean): Promise<boolean> {
    if (!isCurrent()) return false;
    this.write(token);
    if (!isCurrent()) { if (this.storage.getItem(SESSION_KEY) === token) this.write(null); return false; }
    return true;
  }
  async clear(): Promise<void> { this.write(null); }
  async clearIfCurrent(token: string): Promise<boolean> {
    if (this.storage.getItem(SESSION_KEY) !== token) return false;
    this.write(null); return true;
  }
  subscribe(listener: (token: string | null) => void): () => void { this.listeners.add(listener); return () => this.listeners.delete(listener); }
  private write(token: string | null): void {
    if (token === null) this.storage.removeItem(SESSION_KEY); else this.storage.setItem(SESSION_KEY, token);
    for (const listener of this.listeners) listener(token);
  }
}
