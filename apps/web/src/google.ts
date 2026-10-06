import { AuthUnavailableError } from '../../mobile/src/auth/controller';
import type { GoogleIdentityProvider } from '../../mobile/src/auth/controller';

interface GoogleId {
  initialize(options: { client_id: string; nonce: string; auto_select: boolean; callback: (response: { credential?: string }) => void }): void;
  renderButton(container: HTMLElement, options: { theme: string; size: string; width: number; shape: string }): void;
  disableAutoSelect(): void;
}
declare global { interface Window { google?: { accounts: { id: GoogleId } } } }
let loading: Promise<GoogleId> | undefined;

function loadGoogle(): Promise<GoogleId> {
  if (window.google) return Promise.resolve(window.google.accounts.id);
  if (loading) return loading;
  loading = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = 'https://accounts.google.com/gsi/client'; script.async = true;
    const timer = setTimeout(() => { script.remove(); loading = undefined; reject(new AuthUnavailableError()); }, 15_000);
    script.onload = () => {
      clearTimeout(timer);
      if (window.google) resolve(window.google.accounts.id);
      else { loading = undefined; reject(new AuthUnavailableError()); }
    };
    script.onerror = () => { clearTimeout(timer); script.remove(); loading = undefined; reject(new AuthUnavailableError()); };
    document.head.append(script);
  });
  return loading;
}

export function browserGoogle(clientId: string | undefined): GoogleIdentityProvider {
  return {
    async signIn({ nonce, signal }) {
      if (!clientId?.trim()) throw new AuthUnavailableError();
      let cancelLoad: (() => void) | undefined;
      const google = await Promise.race([loadGoogle(), new Promise<null>((resolve) => {
        cancelLoad = () => resolve(null);
        if (signal.aborted) cancelLoad(); else signal.addEventListener('abort', cancelLoad, { once: true });
      })]).finally(() => { if (cancelLoad) signal.removeEventListener('abort', cancelLoad); });
      const container = document.getElementById('google-signin-button');
      if (signal.aborted || !google) return null;
      if (!container) throw new AuthUnavailableError();
      return new Promise((resolve, reject) => {
        let settled = false;
        const finish = (result: { idToken: string } | null, expired = false) => {
          if (settled) return; settled = true;
          clearTimeout(timer); signal.removeEventListener('abort', cancel);
          container.replaceChildren(); if (expired) reject(new AuthUnavailableError()); else resolve(result);
        };
        const cancel = () => finish(null);
        const timer = setTimeout(() => finish(null, true), 4 * 60_000);
        signal.addEventListener('abort', cancel, { once: true });
        try {
          google.initialize({ client_id: clientId, nonce, auto_select: false,
            callback: (response) => finish(signal.aborted || !response.credential ? null : { idToken: response.credential }) });
          google.renderButton(container, { theme: 'outline', size: 'large', width: 300, shape: 'pill' });
        } catch { finish(null, true); }
      });
    },
  };
}
