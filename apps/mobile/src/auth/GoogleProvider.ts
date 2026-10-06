import type { GoogleIdentityProvider } from './controller';
import { AuthUnavailableError } from './controller';

export interface GoogleNativeModule {
  configure(options: { webClientId: string; iosClientId?: string; nonce: string }): void;
  presentExplicitSignIn(): Promise<{
    type: 'success' | 'cancelled' | 'noSavedCredentialFound';
    data: { idToken: string } | null;
  }>;
}

export interface GoogleProviderConfig {
  webClientId?: string;
  iosClientId?: string;
  platform: 'ios' | 'android' | 'web' | 'other';
}

export function createGoogleProvider(
  native: GoogleNativeModule,
  config: GoogleProviderConfig,
): GoogleIdentityProvider {
  return {
    async signIn({ nonce, signal }) {
      const webClientId = config.webClientId?.trim();
      const iosClientId = config.iosClientId?.trim();
      if (!webClientId || (config.platform === 'ios' && !iosClientId)) {
        throw new AuthUnavailableError();
      }
      if (signal.aborted) return null;

      // The server's nonce is passed unchanged. Do not call signIn()/getTokens():
      // iOS may restore a cached ID token that was issued for a previous nonce.
      native.configure({
        webClientId,
        ...(config.platform === 'ios' ? { iosClientId } : {}),
        nonce,
      });
      const result = await native.presentExplicitSignIn();
      if (signal.aborted || result.type === 'cancelled') return null;
      if (result.type !== 'success' || !result.data?.idToken) {
        throw new Error('Google sign-in did not return an ID token.');
      }
      return { idToken: result.data.idToken };
    },
  };
}

declare const require: (id: string) => unknown;

export function loadGoogleProvider(config: GoogleProviderConfig): GoogleIdentityProvider {
  return {
    signIn(input) {
      if (!config.webClientId?.trim() || (config.platform === 'ios' && !config.iosClientId?.trim())) {
        return Promise.reject(new AuthUnavailableError());
      }
      const native = (require('react-native-nitro-google-signin') as {
        GoogleOneTapSignIn: GoogleNativeModule;
      }).GoogleOneTapSignIn;
      return createGoogleProvider(native, config).signIn(input);
    },
  };
}
