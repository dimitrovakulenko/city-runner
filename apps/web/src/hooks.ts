import { useCallback, useSyncExternalStore } from 'react';

export function useStore<T>(store: { getState(): T; subscribe(listener: (state: T) => void): () => void }): T {
  const subscribe = useCallback((notify: () => void) => store.subscribe(() => notify()), [store]);
  const snapshot = useCallback(() => store.getState(), [store]);
  return useSyncExternalStore(subscribe, snapshot);
}
