# City Runner development plan

Updated 6 October 2026; original baseline main `f3e67ba`. This is the execution plan for a working development application. [PROJECT.md](PROJECT.md) owns product requirements; [BUILD_PLAN.md](BUILD_PLAN.md) owns architecture and risks; [BACKLOG.md](BACKLOG.md) owns work-package status. D IDs below subdivide the existing T packages, not additional scope or estimates. Current execution states are recorded below.

## Execution record

| Task | State | Evidence / remaining gate |
| --- | --- | --- |
| D01 | Done: local and hosted checks passed | `8478f10`; disposable PostGIS runner passed, including inherited unrelated database URL; [hosted CI on f3bbfc4](https://github.com/dimitrovakulenko/city-runner/actions/runs/37368238597) passed backend and mobile typecheck |
| D02 | Partial; native execution blocked by missing tooling | `3fc2517`, [native testing](docs/native-testing.md); no installed iOS runtime, CocoaPods, Android tools or usable JDK; no native build/UI acceptance |
| D03 | Done: feasibility dossier; provider approval remains open | `cb6a26e`, [provider dossier](docs/provider-feasibility.md); no outreach/access claims |
| D04 | Done: reviewed contract and typed activity responses | `336f86f`, [backend contract](docs/backend-contract.md); baseline API tests pass; source ownership, nonce and map-version invariants pinned |
| D05 | Server implementation verified; native/provider acceptance pending | `4219f5d`, Luna High; 11 backend tests and four disposable PostGIS checks passed, including concurrent first login, single-use challenge and legacy-owner migration; real credentials/native login remain open |
| D06 | Client implementation verified; native acceptance pending | `2dece0a`, `3d7ea59`, Luna Medium; generated activity/auth types, pagination/search/detail, explicit fixture mode and session isolation; 11 client tests and typecheck passed; D07 supplies implemented login and secure storage; device checks remain open |
| D07 | Google adapter and secure sessions verified in software; native/provider acceptance pending | `c27273d`, `1e6b075`; fresh exact-nonce native request, backend exchange, serial SecureStore adapter, restore/logout/cancel and account-race regressions; public OAuth configuration and both device walkthroughs remain open |
| D09 | Done: reviewed and verified; Luna Medium | `d2aea13`; 12 disposable PostGIS checks passed across auth/jobs/activities, including lease recovery, stale acknowledgements, parallel claims, bounded recovery, rollback, priority/retry/cancellation and sanitized logs; CLI completed a synthetic job in one attempt |
| D10 | Done: reviewed and verified; Luna High | `515f0da`, [GPX import](docs/gpx-import.md); bounded authenticated multipart upload, private originals, per-account dedupe, atomic source/job completion and stale-worker protection; actual HTTP + worker CLI smoke passed |
| D11 | Done: reviewed and verified; Luna | `2068abb`, [OSM import](docs/osm-import.md); versioned XML import, boundary holes/components, shared original nodes and indexed lookups; public Gent snapshot validated 3,108 streets and 51,134 distinct nodes; samples and replacement versions stay staged |
| D12 | Done: matching and summaries reviewed and verified; Luna High | `1d55e48`, `51502e6`, [coverage](docs/coverage.md); indexed 25 m sample matching, source/revision/lease fencing, overlap-preserving deletion and bounded requeue; both matcher/requeue lock interleavings verified; replacement activation remains deferred |
| D13 | Done: bounded map/progress APIs reviewed and verified; Luna | `4fd00d7`, `acf9dd7`, [map API](docs/map-api.md); owner-scoped segmented geometry, live lifetime coverage, close-zoom missing nodes, consistent versions, explicit truncation and pending states; public Gent fresh-import check passed |
| D14 | Connected Explore/import UI verified in software; native acceptance pending | `83f2036`, [mobile Explore](docs/mobile-explore.md); GPX picker/upload, bounded foreground status, segmented tracks, street completion and close-zoom missing nodes; account, viewport and polling races covered; actual picker/gestures/import remain device gates |
| D15 | Done: city/street API reviewed and verified; Luna | `e2c25f7`, [explorer API](docs/explorer-api.md); version-qualified bounded search/filter pages, remaining original nodes and live owned contributions; pending/failed coverage never appears complete; PostgreSQL and localhost HTTP/browser checks passed |
| D16 | City/street mobile screens verified in software; native acceptance pending | `c3537a6`, [mobile cities](docs/mobile-cities.md); explicit active dataset selection, city/street search and paging, GPS remainder and contribution navigation; account/selection/paging revisions covered |
| D17 | Deletion/manual completion verified in software; native acceptance pending | `04f42cd`, `fceab12`, `05faefb`, [backend corrections](docs/corrections.md), [mobile controls](docs/mobile-corrections.md); owner/version-scoped labels and undo, overlap-preserving deletion, durable original cleanup and reimport; concurrent source/job changes rollback and retry under READ COMMITTED; account-safe mutations and dependent refreshes |
| D18 | FIT and durable bulk imports verified in software; native acceptance pending | `99edcdd`, `daac906`, `65971c2`; Luna High parser/storage, coordinator SQL orchestration and Luna Medium mobile, independent GPT-6.1 Sol High review; generated types preceded UI wiring; [file imports](docs/file-import.md), [mobile imports](docs/mobile-imports.md); bounded parsing, manifests, sequential submission, duplicate/retry/stop/resume/restart and terminal deletion |
| D26 | Development foot-routing adapter verified; public provider acceptance pending | `c7fe102`, `9f76ca3`; [routing](docs/routing.md); explicit FOSSGIS foot graph, 100 m bounded snapping, shared database quota, bounded response/timeout and safe errors; access profile reviewed, live restriction/capacity/terms acceptance remains open |
| D27 | Web waypoint editor verified; native UI pending | `3d4036f`, `cf838ff`; add/drag/move/reorder/remove/undo, remaining-node entry, explicit distance preview and stale edit/account response fencing; responsive Chrome controls verified |
| D28 | Saved-route backend/web flows verified; native UI pending | `9f76ca3`, `3d4036f`, `cf838ff`; owner-only persistence, revision-qualified edits/deletion, reopen across browser sessions/API restart and valid planned GPX export; no GPS coverage effects |
| D37 | Partial: reviewed browser UI and planner verified | `cf838ff`, earlier browser commits; [browser client](apps/web/README.md); real activities/map/cities/corrections, resumable GPX/FIT imports and saved-route planning; 10 adapter tests, typecheck/build, 6 production Chrome flows and public-Gent verification; provider connections, real Google consent and desktop packages remain open |

Other D tasks remain todo. The coordinator owns these status updates.

Current priority per user direction: web UI first. Android/iOS execution and device walkthroughs belong to a separate agent/team. D37's browser baseline reuses the existing production APIs and client logic; native testing is outside this work. D19 remains the next source/sync backend contract slice.

Public-geography web verification on 6 October: the documented Gent snapshot passed checksum validation and loaded 3,108 street groups and 51,134 original OSM nodes into the local preview. A production-bundle Chrome walkthrough uploaded synthetic GPX/FIT files through the real picker and sequential bulk upload, then verified workers, exact coordinates/timestamps and segments, processed activity detail, street contributions, completed-map responses and progress. Goudenleeuwplein reached 38/38 GPS nodes and Poeljemarkt 20/20. The default account stayed at zero activities/visits, and the test removed its own activities afterwards. Eight adapter tests, web typecheck and generated-type drift checks also passed. This is public Gent coverage only; the preview database is disposable and Google OAuth was outside this requested slice.

Browser polish on 6 October: missing nodes start hidden at overview zoom, remaining streets use lighter strokes, imports show per-file submission progress and actionable pause/reselection/retry guidance, and FIT activities receive sport/date titles. Five production-bundle Chrome checks passed, including delayed manifest creation/navigation with sequential batch queuing, failed-upload retry with retained files, visible-layer truncation warnings, stopped-file reselection, corrupt FIT guidance and terminal deletion; the public-Gent walkthrough also passed again. Shared mobile software checks passed 89 tests/typecheck, backend units passed 32, disposable PostGIS passed 62 and generated types matched. Both JavaScript exports passed; device testing remains assigned to the separate team. Real Google consent is requested but awaits the registered web OAuth client ID.

Independent GPT-6.1 Sol High review passed `b30c888` against `194b262`, covering auth/account fencing, upload recovery/serialization, terminal deletion, segmented maps and FIT compatibility. Reviewed commits merged into main as `a6283fd` and pushed. [Hosted browser/import CI](https://github.com/dimitrovakulenko/city-runner/actions/runs/37518803881) passed backend (including generated contracts and disposable PostGIS), mobile typecheck/tests and web adapter tests/build.

At the user's request, local web development now supports an explicitly enabled automatic test-account login. Only the disposable loopback fixture backend exposes session creation; production bundles disable the client flow and the production backend has no endpoint. Chrome verified fresh login, reload, logout/re-login, stale-session recovery and empty activity history; 10 adapter tests/build and all 5 production walkthroughs passed. This development shortcut does not establish real Google consent.

Web planning on 6 October: imported filenames now open their activity and focus its segmented track (`214227c`). Independently reviewed contract `c7fe102` preceded Luna High backend `9f76ca3`, backend-generated types, Luna Medium shared client `3d4036f`, then web wiring `cf838ff`. GPT-6.1 Sol High review passed the exact integrated head. Checks passed: 41 backend units, 67 disposable PostGIS integrations, 104 shared/mobile tests and full typecheck, generated-type drift check, 10 web adapter tests/build and 6 production Chrome flows. Regressions cover malformed/nonfinite routing input, provider bounds/errors, concurrent edits/deletion, response snapshots, stale detail/preview/account races, GPX XML/decimal correctness and unchanged GPS progress. Browser tests inject synthetic routing responses; actual FOSSGIS HTTP through localhost separately returned a 243.3 m foot route with 44 positions, then saved/reopened/exported it and recovered it after restarting only the new route API. The existing preview upload API/worker stayed alive and its uploaded activity/source/batch counts remained 1/1/1. Both JavaScript exports passed using existing dependencies; no native SDK, native build/device acceptance, deployment or private PoC file access was involved. Public routing capacity/terms and live access restrictions remain release gates; Android/iOS UI execution belongs to the separate team.

D16/D17 verification on 6 October: 20 unchanged PoC tests, 25 backend unit tests, 51 disposable PostGIS tests, 64 integrated mobile tests, mobile typecheck and generated-type drift check passed. Actual localhost HTTP verified authenticated GPX upload → ingestion worker → coverage worker → viewport/progress and city/street detail/contributions, including BIGINT IDs, gaps/timestamps, two-account isolation, pending geography, duplicates, shared nodes and safe malformed failure. D17 HTTP checks also passed public activity deletion, overlapping support, worker file cleanup, exact-file reimport and manual completion/undo without fabricated GPS visits. Threaded regressions cover late source attachment, requeued jobs and upload reads during deletion. In-app browser Swagger verified authenticated GPS/manual/effective city and progress totals; earlier checks covered incomplete filtering and remaining-node detail. Activities were synthetic; Gent used public OSM only. Native UI and real provider login/sync remain pending. D02/D07 device acceptance remains required for M1. M1 is not yet complete.

D18 backend verification: 20 unchanged PoC tests, 31 backend unit tests and 62 disposable PostGIS tests passed, including concurrent same-item uploads, deletion/retry interleavings, transaction/commit failure cleanup and stale revision/lease fencing. Actual HTTP and worker CLI passed synthetic FIT/GPX imports, duplicate manifest/content replay, stop/resume, expired-lease recovery, API restart, corrupt-file retry and terminal deletion. Safari Swagger verified unauthenticated 401 and authenticated persisted per-file states/counts. No private PoC files, deployment or native SDK installation was involved. Next software contract slice: D19 source/sync status; native acceptance remains separate.

D18 mobile review and integrated checks passed 87 tests, typecheck and generated-type drift checks. Regressions cover sequential per-file submission, stopping an in-flight/resumed upload, queued resumes, restart/reselection, deleted items, reciprocal stale list/detail responses, concurrent manifest creation, account/picker races and foreground polling ownership. The generated types were checked against the implemented backend before wiring UI. D18 iOS and Android JavaScript exports passed with synthetic public OAuth IDs; neither establishes native build or device acceptance.

[Hosted D18 CI on `94cfe5d`](https://github.com/dimitrovakulenko/city-runner/actions/runs/37509349402) passed backend units, generated-type drift, disposable PostGIS migrations/integration tests and mobile install/typecheck/tests.

Integrated D16/D17 iOS and Android JavaScript exports passed with synthetic public OAuth IDs. Exports do not verify native builds, SDK execution or device gestures.

## Target and current gap

Build a complete personal exploration loop: **sign in → import history → inspect all supported cities and missing streets → plan a route → finish an activity → see updated coverage**. Android and iPhone first, iPad/Android tablets with the mobile beta, then installable Windows/macOS/Linux apps. Core maps, missing-node discovery and manual route planning remain free. Introduce the 14-day trial only after defining premium features.

“Comparable to CityStrides” means comparable core exploration capabilities. Its advertised challenges, replay, weather and other extras are a separate later parity backlog. It currently lists $5/month or $50/year, with route building and Node Hunter in the supporter tier; our initial planner and missing-node tools are free. [Official pricing](https://citystrides.com/pricing), [homepage](https://citystrides.com/).

| Existing code | Keep | Missing for a real app |
| --- | --- | --- |
| `poc/` | GPX parsing, matching fixtures, coverage rules, working browser comparison | Single-user SQLite and full scans cannot be the shared application backend |
| `backend/` | FastAPI, Alembic, PostgreSQL/PostGIS, owner-scoped activities, verified provider tokens, bearer sessions, durable GPX/FIT manifests and ingestion, shared regional OSM, node matching, viewport/progress, city/street and deletion/manual completion APIs | Planner, replacement activation, hosted S3 source storage and real provider credentials |
| `apps/mobile/` | Expo/React Native, MapLibre Native, generated clients, Google adapter, SecureStore, activity and city/street search/detail, resumable GPX/FIT uploads and Explore coverage; fixtures explicitly opt-in | Native acceptance, planner and location; default mode requires sign-in |
| `infra/` | Terraform Lightsail/private S3 and Caddy/systemd templates | Worker deployment, executable installation procedure, secrets, backups/restore, deployed acceptance |
| Tests | PoC, backend/client checks, repeatable PostGIS CI and actual HTTP import/matching/map checks | Native builds/UI checks, real identity/provider acceptance and deployed end-to-end checks |

Earlier manual Chrome checks covered the PoC import/map only. Current production API verification is recorded above. **Native builds/UI, real cloud sync and deployed end-to-end functionality remain unverified.** JS export is not native acceptance.

## Explicit transition out of the PoC

New application functionality belongs in `backend/`, `apps/mobile/` and `apps/web/`. Keep `poc/` runnable as a comparison until the first production vertical slice passes. Extract reusable pure parsing/rule code or deliberately port it with shared fixtures; do not make the new backend call the PoC SQLite store. There is no backend language rewrite.

Use the same production modules locally: API process, worker process and PostgreSQL/PostGIS. Development uses local private files; the hosted application uses private S3 through a small storage boundary. A local synthetic account is an explicit development/test mode, bound to localhost and rejected in hosted configurations. Ordinary deployments continue to fail closed until real authentication is configured. Real Google login is required for the working development milestone; do not leave the synthetic login as the product login.

At milestone M1, the regular development command starts the production application. PoC becomes an experiment/regression reference. The new app must upload and process data itself; inserting activity rows by hand is not acceptance. Reimport permitted source files into the new backend initially; migrating every experimental SQLite row is not required. Keep private PoC files untouched.

## Architecture to implement

- Python/FastAPI modular backend; a worker from the same package. PostgreSQL/PostGIS for shared OSM geography, accounts, sources, visits and durable jobs.
- One production Lightsail VM, Caddy, systemd and private S3. No containers, Lambda, Redis or microservices needed. Separate disposable compute comes later for untrusted agent previews.
- React Native/TypeScript for mobile/tablets, MapLibre Native, secure platform session storage. A generated OpenAPI client supplies actual endpoint types. Native health bridges follow cloud/file integration.
- Build the requested browser UI in `apps/web/` as the first slice of the eventual desktop React/MapLibre GL JS interface, reusing the mobile core's pure stores and generated contracts. Keep the existing PoC browser available meanwhile.
- Regional OSM import and local city lookup. Proposed first real dataset: Belgium, validating Gent and Brussels. Importer configuration must support subsequent regions; unsupported geography displays **coverage pending**, not “you ran nothing”. This is a staged coverage rollout, not a reduction of the eventual multi-city product.
- External street-level basemap and pedestrian routing service selected before planner work. Use configurable providers and bounded quotas; do not host a worldwide routing graph on the initial VM.
- Foreground status refresh initially; background cloud imports run on the server. Add a status stream only if polling measurements justify it. During-run foreground location is separate from importing a completed workout.

### Contracts to settle before dependent agents

D04 writes the concrete schema/API contract; a reviewer approves it before implementation tasks consume it. These are the required invariants:

| Area | Rule |
| --- | --- |
| Identity | Verified provider subject maps to our account; email alone cannot link accounts. Backend resolves identity from each request and checks ownership on every private resource. No client-supplied user ID is trusted. |
| Activity IDs | Our database allocates internal IDs. Provider IDs are separate strings, unique within their source/account. Preserve existing API string IDs, including values outside JS integer precision; two users/providers can have the same external activity ID. |
| Tracks | Segmented `[longitude, latitude]` samples with aligned original timestamps. Unknown metadata remains unknown. No line across gaps; no new checkpoints; simplify rendering only. |
| Geography | Original OSM node IDs, ways and city boundaries shared once per dataset version. Version eligibility and city/name street grouping. Retain way membership so an OSM update can be explained. |
| Matching | Actual GPS samples within 25 m of an eligible node. Normal completion: at least 90%, but all nodes when fewer than ten; strict mode: all nodes. City progress is completed eligible streets / eligible streets. Zero-node streets excluded. Node score is not precise road-length coverage. |
| Contributions | Keep account/source/activity/node provenance. Deleting one activity removes only its support; visits supported by another activity remain. Manual completion is separate and reversible. |
| Sources | Exact source IDs/content hashes make retries idempotent. Similarity across providers is not sufficient to destructively merge records; preserve independent provenance before cross-source matching. |
| Jobs | Upload/event and durable job committed before acknowledgement. Lease, retry/backoff, terminal failures, cancellation and bounded attempts. New activities outrank historical backfill. Revision checks stop stale jobs restoring deleted data. |
| Maps | Indexed, bounded viewport/zoom requests, simplified tracks and close-zoom node layers. Explicit truncation/paging rather than silent missing data. Period filters do not silently change lifetime coverage. |
| Privacy | Default-private GPS and private source files. Source-specific retention/deletion applies to derived progress and backups as well as raw files. Agents receive synthetic fixtures and permitted diagnostics. |

API groups, added only when their task starts: current account/session; activities; uploads/import status; cities/streets/nodes/contributions; viewport layers/progress revision; connections; routes; reports; account export/deletion. Preserve existing activity list/detail behaviour; add explicit response models and additive fields. Pin pagination, filter semantics, error codes and auth behaviour before a mobile agent uses them.

## Milestones and exit gates

| Milestone | Usable result | Exit proof |
| --- | --- | --- |
| M0: runnable foundation | Documented local database/API and native development builds; reviewed contracts; provider feasibility tracked | Clean setup follows documented commands; actual PostgreSQL/PostGIS tests in CI; Android and iOS map/list launch with build/device evidence |
| M1: real development app | Google account, real GPX import, durable processing, regional OSM, coverage and map in both apps | Two accounts isolated; import spanning Gent/Brussels; duplicate/restart/delete; both clients read the same persisted data; no fixture dependency |
| M2: full exploration | City/street search/detail, missing nodes, contribution explanations, manual overrides, FIT/bulk imports, free planner and current position | Find remaining streets, plan/edit/save/reopen/export a pedestrian route, finish/import a run and see progress on both phones |
| M3: connected mobile beta | Approved Garmin and Strava history/new-event sync, health imports, reconnection, tablets, privacy and private reports | Real account history comparison, new activity/edit/delete/revocation, measured latency, physical-device and iPad/Android tablet checks, restore drill |
| M4: release and required distribution | Deployed mobile release and Windows/macOS/Linux packages using the same account/backend | Signed install/update/login/map/planner/import checks on every declared platform; publish only with launch gates satisfied |
| M5: later product stages | Defined premium/trial/payments, isolated support previews and trusted automatic fixes; chosen parity extras | Trial preserves free core; payment replay checks; seeded report verified at exact revision, tests/review then merge and observable delivery |

M1/M2 development can progress while cloud approvals are pending. **M3 and public launch cannot claim mandatory integrations from fixtures or file imports.** M4 desktop work and M5 support/payment work are independent after their foundations; their numbering does not require finishing desktop before private preview experiments.

### Provider launch gates

Garmin requires business-program approval before evaluation. Test actual permitted history, files, notifications and reconnect before promising all-history sync. [Garmin FAQ](https://developer.garmin.com/gc-developer-program/program-faq/), [Activity API](https://developer.garmin.com/gc-developer-program/activity-api/).

Strava's published policy restricts permanent storage of source/derived data, caching duration, sharing and AI use. Our assessment: the lifetime-progress design needs explicit scope/storage clarification or permission; ordinary OAuth alone is insufficient evidence. Strava owner-only initial access, capacity review and webhook behaviour also need verification in our registered app. Never use real Strava data as agent context. [API policy §§2.3, 5.3–5.5, 6.2](https://www.strava.com/legal/api_policy), [access guide](https://developers.strava.com/docs/getting-started/), [webhooks](https://developers.strava.com/docs/webhooks/).

D03 prepares a concrete approval/access checklist, not external messages or applications. Owner supplies registration decisions/credentials and authorizes submissions separately. A denied mandatory integration triggers a product decision; do not hide it behind “coming soon”. Public endpoints do not establish access to other people's private workouts.

## Agent-sized implementation backlog

Tasks below define scope and dependencies; current states are in the execution record. File ownership is assigned at dispatch. D04 owns shared contract/schema decisions; migrations and `backend/app/main.py` changes are integrated serially. Later broad platform/provider tasks must be split again if they exceed one reviewable change.

| ID / parent | Deliverable | Dependencies | Required acceptance |
| --- | --- | --- | --- |
| D01 / T02 | Reproducible local PostGIS setup and baseline CI | None | Actual migration/API integration checks, existing PoC tests, mobile typecheck; ordinary processes, no cloud apply |
| D02 / T11 | Android and iOS native development baseline | None | MapLibre map/list launches on emulator/simulator; document versions and blockers; physical-device acceptance still pending |
| D03 / T01 | Provider feasibility and registration dossier | None | Separate Garmin/Strava access, history, retention and approval questions with official sources; no claimed approval |
| D04 / T02 | Account/activity/source schema and API contract | D01 | Reviewed ID allocation, owner/session boundary, response types and migration plan; legacy contract retained |
| D05 / T03 | Backend identity verification and revocable sessions | D04 | Valid Google/Apple subjects, invalid issuer/audience/expiry/state rejected where applicable; request-owned identity, logout/revocation and two-account tests |
| D06 / T11 | Mobile generated client, configuration and real activity screens | D02,D04,D05 | Reads paginated list/detail from backend; expired session/error/retry; explicit fixture mode only; large IDs/gaps retained |
| D07 / T03 | Google login on Android/iOS | D05,D06 | Real signup/login/cancel/resume, secure session storage, account persists after restart; native redirect/config evidence |
| D08 / T03 | Apple login and safe account linking | D05,D06 | iOS real-device login/revocation, explicit authenticated linking, same-email identities remain separate until linked |
| D09 / T05 | Durable job runner | D04 | Claim/lease/retry/dead-letter tests, restart recovery, priority of new work, idempotent state changes |
| D10 / T05 | GPX upload/parser/storage pipeline | D05,D09 | Real upload through API, point/timestamp validation and limits, stored private source, exact duplicate and visible job failure |
| D11 / T04 | Regional OSM import and indexed city/street/node tables | D04 | Configurable public extract, stable OSM IDs, repeat import, boundaries/private-road fixtures, dataset version; no private GPS sent for discovery |
| D12 / T06 | PostGIS sample-to-node matching and summaries | D09,D10,D11 | 25 m boundary, small/strict streets, nearby parallel roads, gaps, overlap/retry; actual PostgreSQL/PostGIS tests |
| D13 / T11 | Viewport track/street/node APIs | D05,D12 | Indexed bounded queries, account isolation, explicit data limits, zoom behaviour, progress revision and pending geography |
| D14 / T11 | Real mobile Explore map and import/status flow | D06,D07,D10,D13 | Pick GPX, follow processing, see actual coverage/missing nodes; refresh/resume; real street basemap; both phones |
| D15 / T11 | City/street search/detail/contribution APIs | D12 | Paginated stable ordering, incomplete/partial/completed filters, remaining nodes and supporting activities; owner tests |
| D16 / T11 | City/street detail and missing-node discovery UI | D14,D15 | Search cities/streets, inspect partial progress, nearest visible unfinished candidates and contributing runs; loading/error/empty states |
| D17 / T06 | Activity deletion, manual completion/undo and recalculation | D05,D12 | Last-support deletion removes visit; overlapping activity preserves it; manual label/undo; source/rule revision cannot restore stale data |
| D18 / T05 | FIT import and bulk/resumable file jobs | D10,D14 | FIT GPS/no-GPS/corrupt fixtures; batch item counts, cancellation/retry/restart, exact duplicate; backend and phone picker exercised |
| D19 / T13 | Connection/sync status and source provenance contracts | D05,D09,D17 | Secrets server-side; per-source cursors/status, reconnect/disconnect semantics, retention/deletion and duplicate attribution defined |
| D20 / T07 | Strava OAuth/token lifecycle | D03 access gate,D19 | Real consent/cancel/refresh/revoke, encrypted server credentials, no account takeover or secret in client/log |
| D21 / T07 | Permitted Strava backfill, events and reconciliation | D20,D12 | History limit evidence, create/update/delete, duplicates, rate limits and missed-event recovery; source policy verified |
| D22 / T08 | Approved Garmin OAuth/token lifecycle | D03 approved evaluation,D19 | Real registered connection/cancel/reconnect, server credentials, required attribution |
| D23 / T08 | Garmin history, files, notification import | D22,D12 | Available-history comparison, notification/new activity, duplicate/update/delete where supported, reconnect and actual limits |
| D24 / T13 | Shared cross-source contribution/reconciliation rules | D19; fixtures before D21/D23 | Same workout across providers/files, distinct similar workouts, source deletion/revocation; no false destructive merge |
| D25 / T11,T13 | Connections/history progress and automatic client refresh | D14,D19; real acceptance after D21,D23 | Counts/oldest available/last success/retry/reconnect; new event outranks backfill; foreground refresh without full-history reload |
| D26 / T12 | Pedestrian routing service adapter | D11; vendor decision | Valid foot route, distance, timeout/quota/no-route, avoid known inaccessible ways; service terms/cost documented |
| D27 / T12 | Free waypoint route editor over missing-node map | D14,D16,D26 | Add/move/remove/undo, geometry/distance preview, no stale response overwrites edits, usable phone controls |
| D28 / T12 | Saved routes and GPX export | D05,D27 | Save/reopen/edit/delete on another device, owner isolation, segmented valid GPX share/export |
| D29 / T11 | Foreground location and route following | D14,D28 | Permission denial/revoke, GPS loss, resume, current position against planned route; no background-recording promise |
| D30 / T09 | HealthKit workout-route import bridge | D18,D19 | Physical Apple workout, permission refusal, late/updated/missing routes; consent before upload; provenance retained |
| D31 / T10 | Health Connect foreground import bridge | D18,D19 | Actual supported Android device, consent-dependent other-app route, missing routes, updates; background limitation visible |
| D32 / T15 | Account export/deletion and retention execution | D17,D19 | Owner export, delete all sources/sessions/private files/derived progress, retry/restart, retention-aware backup restore behaviour |
| D33 / T11,T13 | History-scale map/import benchmark and targeted fixes | D16,D18,D25 | Synthetic workload, recorded device/VM counts, CPU/memory/storage and p50/p95; fixes tied to measured bottlenecks |
| D34 / T15 | VM install/runbook, worker unit, secrets, backup/restore | D09,D32; infra already merged | Local/staging rehearsal, least-privilege file/database access, restore drill, rollback; deployment only when authorized |
| D35 / T20 | Private report submission and status | D05,D16 | Report expected/actual/steps/version, track state, other-account 404, no GPS/tokens attached automatically |
| D36 / T16,T15 | Tablet layouts and mobile beta acceptance | D08,D25,D28–D35 | iPad Air and Android tablet rotation/window checks plus both phones; device matrix and exact builds; real mandatory integrations |
| D37 / T17 | Desktop React/MapLibre interface | M2 API stable | Same account/maps/street search/planner/file imports; browser walkthrough; share client contracts, not server rules |
| D38 / T18 | Tauri packaging and native auth/file/session integration | D37 | Windows/macOS/Linux packages, OAuth return, secure credentials, import/export and update behaviour |
| D39 / T19 | Platform release CI/signing/install QA | D36,D38; OS/license decisions | Actual declared-OS installation/update evidence, protected signing credentials, store/self-hosting docs |
| D40 / T14 | Premium entitlements, 14-day trial and payments | Premium/trial/store decisions,D05 | Server authority, sandbox purchase/replay/cancel/refund; trial expiry retains free planner/maps/coverage |
| D41 / T21 | Isolated previews and reporter revision verification | D35,D01; preview budget/runtime decision | Synthetic DB, exact commit/build, no production access, expiry; native defects get native builds; new commit invalidates verdict |
| D42 / T22 | Bounded fix agent and trusted merge/release controller | D41; protected repository checks | Seeded eligible defect: reproduce/test/preview/user verify/review/merge; failed checks/stale verdict/sensitive change block landing |

M1 uses D01,D02,D04–D07,D09–D14 and the deletion portion of D17. M2 adds D08,D15–D18,D26–D29. M3 requires provider/health/status work D19–D25,D30–D36 and actual provider access. Contract dependencies do not mean unrelated work waits for the entire milestone.

## How to run cheaper agents

Recommended execution model: **Luna, Medium reasoning** for bounded tasks; **Luna, High** for well-specified parsing/job/SQL work. Use **GPT-6.1 Sol, High** for contract decisions and review of identity, spatial matching, provider retention/deletion, migrations and merge orchestration. These effort assignments are our recommendation, not a guaranteed model capability. Prefer a small reviewed brief over “build the backend”. Official guidance places Luna on scoped work and Sol on complex technical work; use the models actually available in your account. [OpenAI model selection](https://developers.openai.com/api/docs/guides/model-selection).

Start with three independent branches from current main:

| Agent | Task | Owned paths | Level |
| --- | --- | --- | --- |
| A | D01 development setup + CI | `scripts/dev/`, `.github/workflows/`, `backend/tests/integration/`, `docs/development.md`; no API/migration edits | Luna Medium |
| B | D02 native baseline | `apps/mobile/`, `docs/native-testing.md`; no backend/infra edits | Luna Medium |
| C | D03 provider dossier | `docs/provider-feasibility.md` only | Luna Medium |

After A is reviewed/merged, D04 settles the contract. Then run API work and mobile integration in parallel only against that reviewed contract. D09 and D11 can develop separate modules after schema ownership is settled; D10 follows D09, D12 follows ingestion/geography. D26 vendor work can proceed while approved-provider implementation waits. Avoid assigning two mobile agents to the same `App.tsx` or two backend agents to the same migration chain.

Each branch handles one D task. Return the commit, changed paths, acceptance evidence, commands and remaining blockers. Implementations stay off main until independent review/integration; the coordinator follows the user's active merge instructions. The coordinator updates this task ledger and existing T statuses after evidence, rather than every agent editing shared tracking files. Review against current main and run relevant checks after merging dependent branches. Archive worktrees only after their changes are integrated/preserved.

If a task spans auth + database + UI + deployment, split it at its contract boundary before giving it to a cheap model. Escalate uncertain design or repeated failed acceptance to review; do not keep spending retries on an underspecified problem. No agent may silently redefine completion rules, provider support or release scope.

### Copyable dispatch template

```text
Work in an isolated branch/worktree from current main in city-runner.
Implement only DEV_PLAN.md task Dxx. Read PROJECT.md, BUILD_PLAN.md and
the task's accepted contracts. Follow AGENTS instructions and RTK.md.
Owned paths: <explicit list>. Dependencies already merged: <commit IDs>.
Deliver: <one behaviour>. Acceptance: <task checks + fixtures>.
Keep the PoC and existing API behaviour working. Use synthetic test data.
Do not edit other agents' paths, deploy, submit provider applications,
install large SDKs without authorization, or merge to main.
Run the relevant checks. Return commit, changes, evidence and blockers;
distinguish code/tests from actual native or provider verification.
```

### Ready briefs

**D01 — reproducible development and integration CI**

Read current backend migration/tests and existing test commands. Add documented ordinary-process PostgreSQL/PostGIS setup, an explicitly disposable test database and a synthetic seed for API tests. Automate migration up/down/up and owner-scoped activity/search/gap/large-ID checks on actual PostGIS; retain the existing SQLite unit tests. Add CI for PoC tests, backend tests/integration and mobile typecheck using locked dependencies. Install database packages on the CI runner without requiring production containers. Do not change runtime API/schema or provision cloud resources. Evidence: clean documented setup and passing checks; if hosted CI cannot be executed, mark it unverified rather than treating YAML as success. Own only the paths assigned above.

**D02 — verify the existing native shell**

Read `apps/mobile/README.md` and verify the current MapLibre shell before adding features. Inventory Android SDK/JDK/emulator, CocoaPods, Xcode and simulator runtimes. This host had Xcode 26.5 but no installed simulator runtime, CocoaPods or Android tools; disk cleanup recovered roughly 117 GB. Record the actual present state, exact proposed downloads and commands. Installation is a separate authorized step. With an available/authorized emulator or device, create Android and iOS development builds, launch them, exercise selection/back/zoom/error/empty states and correct only build blockers. MapLibre cannot run in Expo Go. Record exact build/device/OS and failures; JS bundles/typecheck alone do not complete D02. No backend or product-feature implementation.

**D03 — mandatory-provider feasibility**

Create one dossier from current official Garmin/Strava sources: registration prerequisites, approved access, actual endpoints/scopes, history/backfill boundaries, notifications, token lifecycle, capacity/rates, retention/derived-progress rights and attribution. Explain which facts are public, which need dashboard/account tests, and which need written permission. Specify the exact permanent OSM-node-progress use case and sanitized questions for the owner to submit. Do not contact providers, enter credentials or announce supported integrations. Completion is a reviewable dossier; provider approval remains open. Keep under one document and cite primary sources.

### First next-wave briefs

**D04:** specify minimal accounts/identities/sessions and separately allocated activity/source IDs; additive Alembic migration plan; typed existing list/detail responses; private import/job/geography response examples. Reserve migration ownership and settle session transport for native/browser consumers. No speculative framework. Reviewer verifies two-account/provider-ID collision and stale-job deletion cases before D05/D09/D11 dispatch.

**D09:** implement one PostgreSQL job table and worker command with claim/lease, priority, capped retry and cancellation using D04's schema. Demonstrate two workers cannot process one lease concurrently and a crashed lease is recovered. Do not add providers, maps or Redis.

**D11:** import original nodes/ways/boundaries from a configurable regional public dataset; implement documented street eligibility/grouping and indexed lookup. Use synthetic boundary/way fixtures and one public city validation. No private GPS query or matching logic. Coordinate its migration with D09.

## Verification and planning limits

M1 walkthrough: user A signs in, imports an allowed multi-city file, watches processing, sees tracks and missing nodes, reloads both phones, and sees the same counts. Import again without duplication; interrupt/restart a worker; user B sees no A data; delete one of two overlapping activities and verify only unsupported visits disappear. Compare selected nodes/streets against the existing PoC with the same rule/dataset. Verify pending geography and missing GPS clearly.

M2 walkthrough: find an incomplete street, inspect missing nodes, plan a pedestrian route, move/undo waypoints, save/reopen on another device, export GPX, follow foreground location, import the resulting run and inspect its contribution. Manual completion remains labelled and undoable. Test offline/reconnect and denied location permission.

M3 walkthrough: approved real provider account connects, available-history counts/limits are reconciled, a new completed activity updates both apps, editing/deleting/revoking removes the correct source contribution, and reconnect does not duplicate history. Health imports require actual supported devices. Native UI acceptance includes both phones and tablets; desktop acceptance later includes all declared OS packages. Browser tests cover browser behaviour only.

Proposed benchmark after correctness: a synthetic account with 5,000 activities/5 million GPS samples across supported cities and 20 concurrent users; record actual dataset, device, VM and network. Target warm viewport p95 under 1 second server-side with a bounded 1 MB compressed response or explicit pagination/tiles. New ordinary activity visibility target remains two minutes after a usable provider event under the agreed workload; exclude provider throttling/outage from that target and measure it separately. These are budgets to test, not achieved performance. Protect interactive requests from backfill CPU/memory and remeasure before expanding regions.

Existing engineering-day estimates are rough and predate the merged foundations. Do not convert “three agents” into a threefold speed guarantee. After M1 record task completion/review/rework time and resource use, then re-estimate M2/M3. External approval time and native/store review are separate. Measure cost per imported history and active user before committing to a lower paid price.

Owner decisions before the relevant task: provider registrations/permission; native SDK download approval; initial regional coverage and final eligibility; tile/routing service; license and supported OS versions; staging/cloud provisioning; premium features/trial trigger/payment strategy; preview/agent budget. None blocks writing or executing unrelated accepted development tasks. Jira is unnecessary: D task status + PR/commit + evidence in this repo is enough initially.
