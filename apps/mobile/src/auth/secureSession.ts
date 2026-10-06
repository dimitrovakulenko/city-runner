import * as SecureStore from 'expo-secure-store';
import { createSecureSessionStore, SecureSessionStore } from './sessionStore';

export const secureSessionStore: SecureSessionStore = createSecureSessionStore(SecureStore);
