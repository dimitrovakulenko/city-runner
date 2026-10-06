import assert from 'node:assert/strict';
import test from 'node:test';
import { AuthApi, AuthController, GoogleIdentityProvider } from '../src/auth/controller';
import { createGoogleProvider } from '../src/auth/GoogleProvider';
import { createSecureSessionStore, SecureStoreModule } from '../src/auth/sessionStore';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

class MemorySecureStore implements SecureStoreModule {
  value: string | null = null;
  async getItemAsync() { return this.value; }
  async setItemAsync(_key: string, value: string) { this.value = value; }
  async deleteItemAsync() { this.value = null; }
}

function api(overrides: Partial<AuthApi> = {}): AuthApi {
  return {
    async getMe() { return { id: 'account-1' }; },
    async createChallenge() { return { id: 'challenge-1', nonce: 'server-nonce' }; },
    async exchange() { return { token: 'opaque-session', account: { id: 'account-1' } }; },
    async logout() {},
    ...overrides,
  };
}

function identity(overrides: Partial<GoogleIdentityProvider> = {}): GoogleIdentityProvider {
  return { async signIn() { return { idToken: 'provider-id-token' }; }, ...overrides };
}

test('compare-and-clear cannot delete a later session when storage operations race', async () => {
  const native = new MemorySecureStore();
  const store = createSecureSessionStore(native);
  await store.setToken('old-session');

  const staleResponseClear = store.clearIfCurrent('old-session');
  const newLoginWrite = store.setToken('new-session');
  assert.equal(await staleResponseClear, true);
  await newLoginWrite;
  assert.equal(await store.getToken(), 'new-session');

  const secondNewLoginWrite = store.setToken('latest-session');
  const olderClear = store.clearIfCurrent('new-session');
  await Promise.all([secondNewLoginWrite, olderClear]);
  assert.equal(await store.getToken(), 'latest-session');
});

test('conditional session commit removes a write if its auth attempt is cancelled mid-write', async () => {
  const written = deferred<void>();
  const continueWrite = deferred<void>();
  const native = new MemorySecureStore();
  native.setItemAsync = async (_key, value) => {
    await continueWrite.promise;
    native.value = value;
    written.resolve();
  };
  const store = createSecureSessionStore(native);
  let current = true;
  const commit = store.setTokenIfCurrent('late-session', () => current);
  await Promise.resolve();
  current = false;
  continueWrite.resolve();
  await written.promise;
  assert.equal(await commit, false);
  assert.equal(native.value, null);
});

test('clearIfCurrent leaves the stored token intact when secure storage read fails', async () => {
  const native = new MemorySecureStore();
  native.value = 'session';
  native.getItemAsync = async () => { throw new Error('Keychain unavailable'); };
  const store = createSecureSessionStore(native);
  await assert.rejects(store.clearIfCurrent('session'), /Keychain unavailable/);
  assert.equal(native.value, 'session');
});

test('restore checks the current account and turns unauthorized sessions into signed-out state', async () => {
  const store = createSecureSessionStore(new MemorySecureStore());
  const controller = new AuthController(api(), identity(), store);
  assert.deepEqual(await controller.restore(), { status: 'signed-in', account: { id: 'account-1' } });

  const unauthorized = new AuthController(api({
    async getMe() { throw Object.assign(new Error('expired'), { status: 401 }); },
  }), identity(), store);
  assert.deepEqual(await unauthorized.restore(), { status: 'signed-out' });
});

test('cancelled native sign-in does not exchange an identity token', async () => {
  const started = deferred<AbortSignal>();
  const store = createSecureSessionStore(new MemorySecureStore());
  let exchanges = 0;
  const controller = new AuthController(api({
    async exchange() { exchanges += 1; return { token: 'unexpected', account: { id: 'account-1' } }; },
  }), identity({
    signIn({ signal }) {
      started.resolve(signal);
      return new Promise((resolve) => signal.addEventListener('abort', () => resolve(null), { once: true }));
    },
  }), store);

  const signIn = controller.signIn();
  const signal = await started.promise;
  assert.equal(controller.cancelSignIn(), true);
  assert.equal(signal.aborted, true);
  assert.deepEqual(await signIn, { status: 'signed-out' });
  assert.equal(exchanges, 0);
  assert.equal(await store.getToken(), null);
});

test('sign-in sends the server challenge nonce unchanged and commits only after exchange', async () => {
  const store = createSecureSessionStore(new MemorySecureStore());
  let receivedNonce = '';
  let persistOption: { persist: false } | undefined;
  const controller = new AuthController(api({
    async exchange(_input, options) {
      persistOption = options;
      return { token: 'opaque-session', account: { id: 'account-1' } };
    },
  }), identity({ async signIn({ nonce }) { receivedNonce = nonce; return { idToken: 'signed-id-token' }; } }), store);
  assert.deepEqual(await controller.signIn(), { status: 'signed-in', account: { id: 'account-1' } });
  assert.equal(receivedNonce, 'server-nonce');
  assert.deepEqual(persistOption, { persist: false });
  assert.equal(await store.getToken(), 'opaque-session');
});

test('logout invalidates a delayed exchange before it can store a session', async () => {
  const exchange = deferred<{ token: string; account: { id: string } }>();
  const exchangeStarted = deferred<void>();
  const store = createSecureSessionStore(new MemorySecureStore());
  const controller = new AuthController(api({
    async exchange() { exchangeStarted.resolve(); return exchange.promise; },
    async logout() { throw new Error('No local session yet.'); },
  }), identity(), store);

  const signIn = controller.signIn();
  await exchangeStarted.promise;
  assert.equal(controller.cancelSignIn(), false);
  assert.deepEqual(await controller.logout(), { status: 'signed-out' });
  exchange.resolve({ token: 'late-session', account: { id: 'account-1' } });
  await signIn;
  assert.equal(await store.getToken(), null);
  assert.equal(controller.getState().status, 'signed-out');
});

test('offline logout clears only the local session and reports unconfirmed revocation', async () => {
  const store = createSecureSessionStore(new MemorySecureStore());
  await store.setToken('session');
  const controller = new AuthController(api({
    async logout() { throw new Error('offline'); },
  }), identity(), store);
  assert.deepEqual(await controller.logout(), {
    status: 'signed-out', error: 'Signed out on this device; server revocation is unconfirmed.',
  });
  assert.equal(await store.getToken(), null);
});

test('Google provider configures each challenge nonce and uses only explicit fresh sign-in', async () => {
  const configurations: Array<{ webClientId: string; iosClientId?: string; nonce: string }> = [];
  let explicitCalls = 0;
  const provider = createGoogleProvider({
    configure(options) { configurations.push(options); },
    async presentExplicitSignIn() {
      explicitCalls += 1;
      return { type: 'success', data: { idToken: `id-token-${explicitCalls}` } };
    },
  }, { platform: 'ios', webClientId: 'web-client', iosClientId: 'ios-client' });
  const signal = new AbortController().signal;

  assert.deepEqual(await provider.signIn({ nonce: 'server-nonce-1', signal }), { idToken: 'id-token-1' });
  assert.deepEqual(await provider.signIn({ nonce: 'server-nonce-2', signal }), { idToken: 'id-token-2' });
  assert.deepEqual(configurations.map(({ nonce }) => nonce), ['server-nonce-1', 'server-nonce-2']);
  assert.equal(explicitCalls, 2);
});

test('Google provider does not start when unconfigured and discards success after cancellation', async () => {
  let nativeCalls = 0;
  const native = {
    configure() { nativeCalls += 1; },
    async presentExplicitSignIn() {
      nativeCalls += 1;
      return { type: 'success' as const, data: { idToken: 'must-not-escape' } };
    },
  };
  const unconfigured = createGoogleProvider(native, { platform: 'android' });
  await assert.rejects(unconfigured.signIn({ nonce: 'nonce', signal: new AbortController().signal }),
    { name: 'AuthUnavailableError' });
  assert.equal(nativeCalls, 0);

  const controller = new AbortController();
  const provider = createGoogleProvider(native, { platform: 'android', webClientId: 'web-client' });
  controller.abort();
  assert.equal(await provider.signIn({ nonce: 'nonce', signal: controller.signal }), null);
  assert.equal(nativeCalls, 0);
});

test('secure-session revocation propagates into an authenticated controller', async () => {
  const store = createSecureSessionStore(new MemorySecureStore());
  await store.setToken('expired-session');
  const controller = new AuthController(api(), identity(), store);
  await controller.restore();
  assert.equal(controller.getState().status, 'signed-in');
  await store.clearIfCurrent('expired-session');
  assert.deepEqual(controller.getState(), { status: 'signed-out' });
});

test('logout does not claim local sign-out when secure storage cannot be read or cleared', async () => {
  const readFailureStorage = new MemorySecureStore();
  readFailureStorage.value = 'session';
  readFailureStorage.getItemAsync = async () => { throw new Error('Keychain unavailable'); };
  let remoteLogoutCalls = 0;
  const readFailureController = new AuthController(api({
    async logout() { remoteLogoutCalls += 1; },
  }), identity(), createSecureSessionStore(readFailureStorage));
  assert.deepEqual(await readFailureController.logout(), {
    status: 'unavailable', error: 'Could not access the saved session. Retry sign out.',
  });
  assert.equal(remoteLogoutCalls, 0);

  const deleteFailureStorage = new MemorySecureStore();
  deleteFailureStorage.value = 'session';
  deleteFailureStorage.deleteItemAsync = async () => { throw new Error('Keychain locked'); };
  const deleteFailureStore = createSecureSessionStore(deleteFailureStorage);
  const deleteFailureController = new AuthController(api({
    async getMe() { return { id: 'account-1' }; },
    async logout() { throw new Error('offline'); },
  }), identity(), deleteFailureStore);
  await deleteFailureController.restore();
  assert.deepEqual(await deleteFailureController.logout(), {
    status: 'signed-in', account: { id: 'account-1' },
    error: 'Could not clear the saved session. Retry sign out.',
  });
  assert.equal(deleteFailureStorage.value, 'session');
});

test('session invalidation during logout is not suppressed by the older logout operation', async () => {
  const store = createSecureSessionStore(new MemorySecureStore());
  await store.setToken('session');
  const remoteLogout = deferred<void>();
  const controller = new AuthController(api({
    async logout() { await remoteLogout.promise; },
  }), identity(), store);
  await controller.restore();

  const logout = controller.logout();
  await Promise.resolve();
  await store.clearIfCurrent('session');
  assert.deepEqual(controller.getState(), { status: 'signed-out' });
  remoteLogout.resolve();
  assert.deepEqual(await logout, { status: 'signed-out' });
});
