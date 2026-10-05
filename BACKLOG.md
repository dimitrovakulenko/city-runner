# City Runner delivery backlog

Updated 5 October 2026. Scope and acceptance are in [PROJECT.md](PROJECT.md); competitor analysis, build order and report/fix architecture are in [BUILD_PLAN.md](BUILD_PLAN.md). One real Strava GPX import/partial coverage is verified; Garmin account acceptance remains pending ([POC.md](POC.md)). Local Git is initialized. Production infrastructure, provider applications, CI, preview service and agent pipeline remain planned.

Use stable CR requirement IDs in changes and reviews. For each task, update status, remaining work, and evidence links. Done means its acceptance criteria were demonstrated, not just code written. Do not count blocked or mock-only provider support as delivered. Jira or another tracker can mirror these IDs later.

## Work estimate

Planning estimate: the existing phone and paid-release baseline is 73–125 engineering days. Explicit tablet support adds 4–7 days; required desktop UI, packaging, and verification add 18–32 days. The expanded scope totals 95–164 engineering days, approximately 19–33 full-time working weeks for one experienced engineer. Android/iPhone stay the first priority; tablets are included in the mobile release, desktop applications follow. These are order-of-magnitude estimates, not commitments. Provider review waiting time, major rework, full offline maps, advanced route optimization, global coverage, and broader provider parity are additional. Task ranges include implementation and focused verification; shared work should not be counted twice.

The spike is sufficient to begin independent product features. Production provider feasibility still needs 2–4 engineering days plus unpredictable external response time and remains a public-launch gate. PoC work overlaps import/OSM/matching investigation; do not add it again as a separate full production implementation. T20–T22 add an estimated 13–23 engineering days for reporting, isolated previews and agent/merge orchestration beyond the prior baseline, excluding already-counted ordinary CI/release work. Re-estimate the combined scope after the first production slice; these are not calendar promises.

## Next implementation slice

Build the useful explorer (CR-09, CR-19) using the current map as a baseline: parse/preserve activity metadata, paginated activity list/detail, city/street search, selected-activity track and missing-only layers. Display source/import limitations and unknown metadata honestly. T05/T11 cover this work; do not add a second estimate for the same screens. Provider feasibility proceeds alongside it. Full provider history, precise road intervals and the agent pipeline are not claimed as part of this first slice.

## First milestone: personal Garmin PoC

| Task | Status | Acceptance/evidence |
| --- | --- | --- |
| P01 Personal Garmin login/MFA, backfill, deduplication, automatic poll | Implemented; real-account verification pending | `poc/garmin_sync.py`; fixture import/retry checks pass. Needs your local login and actual available-history comparison. |
| P02 GPS-driven city discovery, OSM nodes, completion | Implemented; one real GPX partially verified | `poc/core.py`; downloaded public highway ways via geometry GET after recursive POST timeouts. Real-file matching demonstrated visited nodes and completed streets; samples outside cached cities remain unassigned. Private activity data is excluded from this repository. |
| P03 Single-user auth and browser map | Implemented; Chrome GPX upload verified | `poc/app.py`, `poc/index.html`; authenticated API/map tests pass; one real exported GPX was imported through Chrome, separately from four seeded demo activities. |
| P04 Real-account acceptance and measurements | Pending user login | Multi-city history, new activity automatic sync, restart, manual accuracy checks, elapsed time, storage, and map latency recorded. This is the milestone gate. |

Fifteen automated tests pass, including synthetic GPX metadata and legacy-database migration, file upload, validation, duplicate detection, coverage updates, OSM road eligibility/geometry, and offline processing while location-query consent is pending. One real Strava GPX export/upload is verified; this does not establish Strava account sync or Garmin acceptance. Start/run instructions and explicit PoC limitations are in [POC.md](POC.md).

## Work packages

| Task | Scope and requirements | Status | Estimate | Completion evidence |
| --- | --- | --- | --- | --- |
| T01 | Strava storage/use permission, capacity path, approved Garmin access/history, further provider priority (CR-03–06) | External access/clarification required; public API research complete, access not verified | 2–4 days | Written provider outcome, tested access/history limits, explicit go/no-go decision |
| T02 | Git repository/CI, backend/mobile workspace, local systemd or ordinary process setup, PostGIS migrations and fixture baseline (CR-15) | Local Git initialized; remaining foundation planned; no backend-language rewrite or containers required | 3–5 days | Reproducible development setup, API/worker/database running, meaningful checks passing |
| T03 | Google signup/login on both platforms, Apple login on iOS, sessions/account linking (CR-01–02) | Planned | 5–8 days | Physical-device signup/login, same account on both apps, verified token rejection and session revocation |
| T04 | Regional OSM import, eligibility/grouping rules, map versions, spatial indexes (CR-08–09) | Planned | 5–8 days | Known city/street fixtures, duplicate names/boundary cases, repeatable import/update |
| T05 | GPX/FIT ingestion, timestamps/type/distance metadata, source storage, validation, durable jobs, exact duplicates (CR-07,19) | In progress; first GPX metadata slice implemented | 4–7 days | Synthetic namespaced GPX coverage verifies original point timestamps, date/type extraction, unknown missing/invalid metadata, and additive legacy-database migration; FIT, source/job workflow, and broader validation remain |
| T06 | Node matching, 90/100% rules, contribution deletion, recalculable summaries and manual override/undo (CR-08–09,14,20) | Planned; override scope needs estimate validation | 4–7 days | GPS/node fixtures, overlap/deletion tests, updated map/rule recalculation, manual status distinguished and undo verified |
| T07 | Strava OAuth, allowed backfill, stream retrieval, webhooks, refresh/revocation (CR-03–05) | Blocked by T01; can develop fixtures meanwhile | 6–10 days | Real account history/new activity/edit/delete flows; measured sync; source-policy compliance |
| T08 | Production Garmin adapter, history/notifications, source attribution and deduplication (CR-03–05) | Depends on T01; personal PoC is separate from approved adapter | 6–10 days | Approved access and real-device activity import, verified backfill limits and reconnect |
| T09 | iPhone HealthKit route/history/update bridge (CR-06) | Planned | 4–7 days | Apple Watch/iPhone workouts, late route, missing route, denied/revoked permission |
| T10 | Android Health Connect availability/permissions/foreground routes (CR-06) | Planned | 4–7 days | Supported-device import, consent-required flow, missing data, visible background limitation |
| T11 | Activity/city/street explorer, search/filter/detail/contributions, map/settings, local cache and uploads (CR-09,11,19) | Planned; begin explorer contracts using current map | 10–18 days | Real activity list/detail and street search; both apps usable on physical devices; large-history map workload and network interruption verified |
| T12 | Free pedestrian route planner, missing-node overlay, saved routes and GPX export (CR-10) | Planned; vendor/access decision needed | 6–10 days | Plan/edit/save/reopen/export a route; verify pedestrian access and distance on fixtures |
| T13 | Sync reconciliation, cross-source deduplication/provenance, fresh app status (CR-04–05,14) | Planned | 4–7 days | Duplicate/missed/out-of-order events, throttling, source deletion, latency benchmark |
| T14 | Minimal web billing surface, Stripe Checkout/portal, entitlements and trial (CR-12–13) | Planned; premium/trial/payment decisions needed | 4–7 days | Sandbox payment/renewal/cancel/refund/replay flows; free features remain accessible after expiry |
| T15 | Terraform/Lightsail/S3, HTTPS, backups/restore, export/deletion, store release and self-hosting docs (CR-14–15) | Planned | 6–10 days | Restore drill, user-isolation checks, signed builds, review requirements and real provider flows verified |
| T16 | iPad and Android tablet layouts, rotation/resizing, keyboard/pointer, device validation (CR-16) | Planned; part of mobile release | 4–7 days | Map/planner/import/login verified on the user's iPad Air and a documented Android tablet; health features capability checked |
| T17 | React desktop UI, MapLibre GL JS, shared client logic, planning/import/sync parity (CR-01,05,09–14,17) | Planned; after mobile core | 8–14 days | Working desktop interface with real account/progress, planner, file import/export and backend updates |
| T18 | Tauri Windows/macOS/Linux packaging, OAuth return, credential storage, file integration and updates (CR-17–18) | Planned; depends on T17 | 6–10 days | Installable packages, browser auth returning to the app, protected sessions, drag/drop and tested update flow |
| T19 | Desktop build/release CI, signing/notarization, OS/CPU matrix and install/map smoke verification (CR-18) | Planned; depends on T18 | 4–8 days | Releases installed and exercised on each supported OS/architecture; documented Linux compatibility and credential management |
| T20 | Private `/support/reportIssue`, report states, safe diagnostics, attachment consent and owner-scoped status (CR-21) | Planned; before free public beta | 2–4 days | Submit/track report on phone/tablet; another user cannot access it; tokens/raw GPS excluded; permitted attachments handled |
| T21 | Isolated temporary preview database/runtime, artifact/commit identity, browser and native beta verification (CR-22) | Planned; after CI/core, before code agent | 5–9 days | Tester verifies a real preview; later revision invalidates verdict; preview has no production credentials/data and expires |
| T22 | Bounded isolated agent, reproduction/regression, trusted scope/merge gates, reporter check and release status (CR-22) | Planned; depends on T20–T21 and repository protections | 6–10 days | Seeded eligible defect reaches user-verified fix and automatic merge/deployment; failed tests/stale approval/forbidden scope cannot merge |

T07–T10 are separate source adapters. T13 covers shared reconciliation and cross-source behaviour, not a second implementation of those adapters. T08 estimates Garmin specifically; replacing it with another provider requires re-estimation. T16 covers tablet work beyond phone UI. T17–T19 cover desktop-specific UI/integration/release work and reuse the backend/provider implementations; they are not another backend project.

## Delivery checkpoints

| Checkpoint | Required outcome | Dependencies |
| --- | --- | --- |
| Personal Garmin verification | Your available Garmin history across supported cities, visible coverage, minimal auth, and a new activity automatically synced | P01–P04; your local Garmin login; independent product work can proceed |
| Integration feasibility | Mandatory cloud access, permitted storage, history limits, and provider priorities established | T01 |
| Useful explorer / working progress slice | Regional OSM + file import + node completion + activity/city/street inspection and search | T02, T04–T06 and initial T11; CR-19 |
| Private phone beta | Google login, both phone apps, mandatory cloud connections, native imports, automatic updates | T03, T07–T11, T13 |
| Free mobile and tablet public beta | Free planner/maps/coverage, manual overrides, private reports, iPad/Android tablet validation, deployment and store release | T06, T12, T16, T20 and free-release portions of T15 |
| Agent-assisted support | Report → reproduced defect → preview → reporter + CI verification → automatic merge → release status | T21–T22 after core/CI/reporting; synthetic/permitted fixtures, never production provider tokens |
| Required desktop release | Windows, Linux and macOS packages with shared accounts, maps/planning/imports and automatic progress refresh | T17–T19 and the working backend |
| Paid release | Defined premium features, 14-day trial, Stripe/web billing and compliant native payment strategy | T14 and paid-release portions of T15 |

Mandatory Strava support remains required for a launch that fulfills this project's scope. If access cannot be obtained, record the blocker and decide on scope explicitly rather than presenting file imports as equivalent.

## Later scope

- COROS, Polar, Suunto, Wahoo or other adapters after demand/access verification.
- Downloaded offline basemaps and route packages.
- Automatic routes maximizing new coverage, turn-by-turn navigation, workout recording, dedicated watch app.
- Exact covered/uncovered road intervals instead of OSM node approximation.
- Challenges, leaderboards, badges, weather, replay, and global coverage.

Free manual route planning and basic cached progress are already first-release requirements; they are not deferred by this list.
