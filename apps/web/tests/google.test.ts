import test from 'node:test';
import assert from 'node:assert/strict';
import { browserGoogle } from '../src/google';

function browser(google: unknown, container: unknown) {
  const previousWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
  const previousDocument = Object.getOwnPropertyDescriptor(globalThis, 'document');
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { google } });
  Object.defineProperty(globalThis, 'document', { configurable: true, value: { getElementById: () => container } });
  return () => {
    for (const [key, previous] of [['window', previousWindow], ['document', previousDocument]] as const) {
      if (previous) Object.defineProperty(globalThis, key, previous); else Reflect.deleteProperty(globalThis, key);
    }
  };
}

test('Google browser adapter preserves the exact server nonce and returns only the credential', async () => {
  let removed = 0; let options: { nonce: string; client_id: string; auto_select: boolean; callback: (response: { credential: string }) => void } | undefined;
  const restore = browser({ accounts: { id: {
    initialize: (input: typeof options) => { options = input; },
    renderButton: () => options!.callback({ credential: 'synthetic-signed-id-token' }),
  } } }, { replaceChildren: () => removed++ });
  try {
    assert.deepEqual(await browserGoogle('public-client-id').signIn({ nonce: 'a'.repeat(64), signal: new AbortController().signal }), { idToken: 'synthetic-signed-id-token' });
    assert.equal(options!.nonce, 'a'.repeat(64)); assert.equal(options!.client_id, 'public-client-id'); assert.equal(options!.auto_select, false);
    assert.equal(removed, 1);
  } finally { restore(); }
});

test('aborted Google popup ignores late credentials and clears its button', async () => {
  let callback!: (response: { credential: string }) => void; let removed = 0;
  let ready!: () => void; const rendered = new Promise<void>((resolve) => { ready = resolve; });
  const restore = browser({ accounts: { id: { initialize: (options: { callback: typeof callback }) => { callback = options.callback; }, renderButton: ready } } }, { replaceChildren: () => removed++ });
  try {
    const controller = new AbortController(); const pending = browserGoogle('public-client-id').signIn({ nonce: 'b'.repeat(64), signal: controller.signal });
    await rendered;
    assert.equal(typeof callback, 'function'); controller.abort();
    assert.equal(await pending, null); callback({ credential: 'late-synthetic-token' }); assert.equal(removed, 1);
  } finally { restore(); }
});

test('unavailable browser Google configuration and SDK errors fail safely', async () => {
  let removed = 0;
  const restore = browser({ accounts: { id: { initialize: () => { throw new Error('unsafe SDK detail'); } } } }, { replaceChildren: () => removed++ });
  try {
    const input = { nonce: 'c'.repeat(64), signal: new AbortController().signal };
    await assert.rejects(browserGoogle(undefined).signIn(input), /Authentication is unavailable/);
    await assert.rejects(browserGoogle('public-client-id').signIn(input), { message: 'Authentication is unavailable. Try again.' });
    assert.equal(removed, 1);
  } finally { restore(); }
});
