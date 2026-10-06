export interface SequentialUploadHooks<T> {
  canContinue(): boolean;
  submit(item: T): Promise<void>;
}

/** Runs only one item at a time and rechecks stop/account state between items. */
export async function uploadSequentially<T>(items: readonly T[], hooks: SequentialUploadHooks<T>): Promise<void> {
  for (const item of items) {
    if (!hooks.canContinue()) return;
    await hooks.submit(item);
  }
}
