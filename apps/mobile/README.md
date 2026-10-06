# City Runner mobile

Expo + React Native client for the paginated activity API. Real mode uses `EXPO_PUBLIC_API_BASE_URL` (default `http://localhost:8000`) and the backend's opaque bearer session. The session adapter currently has no token configured, so real mode displays sign-in-required until D07 supplies native provider login and secure storage. Do not place bearer tokens in Expo public environment variables.

Fixtures are opt-in with `EXPO_PUBLIC_FIXTURE_MODE=true`; they are local sample data and do not simulate login. The map keeps each API track segment separate, including GPS gaps, and activity IDs remain strings.

## Checks and API types

Install the locked JavaScript dependencies with `npm ci`. Run `npm run typecheck` and `npm test` from this directory. Set up the repository Python environment once, then generate/check API types from FastAPI OpenAPI:

```sh
python3 -m venv ../../.venv
../../.venv/bin/pip install -r ../../backend/requirements.txt
source ../../.venv/bin/activate
npm run api:types
npm run api:types:check
```

The generated file is `src/api/generated.ts`; do not edit it manually.

## Native run

Run `npm run android`, `npm run ios`, or `npm start` after a native development build exists. MapLibre requires a native build and cannot run in Expo Go. Native launch and secure-session integration remain unverified; D07 supplies the platform login/storage adapter.
