# Browser client

The first D37 slice uses React and MapLibre with the production backend. It reuses the generated API types, API clients, authentication controller and account-scoped stores in `apps/mobile/src`. Browser adapters supply Google Identity Services, tab session storage and real `File` multipart bodies. The PoC is separate.

Implemented: responsive Explore map, activities/search/detail/deletion, city/street search and remaining nodes, separate manual completion/undo, durable GPX/FIT manifests, and waypoint planning with saved routes and GPX export. Provider connections and desktop packaging remain future slices.

## Development

Use Node 22.12+ and the API/worker/PostGIS setup in [development](../../docs/development.md). From this directory:

```sh
rtk npm ci
rtk npm run dev
```

Open `http://127.0.0.1:5173`. Vite proxies `/api` to `http://127.0.0.1:8001`; `API_PROXY_TARGET` can select another local API. `VITE_API_BASE_URL` defaults to the same origin. A separate origin requires an explicitly configured API CORS policy. `VITE_MAP_STYLE` can override the public OpenFreeMap Liberty style.

For real Google login, set the public `VITE_GOOGLE_CLIENT_ID` to a registered **web** OAuth client ID, allow the exact browser origin in Google Console and include that ID in backend `GOOGLE_CLIENT_IDS`. Restart Vite and the API after changing configuration. The browser passes the exact server challenge nonce to [Google Identity Services](https://developers.google.com/identity/gsi/web/reference/js-reference), then exchanges the returned signed ID token with the backend. There is no browser client secret. Missing configuration fails closed; real Google consent remains unverified.

The opaque bearer session is held in this tab's `sessionStorage`; reload restores it through `/api/me`, and logout revokes it. Account changes clear private stores, abort requests and release selected files. Manifests and accepted jobs remain server-side. Browser `File` references are memory-only: after reload, select each waiting file again, then resume a paused batch. Accepted jobs continue after **Stop uploading**; deleted items have no retry/reselection action.

## Route planning

Apply the backend migrations and explicitly set `ROUTING_PROVIDER=fossgis` for development foot routing. Routing is disabled by default; see [routing limits and provider policy](../../docs/routing.md). Routes use the account-owned production APIs, not browser storage.

Open **Routes**, click the map to add up to 20 waypoints, drag numbered markers to move them, and use the list to reorder/remove/undo or enter precise coordinates. A city street's remaining-node **+** button starts or extends the draft. Preview is explicit; edits discard old geometry and late responses cannot overwrite a newer draft. Enter a name, preview, then save. Reopen saved routes after reload or on another device, edit with revision conflict protection, delete, or export the saved GPX. Planned routes never change GPS coverage. GPX uses route points without recording timestamps.

The development service receives/logs waypoints and permits at most one request per second. Its foot graph and 100 m snapping bound do not establish acceptance for every access restriction or closure. Provider capacity/terms acceptance remains open before public release.

For an existing disposable preview that must keep its uploaded activities while adding the route API, `ROUTES_API_PROXY_TARGET` can override only `/api/routes`; keep `API_PROXY_TARGET` pointed at its original upload/auth API. Both APIs must use the same migrated database. This local override is unnecessary with a single updated backend.

## Verification

```sh
rtk npm test
rtk npm run build
```

The build includes strict typechecking and a separately bundled MapLibre worker. CI runs both commands. Regenerate the shared contracts with `rtk .venv/bin/python apps/mobile/scripts/generate-api-types.py` from the repository root; use `--check` to detect drift.

Browser checks require backend Python dependencies on `PATH`, an existing Google Chrome installation and a local PostGIS administrator connection:

```sh
rtk proxy env POSTGIS_ADMIN_DATABASE_URL=postgresql+psycopg://USER@127.0.0.1:PORT/postgres npm run test:browser
rtk proxy env WEB_TEST_PRODUCTION=1 POSTGIS_ADMIN_DATABASE_URL=postgresql+psycopg://USER@127.0.0.1:PORT/postgres npm run test:browser
```

Run `build` before the production check. Automated checks use ports 5174/8004, leaving the preview on 5173/8003 untouched. Each run starts the real API and worker, migrates a new UUID-named disposable database, imports synthetic OSM/activities through production handlers and drops that database afterwards. Fixture sessions exist only in that local test database; the application has no fixture login. Existing databases and private PoC files are never opened. Playwright uses installed Chrome without downloading a browser or native SDK.

For a clean local UI preview, start `tests/serve-fixture.py` with `WEB_PREVIEW_EMPTY=1` and proxy to its port 8003. This creates no synthetic streets or activities. Open the browser signed out; seeded accounts and horizontal test geometry are reserved for the automated walkthroughs.

For the requested development test login, start that disposable backend with `WEB_TEST_LOGIN=1` and start Vite with `VITE_DEV_TEST_LOGIN=1 API_PROXY_TARGET=http://127.0.0.1:8003 npm run dev`. A fresh localhost tab automatically receives a session for the empty synthetic account and shows **Test account**. Reload restores the session; after sign-out, reloading creates a new one. Existing sessions are preserved. This opt-in works only on loopback in Vite development mode; production bundles never request it, and the production backend has no test-login endpoint.

For public streets without seeded runs, also set `WEB_PREVIEW_GENT_OSM=/path/to/gent.osm` to the exact public snapshot recorded in [OSM import](../../docs/osm-import.md). The local test backend verifies its checksum, imports Gent's 3,108 street groups and 51,134 original nodes, and generates GPX/FIT fixtures along two real central streets. Accounts start with zero activities. This preview remains a disposable local database.

With that backend and the web preview running, `rtk npm run test:gent` verifies the actual browser file picker/upload → worker ingestion → street matching → activity detail, contributions, progress and rendered-map responses. It checks original coordinates/timestamps and segment preservation, GPX completion of all 38 Goudenleeuwplein nodes and FIT completion of all 20 Poeljemarkt nodes, restart display and isolation from the empty default account. The test deletes its own two synthetic activities afterwards. No real workout history is used.

Verified locally: 10 adapter tests, 6 synthetic Chrome walkthroughs against the production bundle and the earlier public-Gent walkthrough. Browser route checks use an injected synthetic provider response, never the public service. They cover marker dragging, reorder/remove/undo, saved-route reload, escaped GPX download, other-account 404s, deletion and unchanged GPS progress. Development Chrome checks also verify automatic login, session restoration, logout/re-login and recovery of stale test sessions. Import checks cover segmented maps, responsive layout, private signed-out state, account isolation, activity/street/correction calls, GPX/FIT stop/resume with reselection after reload, serialized file selection, failed-upload retry, corrupt-file guidance and terminal deletion. Real Google consent still requires a registered web client ID. Deployment, native route UI/device acceptance and desktop packaging remain future work.
