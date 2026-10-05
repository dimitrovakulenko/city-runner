# City Runner mobile shell

Expo + React Native + TypeScript shell for Android, iPhone and tablets. Activity fixtures match `GET /api/activities` and `GET /api/activities/{id}` in `poc/activity_api.py`; IDs stay strings and tracks retain the API's segmented `[longitude, latitude]` shape. The demo has no backend connection or provider credentials.

The map uses MapLibre Native with the public MapLibre demo style. Use a licensed production style and tiles before release. MapLibre requires a native development build and cannot run in Expo Go.

## Build and run

From this directory:

```sh
npm install
npm run android
npm run ios
```

Android requires Android Studio/SDK and an emulator or device. iOS requires macOS, Xcode and an iOS simulator or device. `npm run typecheck` checks TypeScript. `npm start` launches the development client server after a native build exists.

## Verification status

- TypeScript: verified with `npm run typecheck`.
- Expo SDK dependency check: verified online with `npx expo install --check`.
- Android build: unverified. `npm run android -- --no-bundler` stops because Android SDK and `adb` are not installed on this host.
- iOS build: unverified. `npm run ios -- --no-bundler` stops because CocoaPods is unavailable; Xcode is present, but CoreSimulator is unavailable on this host.
