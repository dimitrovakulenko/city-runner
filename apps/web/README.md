# Browser client

The first D37 slice uses React and MapLibre with the production backend. It reuses the generated API types, API clients, authentication controller and account-scoped stores in `apps/mobile/src`. Browser adapters supply Google Identity Services, tab session storage and real `File` multipart bodies. The PoC is separate.

Implemented: responsive Explore map with current location, account-scoped remembered view and offline recovery, date/type/source activity and track filters with explicit lifetime/selection GPS coverage, activities/search/detail/deletion with per-run exploration gains, city/street search, completion sorting, nearly-done filtering and Node Hunter, separate manual completion/undo, durable GPX/FIT manifests, source status and automatic foreground refresh, account archives/deletion, and waypoint planning with saved routes, GPX export and foreground GPS route following. Provider connections and desktop packaging remain future slices.

## Development

Use Node 22.12+ and the API/worker/PostGIS setup in [development](../../docs/development.md). From this directory:

```sh
rtk npm ci
rtk npm run dev
```

Open `http://127.0.0.1:5173`. Vite proxies `/api` to `http://127.0.0.1:8001`; `API_PROXY_TARGET` can select another local API. `VITE_API_BASE_URL` defaults to the same origin. A separate origin requires an explicitly configured API CORS policy. `VITE_MAP_STYLE` can override the public OpenFreeMap Liberty style.

For real Google login, set the public `VITE_GOOGLE_CLIENT_ID` to a registered **web** OAuth client ID, allow the exact browser origin in Google Console and include that ID in backend `GOOGLE_CLIENT_IDS`. Restart Vite and the API after changing configuration. The browser passes the exact server challenge nonce to [Google Identity Services](https://developers.google.com/identity/gsi/web/reference/js-reference), then exchanges the returned signed ID token with the backend. There is no browser client secret. Missing configuration fails closed; real Google consent remains unverified.

The opaque bearer session is held in this tab's `sessionStorage`; reload restores it through `/api/me`, and logout revokes it. Account changes clear private stores, abort requests and release selected files. Manifests and accepted jobs remain server-side. Browser `File` references are memory-only: after reload, select each waiting file again, then resume a paused batch. Accepted jobs continue after **Stop uploading**; deleted items have no retry/reselection action.

Activity detail lists paged **Imported files**. GPX/FIT exports with exactly matching original coordinates, segments, complete timestamps and parsed activity type share one activity, with separate originals and coverage support. Rounded or resampled exports remain separate. **Remove import** removes only that file's support; other imports keep the activity and coverage. Removing the final import removes the activity. **Delete activity** confirms removal of all its imports. Deleted manifest items stay terminal; a fresh explicit upload is allowed. An earlier accepted copy still resolving a deletion can temporarily return a retryable conflict. See [recording groups](../../docs/recording-groups.md).

## Regions and cities

Open **Cities & streets**, search available regions and use **Load more regions** beyond the first 20. The selected region remains visible when a search excludes it. Choosing a region or city centers the map on its public boundary bounds; foreground refresh preserves selection, filters and route drafts. City/street progress uses lifetime GPS. Pending or failed matching is labelled explicitly and never appears as zero completion.

An operator adds public geography using the [durable region import commands](../../docs/osm-import.md#durable-additional-regions). Existing activities are matched automatically without another file upload. Imported complete regions appear in the catalog; incomplete/staged geography does not claim supported coverage. Overlapping regions keep their own city denominators. The Explore overview remains capped at 20 datasets; lifetime selected-region summary can use its exact catalog detail, while a filtered selection outside that overview is labelled unavailable and its map still honors the filters.

## History imports

In **Imports**, use **Choose activity files**, **Choose folder** or **Import ZIP** to bring in a history. Up to 1,000 GPX/FIT files are accepted, with 10 MiB per file and 256 MiB combined; ZIPs are limited to 64 MiB and 10,000 entries. Unrelated files and nested archives are skipped. Invalid archive paths, metadata, activity CRC or output sizes fail preparation before any server manifest is created. **Cancel preparation** releases local files.

Histories have overall progress and duplicate/deleted counts; **Show files** opens paged groups of at most 20 with per-file status and safe parser guidance. Files upload one at a time. **Stop uploading** pauses waiting files while accepted jobs continue. **Retry uploading** reuses retained files after a network failure. After reload, select the same ZIP/folder/files again; matching uses basename, format, size and SHA-256, so duplicate names cannot attach different bytes. Reselection does not automatically resume a stopped history. Deleted items remain terminal. File handles and folder paths stay in memory and are cleared on account changes. See [history contract](../../docs/history-imports.md).

## Sources and automatic refresh

Open **Sources & sync** for account-wide retained activity counts, oldest known recording, last successful file import, GPX/FIT ingestion and coverage status, and paged failed jobs. **Retry coverage** recalculates an imported activity without uploading its original again. Corrupt/no-GPS files require a corrected file. Waiting files still require selection in Imports; stopped uploads leave accepted jobs processing. Garmin and Strava are explicitly unavailable until their adapters exist.

While signed in, visible and online, the browser checks a lightweight change token every 2.5 seconds. Unchanged responses do not reload history/maps. Changes refresh current views while preserving filters, selections, map scope and unsaved route edits; activity refresh is capped at five already-loaded pages. Hidden/offline tabs pause; reconnect reads fresh state. Five consecutive errors pause automatic refresh with an explicit **Retry refresh** action. See [status and refresh contract](../../docs/sync-status.md).

## Account data

Open **Account** (or the account avatar) and **Download account archive** to save your original files, segmented activity data, import/source history, GPS support, manual labels and saved routes. The private ZIP includes a manifest and excludes credentials and shared geography.

**Delete my account** requires typing `DELETE`. It revokes all sessions and removes account history immediately after durable acceptance; the notice distinguishes queued original-file cleanup. Cancellation and request errors leave controls available, and late responses cannot clear a replacement account. Only the deleted account's remembered viewport is forgotten. See [configuration, cleanup and backup recovery](../../docs/account-data.md).

## Route planning

Apply the backend migrations and explicitly set `ROUTING_PROVIDER=fossgis` for development foot routing. Routing is disabled by default; see [routing limits and provider policy](../../docs/routing.md). Routes use the account-owned production APIs, not browser storage.

Open **Routes**, click the map to add up to 20 waypoints, drag numbered markers to move them, and use the list to reorder/remove/undo or enter precise coordinates. A city street's remaining-node **+** button starts or extends the draft. Preview is explicit; edits discard old geometry and late responses cannot overwrite a newer draft. Enter a name, preview, then save. Reopen saved routes after reload or on another device, edit with revision conflict protection, delete, or export the saved GPX. Planned routes never change GPS coverage. GPX uses route points without recording timestamps.

Choose **Follow route** on the map after previewing or opening a saved route. Live position, accuracy-aware on/off-route status and pan/recenter work while the page is visible. GPS loss hides stale guidance and recovers on a fresh fix; hidden pages require explicit resume. Route edits, navigation and logout stop following. See the [following contract](../../docs/routing.md#web-route-following).

The development service receives/logs waypoints and permits at most one request per second. Its foot graph and 100 m snapping bound do not establish acceptance for every access restriction or closure. Provider capacity/terms acceptance remains open before public release.

For an existing disposable preview that must keep its uploaded activities while adding the route API, `ROUTES_API_PROXY_TARGET` can override only `/api/routes`; keep `API_PROXY_TARGET` pointed at its original upload/auth API. Both APIs must use the same migrated database. `DISCOVERY_API_PROXY_TARGET` can similarly override `/api/account`, `/api/cities`, `/api/streets` and activity list/filter options, `/api/map`, `/api/progress`, `/api/activities/{id}/impact`, `/api/sync` and matching-retry routes during local API upgrades without restarting the original upload worker. Automated browser fixtures clear both overrides. These local overrides are unnecessary with a single updated backend.

## Street discovery and Node Hunter

In **Cities & streets**, filter **Nearly done** to find streets with at least 80% GPS coverage that still need visits, or sort the whole city by most/least explored and fewest missing nodes. Click a street on the map to open its full detail and contributing activities, including streets absent from the current filtered page.

In **Explore**, choose **Find missing nodes**, then **Zoom to missing nodes**. Select a red map node or a node from the visible-area list and explicitly **Add node to route**. Node Hunter stays available alongside the route editor. Selection never adds a waypoint by itself; moving the map clears stale targets. Lists disclose unavailable/processing coverage and the existing 1,000-node cap. GPS gaps remain distinct from normal-threshold or manual street completion.

## Activity filters and map controls

Open **Activity & map filters**, choose inclusive start/end dates, activity type and GPX/FIT source, then **Apply filters**. The activity list and tracks use the same selection. **Map coverage** explicitly chooses **Lifetime GPS coverage** (default) or **Selected activities’ GPS coverage**; date filters alone never reduce lifetime coverage. Selection coverage uses the selected recordings’ live original GPS support, including source-kind provenance, with no manual GPS credit. Unknown dates are excluded by date bounds. **Reset filters** restores all history and lifetime coverage. City/street details and per-run contribution comparisons continue to use lifetime retained history. See [filter contract](../../docs/activity-filters.md).

**My location** requests browser location only after a click, centers the map and shows a temporary marker. Denial, unavailable GPS and timeout have explicit retry guidance; **Cancel location** ignores late responses. This changes no activities, coverage or route waypoints. Only center/zoom are remembered in account-scoped browser storage; location markers and private map/progress data are not cached on disk.

When offline, the app labels last loaded progress, removes map targets and disables cached street-node actions. Reconnecting refreshes the latest viewport and retains applied filters and coverage scope; **Retry connection** handles network failures while the browser still reports online. The basemap requires network access.

## Activity contribution

Activity list/detail also show GPS distance, recorded time and average pace using [persisted activity statistics](../../docs/activity-statistics.md). Distance and time use original recording segments, excluding gaps between segments; unmarked stops inside a continuous segment remain included. Missing/invalid timing makes time and pace unknown while preserving valid distance. Existing history requires the explicit bounded [backfill command](../../docs/development.md#recorded-activity-statistics); its updates appear through normal foreground refresh.

Open an activity to see **What this run added**: new GPS nodes, streets advanced/completed and affected streets in the selected region. Normal/strict completion uses the existing rules. Select a street to focus its original bounds and highlight its bounded display geometry, or open its details. Recording samples/segments are available under **Recording details**.

Gains compare retained matched activities by recorded date; same-day ordering is approximate. Importing older history or deleting activities can change the comparison. Manual labels never add GPS credit. Missing/invalid dates or the 10,000-activity comparison limit hide historical gains while preserving supported street links; unfinished matching stays unavailable. See the [impact contract](../../docs/activity-impact.md).

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

Verified locally: 33 web unit/adapter tests, 31 synthetic Chrome walkthroughs against the production bundle and the earlier public-Gent walkthrough. Filter/control checks cover inclusive date/type/source selection, unknown metadata, lifetime versus selected GPS nodes, owner isolation, current-location success/denial/unavailable/timeout/cancel, late logout callbacks, account-scoped reload views, offline target disabling and retained-filter reconnect. Contribution checks cover out-of-order history imports, completion and repeat credit, deletion refresh, repeated map focus, absent geometry, strict-rule requests, unavailable dates/processing, retry and account isolation. A separate localhost Gent walkthrough verified Goudenleeuwplein progressing from 13 to 38 matched nodes; its temporary account and uploads were removed, preserving the existing user activity. Street-discovery checks cover completion sorting, nearly-done filtering, map detail outside a filtered page, Node Hunter selection/route addition, stale target removal and mixed pending/limited coverage. Browser route checks use an injected synthetic provider response, never the public service. They cover marker dragging, reorder/remove/undo, saved-route reload, escaped GPX download, other-account 404s, deletion and unchanged GPS progress. Development Chrome checks also verify automatic login, session restoration, logout/re-login and recovery of stale test sessions. Import checks cover segmented maps, responsive layout, private signed-out state, account isolation, activity/street/correction calls, GPX/FIT stop/resume with reselection after reload, serialized file selection, failed-upload retry, corrupt-file guidance and terminal deletion. Real Google consent still requires a registered web client ID. Deployment, native route UI/device acceptance and desktop packaging remain future work.

Recording checks use exact synthetic GPX/FIT equivalents, separate original sources and one logical activity. They verify unchanged GPS/timestamps, responsive import controls, cancellation, single-import/whole-activity removal, retained coverage, terminal deleted manifests, explicit reimport and remote-source removal invalidating an open confirmation. The production recording flows were repeated after the final backend guard fix.

History checks use a 45-file synthetic ZIP split into three batches and a 23-file folder. They cover accepted-job completion after stop, repeated stop/resume, duplicates, failed-upload retry, corrupt-file guidance, terminal deletion, reload/reselection, original GPS preservation, late account responses and 390 px layout. ZIP unit checks cover preflight bounds, CRC/truncation, paths, encryption/symlinks, bad offsets, oversized extraction, skipped entries and cancellation.

Region checks cover importing geography after activity ingestion, automatic source backfill, original GPS preservation, independent overlapping-region progress, catalog paging/search beyond 20 regions, exact selected-region refresh, repeated map focus, pending counts, retry, late account responses and responsive layout. The persistent preview also passed actual Chrome checks against public Brugge with zero default activities/tracks.

Automatic city preparation adds local discovery from original GPS, shared public country/city tasks and city-specific preparation/retry status on Explore, Imports, Cities and Sources. Enable the development adapter on both API and worker as described in [development setup](../../docs/development.md#activity-triggered-city-preparation). The current suite passes 33 web unit/adapter tests and 31 production Chrome flows, including first-city indexing, second-account reuse, retry without reupload, logout fencing and a 390 px layout. Country coverage, a production data provider and hosted worker execution remain acceptance gates.

Following checks use browser geolocation with synthetic positions and real saved-route APIs. They verify on/off-route accuracy, pan/recenter and camera position after responsive resizing, offline GPS, stale/coarse/error recovery, permission revocation, explicit foreground resume, route edits, delayed logout, stopped-watch callbacks and unchanged activities/progress. Automatic camera moves preserve remembered viewport preferences.

All seven [hosted CI jobs](https://github.com/dimitrovakulenko/city-runner/actions/runs/38779194141) pass on `01d706d`, including the analytics capacity, native authoring, cancellation, restart, restore and upgrade compatibility suites.
