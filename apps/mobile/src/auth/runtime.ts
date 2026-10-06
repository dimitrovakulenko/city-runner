import { Platform } from 'react-native';
import { createActivityApi } from '../api/client';
import { AuthController } from './controller';
import { loadGoogleProvider } from './GoogleProvider';
import { secureSessionStore } from './secureSession';

export const mobileApi = createActivityApi({
  baseUrl: process.env.EXPO_PUBLIC_API_BASE_URL ?? 'http://localhost:8001',
  sessionStore: secureSessionStore,
});

export const authController = new AuthController(
  mobileApi,
  loadGoogleProvider({
    webClientId: process.env.EXPO_PUBLIC_GOOGLE_WEB_CLIENT_ID,
    iosClientId: process.env.EXPO_PUBLIC_GOOGLE_IOS_CLIENT_ID,
    platform: Platform.OS === 'ios' || Platform.OS === 'android' ? Platform.OS : 'other',
  }),
  secureSessionStore,
);
