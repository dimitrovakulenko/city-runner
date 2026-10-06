export interface AuthAccount {
  id: string;
}

export interface AuthChallenge {
  id: string;
  nonce: string;
}

export interface AuthExchange {
  token: string;
  account: AuthAccount;
}

export interface AuthApi {
  getMe(): Promise<AuthAccount>;
  createChallenge(input: { provider: 'google' }): Promise<AuthChallenge>;
  exchange(input: { challenge_id: string; id_token: string }, options: { persist: false }): Promise<AuthExchange>;
  logout(): Promise<void>;
}

export interface AuthSessionStore {
  getToken(): Promise<string | null>;
  setTokenIfCurrent(token: string, isCurrent: () => boolean): Promise<boolean>;
  clearIfCurrent(token: string): Promise<boolean>;
  subscribe(listener: (token: string | null) => void): () => void;
}

export class AuthUnavailableError extends Error {
  constructor() {
    super('Authentication is unavailable. Try again.');
    this.name = 'AuthUnavailableError';
  }
}

export interface GoogleIdentityProvider {
  /** Return null when the user cancels. The native adapter should honor abort. */
  signIn(input: { nonce: string; signal: AbortSignal }): Promise<{ idToken: string } | null>;
}

export type AuthState =
  | { status: 'restoring' }
  | { status: 'signed-out'; error?: string }
  | { status: 'signing-in'; step: 'challenge' | 'provider' | 'exchange' }
  | { status: 'signed-in'; account: AuthAccount; error?: string }
  | { status: 'unavailable'; error: string };

type Listener = (state: AuthState) => void;

/** Injectable auth orchestration; the UI and native Google module stay separate. */
export class AuthController {
  private state: AuthState = { status: 'restoring' };
  private generation = 0;
  private signInInFlight = false;
  private logoutInFlight = false;
  private abortController: AbortController | null = null;
  private listeners = new Set<Listener>();
  private readonly unsubscribeSession: () => void;

  constructor(
    private readonly api: AuthApi,
    private readonly google: GoogleIdentityProvider,
    private readonly sessions: AuthSessionStore,
  ) {
    this.unsubscribeSession = sessions.subscribe((token) => {
      if (token === null && this.state.status === 'signed-in') {
        this.beginOperation();
        this.setState({ status: 'signed-out' });
      }
    });
  }

  getState(): AuthState {
    return this.state;
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    listener(this.state);
    return () => this.listeners.delete(listener);
  }

  async restore(): Promise<AuthState> {
    if (this.logoutInFlight) return this.state;
    const generation = this.beginOperation();
    this.setState({ status: 'restoring' });
    try {
      const account = await this.api.getMe();
      if (generation === this.generation) this.setState({ status: 'signed-in', account });
    } catch (error) {
      if (generation === this.generation) {
        this.setState(isUnauthorized(error)
          ? { status: 'signed-out' }
          : { status: 'unavailable', error: safeMessage(error) });
      }
    }
    return this.state;
  }

  async signIn(): Promise<AuthState> {
    if (this.signInInFlight || this.logoutInFlight) return this.state;
    this.signInInFlight = true;
    const generation = this.beginOperation();
    const abortController = new AbortController();
    this.abortController = abortController;
    try {
      this.setState({ status: 'signing-in', step: 'challenge' });
      const challenge = await this.api.createChallenge({ provider: 'google' });
      if (!this.isCurrent(generation)) return this.state;

      this.setState({ status: 'signing-in', step: 'provider' });
      const identity = await this.google.signIn({ nonce: challenge.nonce, signal: abortController.signal });
      if (!this.isCurrent(generation)) return this.state;
      if (!identity) {
        this.setState({ status: 'signed-out' });
        return this.state;
      }

      this.setState({ status: 'signing-in', step: 'exchange' });
      const session = await this.api.exchange(
        { challenge_id: challenge.id, id_token: identity.idToken }, { persist: false },
      );
      if (!this.isCurrent(generation)) return this.state;
      const stored = await this.sessions.setTokenIfCurrent(session.token, () => this.isCurrent(generation));
      if (this.isCurrent(generation) && stored) this.setState({ status: 'signed-in', account: session.account });
    } catch (error) {
      if (this.isCurrent(generation)) {
        if (isCancellation(error) || abortController.signal.aborted) {
          this.setState({ status: 'signed-out' });
        } else if (error instanceof AuthUnavailableError) {
          this.setState({ status: 'unavailable', error: safeMessage(error) });
        } else {
          this.setState({ status: 'signed-out', error: safeMessage(error) });
        }
      }
    } finally {
      if (this.abortController === abortController) this.abortController = null;
      this.signInInFlight = false;
    }
    return this.state;
  }

  /** Returns false once exchange starts; it cannot safely be undone client-side. */
  cancelSignIn(): boolean {
    if (this.state.status !== 'signing-in' || this.state.step === 'exchange') return false;
    this.generation += 1;
    this.abortController?.abort();
    this.abortController = null;
    this.setState({ status: 'signed-out' });
    return true;
  }

  async logout(): Promise<AuthState> {
    if (this.logoutInFlight) return this.state;
    const previousAccount = this.state.status === 'signed-in' ? this.state : null;
    this.logoutInFlight = true;
    if (this.state.status === 'signing-in') this.cancelSignIn();
    const generation = this.beginOperation();
    let token: string | null;
    try {
      token = await this.sessions.getToken();
    } catch {
      this.logoutInFlight = false;
      const error = 'Could not access the saved session. Retry sign out.';
      if (generation === this.generation) this.setState(previousAccount
        ? { ...previousAccount, error }
        : { status: 'unavailable', error });
      return this.state;
    }
    if (!token) {
      this.logoutInFlight = false;
      if (generation === this.generation) this.setState({ status: 'signed-out' });
      return this.state;
    }
    let revokeConfirmed = false;
    try {
      await this.api.logout();
      revokeConfirmed = true;
    } catch {
      // Local sign-out still proceeds; the UI reports that server revocation is unconfirmed.
    }
    let localCleared = false;
    let storageFailed = false;
    try {
      localCleared = await this.sessions.clearIfCurrent(token);
      if (!localCleared) localCleared = (await this.sessions.getToken()) === null;
    } catch {
      storageFailed = true;
    }
    this.logoutInFlight = false;
    if (generation === this.generation) {
      if (localCleared) {
        this.setState(revokeConfirmed
          ? { status: 'signed-out' }
          : { status: 'signed-out', error: 'Signed out on this device; server revocation is unconfirmed.' });
      } else {
        const message = storageFailed
          ? 'Could not clear the saved session. Retry sign out.'
          : 'The session changed during sign out. Retry.';
        const previous = this.state;
        this.setState(previous.status === 'signed-in'
          ? { ...previous, error: message }
          : { status: 'unavailable', error: message });
      }
    } else if (localCleared && !revokeConfirmed && this.state.status === 'signed-out') {
      this.setState({
        status: 'signed-out',
        error: 'Signed out on this device; server revocation is unconfirmed.',
      });
    }
    return this.state;
  }

  private beginOperation(): number {
    this.generation += 1;
    this.abortController?.abort();
    this.abortController = null;
    return this.generation;
  }

  private isCurrent(generation: number): boolean {
    return generation === this.generation;
  }

  private setState(state: AuthState): void {
    this.state = state;
    for (const listener of this.listeners) listener(state);
  }
}

function isUnauthorized(error: unknown): boolean {
  return typeof error === 'object' && error !== null
    && 'status' in error && (error as { status?: unknown }).status === 401;
}

function isCancellation(error: unknown): boolean {
  return typeof error === 'object' && error !== null
    && 'name' in error && (error as { name?: unknown }).name === 'AbortError';
}

function safeMessage(error: unknown): string {
  void error;
  return 'Authentication is unavailable. Try again.';
}
