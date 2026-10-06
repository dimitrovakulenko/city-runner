# City Runner mobile

Expo + React Native client for the production backend: Google sign-in, secure sessions, activity search/detail, Explore coverage, GPX import, city/street browsing and [deletion/manual completion](../../docs/mobile-corrections.md). Real mode uses `EXPO_PUBLIC_API_BASE_URL` (default `http://localhost:8001`). Never put bearer tokens or provider secrets in Expo public variables.

See [Google login setup](../../docs/mobile-login.md) for public OAuth client IDs and native registration, [Explore/import](../../docs/mobile-explore.md) for device API addresses, map configuration and bounded foreground refresh, and [city/street browsing](../../docs/mobile-cities.md) for version selection, missing nodes and activity contributions. The development street map defaults to OpenFreeMap Liberty; `EXPO_PUBLIC_MAP_STYLE_URL` is optional. Coverage needs an active complete OSM dataset and a running backend worker.

Fixtures are opt-in with `EXPO_PUBLIC_FIXTURE_MODE=true`. They use local sample activities without real authentication, coverage requests or imports. Track segments preserve GPS gaps; IDs remain strings.

## Checks and API types

Install locked dependencies with `rtk npm ci`. Run `rtk npm run typecheck` and `rtk npm test` from this directory. Set up the repository Python environment once, then generate/check API types from FastAPI OpenAPI:

```sh
rtk proxy python3 -m venv ../../.venv
rtk proxy ../../.venv/bin/pip install -r ../../backend/requirements.txt
rtk proxy ../../.venv/bin/python scripts/generate-api-types.py
rtk proxy ../../.venv/bin/python scripts/generate-api-types.py --check
```

The generated file is `src/api/generated.ts`; do not edit it manually.

## Native run

Run `rtk npm run android`, `rtk npm run ios`, or `rtk npm start` after installing the required native tooling. MapLibre, SecureStore and Google sign-in require a development/native build and cannot run in Expo Go. See [native testing](../../docs/native-testing.md) for this host's tooling gaps. Typechecks, controller tests and JS exports do not establish native acceptance. Configured Google login, picker/upload, map gestures and restart/logout still need Android and iOS device checks.
