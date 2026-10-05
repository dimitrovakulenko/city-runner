# Native mobile testing

The MapLibre screen requires a native development build. Expo Go and JavaScript bundle exports do not verify the native map integration.

## Current host inventory

Checked 5 October 2026 on an Apple Silicon Mac:

- Xcode 26.5 (build 17F42), with iOS 26.5 device and simulator SDKs.
- `rtk proxy xcrun simctl list runtimes` returned an empty runtime list when checked outside the sandbox; no iOS Simulator runtime is installed.
- No Android Studio, Android SDK, `adb`, or emulator command is installed; `ANDROID_HOME` and `ANDROID_SDK_ROOT` are unset.
- `/usr/bin/java` exists as the macOS launcher, but `rtk proxy java -version` reports that no Java runtime is installed. There is no usable JDK.
- CocoaPods (`pod`) is unavailable.
- Root volume reported 129 GiB available at the time of the check.

No native build or device launch was possible on this host. The existing TypeScript check and Expo bundle export are not native verification.

## Required local downloads

The app uses Expo SDK 57, React Native 0.86.3, and `@maplibre/maplibre-react-native` 11.0.0.

For iOS, install the **iOS 26.5 Simulator runtime** from Xcode > Settings > Components (or Platforms, depending on Xcode's UI). Xcode already contains the iOS 26.5 SDK; only the simulator runtime is missing. Xcode displays the exact download size before installation. The host had 129 GiB available; check free space again before installing.

For Android, install Android Studio and use its bundled compatible JDK, then verify the generated project's Java requirement. Use SDK Manager to install the Android SDK platform and build tools required by the Expo SDK 57 prebuild, Android SDK Platform-Tools (`adb`), Android Emulator, and one Android emulator system image. Run `rtk npx expo prebuild --platform android` first to see the generated project's exact `compileSdkVersion`, `buildToolsVersion`, and Java requirement; select the matching versions in SDK Manager. Android Studio displays package download sizes before installation. Check free space again before installing; the emulator system image is several gigabytes.

CocoaPods is needed to install iOS native dependencies. Install it through the host's configured Ruby/Bundler setup before building. The iOS build will create/use the native project and resolve pods.

## Build and exercise

From `apps/mobile/`, after installing the required tools and project dependencies:

```sh
rtk npm ci
rtk npx expo prebuild --platform ios
rtk npm run ios
```

For Android, first set `ANDROID_HOME` to the installed SDK directory and add its `platform-tools` and `emulator` directories to `PATH`. Then run:

```sh
rtk npx expo prebuild --platform android
rtk npm run android
```

To run an installed native development build with the Metro server:

```sh
rtk npm start
```

Record the device model, OS version, build command/result, and any native logs. Manually verify opening the activity list, selecting an activity, returning from its detail view, zooming/panning the map, and the empty/error states. A successful typecheck, prebuild, or JavaScript export alone does not establish native acceptance.
