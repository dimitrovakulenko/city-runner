/** A serialized adapter around Expo SecureStore's small async key/value API. */

export interface SecureStoreModule {
  getItemAsync(key: string): Promise<string | null>;
  setItemAsync(key: string, value: string): Promise<void>;
  deleteItemAsync(key: string): Promise<void>;
}

export interface SessionStore {
  getToken(): Promise<string | null>;
  setToken(token: string): Promise<void>;
  clear(): Promise<void>;
  clearIfCurrent(token: string): Promise<boolean>;
}

export interface SecureSessionStore extends SessionStore {
  /** Commit only while the owning auth attempt is still current. */
  setTokenIfCurrent(token: string, isCurrent: () => boolean): Promise<boolean>;
  subscribe(listener: (token: string | null) => void): () => void;
}

export const SESSION_TOKEN_KEY = 'city-runner.session-token';

/**
 * Serialize reads, writes, and compare-and-clear so a late 401 cannot erase a
 * newer login stored by another request in the same JS runtime.
 */
export function createSecureSessionStore(
  secureStore: SecureStoreModule,
  key = SESSION_TOKEN_KEY,
): SecureSessionStore {
  let tail: Promise<void> = Promise.resolve();
  const listeners = new Set<(token: string | null) => void>();

  function notify(token: string | null): void {
    for (const listener of listeners) {
      try { listener(token); } catch { /* A view listener cannot break session persistence. */ }
    }
  }

  function run<T>(operation: () => Promise<T>): Promise<T> {
    const result = tail.then(operation);
    tail = result.then(() => undefined, () => undefined);
    return result;
  }

  return {
    getToken() {
      return run(() => secureStore.getItemAsync(key));
    },
    setToken(token) {
      if (typeof token !== 'string' || token.length === 0 || token.length > 4096) {
        return Promise.reject(new Error('Invalid session token.'));
      }
      return run(async () => {
        await secureStore.setItemAsync(key, token);
        notify(token);
      });
    },
    setTokenIfCurrent(token, isCurrent) {
      if (typeof token !== 'string' || token.length === 0 || token.length > 4096) {
        return Promise.reject(new Error('Invalid session token.'));
      }
      return run(async () => {
        if (!isCurrent()) return false;
        await secureStore.setItemAsync(key, token);
        if (isCurrent()) {
          notify(token);
          return true;
        }
        if (await secureStore.getItemAsync(key) === token) await secureStore.deleteItemAsync(key);
        notify(null);
        return false;
      });
    },
    clear() {
      return run(async () => {
        await secureStore.deleteItemAsync(key);
        notify(null);
      });
    },
    clearIfCurrent(token) {
      return run(async () => {
        if (await secureStore.getItemAsync(key) !== token) return false;
        await secureStore.deleteItemAsync(key);
        notify(null);
        return true;
      });
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}
