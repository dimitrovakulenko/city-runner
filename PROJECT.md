# City Runner project requirements

Updated 5 October 2026. The spike has demonstrated one real Strava GPX import and partial street coverage; real Garmin account sync remains unverified. [BUILD_PLAN.md](BUILD_PLAN.md) defines competitor scope, architecture, data constraints and the new report/fix workflow. [POC.md](POC.md) records experiment evidence; [BACKLOG.md](BACKLOG.md) tracks delivery. Production application development has not started.

Build an open-source street exploration service for Android, iPhone, iPad, Android tablets, Windows, Linux, and macOS. Android and iPhone are the first delivery priority; iPad/tablet support belongs to the mobile release, and desktop packages are a required following stage. Mandatory provider imports and automatic updates are part of the product, not optional polish. Use repo documentation as the project tracker initially. Move the same requirement IDs to an issue tracker when collaboration needs it.

## Agreed scope

- Android and iPhone first, with explicit iPadOS and Android tablet support. Windows, Linux, and macOS applications are also required; browser access alone does not fulfill desktop distribution.
- Use existing OSM nodes initially. Do not generate extra checkpoints or implement precise road segment matching for the first release.
- Route planning, lifetime GPS maps, and visited/missing node displays are free initially.
- Fast Google signup and login are mandatory. Connecting an activity provider happens after account creation and does not create another account.
- Smooth Garmin and Strava connection, historical import, and automatic sync are mandatory. Garmin is the first PoC integration; Apple Health and Health Connect cover phone workout data. Other provider priorities need confirmation.
- Support a 14-day trial for future paid features and Stripe payments later. Free tools remain available after a trial ends. The paid feature set is undecided; EUR 30/year or EUR 3/month remains a pricing proposal.
- Deployment: AWS, Terraform, one Lightsail application machine with systemd-managed processes and private S3 storage. No Lambda or containers required. Later isolated agent/preview execution needs separate disposable compute; it cannot share production privileges.
- Built-in support: users report issues, an agent attempts eligible fixes, users verify the exact preview revision, required checks pass, and a trusted controller merges automatically. Reports are private; production credentials and provider-derived GPS data are not agent inputs. Implementation follows the useful core application and preview/test infrastructure.

## User journey

The experiment has demonstrated the file-to-map path with a real run. Keep available-history and automatic Garmin sync verification as outstanding integration work while developing independent application features. Single-user password authentication is sufficient only for the local experiment; verified identity and per-user ownership checks precede a shared remote beta. Production cloud access remains a launch gate. PoC acceptance and limitations are in [POC.md](POC.md).

Sign in with Google, connect a provider, see import progress, then see GPS history and completed/unfinished streets. Plan a walking or running route using missing nodes, save/export it, finish an activity in an existing tracker, and see progress update after the provider receives that activity.

Health and provider permissions are optional until that source is connected. Denied permissions, missing GPS, or a disconnected provider must not prevent account access or use of existing permitted data.

## Requirements and acceptance

| ID | Requirement | Acceptance |
| --- | --- | --- |
| CR-01 | Google signup and login | First verified Google login creates one account; later logins return to it on all supported platforms. Cancelled/expired login has a clear retry. Backend verifies identity tokens and issues its own sessions. |
| CR-02 | Apple login and account linking | Offer Sign in with Apple on iOS to satisfy the equivalent login requirement. Link identities from an authenticated account; never merge accounts merely because emails match. Logout and deletion revoke sessions. |
| CR-03 | Provider connection | Connect/disconnect from settings; handle cancelled consent, expired tokens, revoked permissions, and reconnect. Keep login identities separate from workout connections. |
| CR-04 | Historical import | Paginate all permitted available history, prioritize recent activities, show processed/queued/failed counts and history limits, resume after restart, and retry failures. No claim of unlimited history without provider verification. |
| CR-05 | Automatic sync | Queue provider events, fetch activity data, process it, and update all supported clients without manual file uploads. Support edits, deletions, deduplication, rate limits, and reconciliation after missed events. |
| CR-06 | Native health imports | iPhone handles late/updated HealthKit routes. Android exposes foreground Health Connect sync and consent requests. Mark workouts without GPS clearly; do not count them toward streets. |
| CR-07 | File imports | Bulk GPX/FIT import with file limits, validation, duplicate detection, progress, and retries. This complements mandatory provider integrations rather than replacing them. |
| CR-08 | OSM completion | Within 25 metres of an actual GPS sample counts a node. Normal mode requires 90% of eligible street nodes, or all nodes for streets with fewer than ten. Support 100% mode. Exclude streets with zero eligible nodes from percentages. |
| CR-09 | Maps and progress | Lifetime tracks, city percentages, partial/completed streets, missing nodes at close zoom, street search, current location, date/type filters, and activity contribution details. Fetch only visible geography and simplify displayed tracks without altering matching input. |
| CR-10 | Free route planner | Select waypoints over an unfinished-street/node overlay, route using pedestrian-accessible roads, display distance, edit/save, reopen on another device, and export GPX. Automatic optimal exploration loops are later scope. |
| CR-11 | Phone experience | Native map rendering, usable street detail sheets, persistent map position, cached progress with a stale-data indicator, reconnect handling, and resumable uploads. Full downloaded offline basemaps are later scope. |
| CR-12 | Stripe and entitlements | Before paid release, support hosted Checkout and customer subscription management. Verify webhook signatures; handle repeated/out-of-order events, renewal, cancellation, payment failure, and refunds. Entitlements are server controlled and shared across devices. |
| CR-13 | Trial and free access | A server records one 14-day trial per account. Trial start trigger and premium features remain undecided. Expiry must not disable free planning, maps, or coverage. |
| CR-14 | Privacy and deletion | Private tracks by default; consent before health-route upload. Export permitted data. Deleting an activity removes its contribution while preserving visits supported by other activities. Account deletion removes data and credentials under applicable retention rules. |
| CR-15 | Operations and release | HTTPS, private database/files, credential protection, bounded jobs, backups with a successful restore drill, failure visibility, signed builds, real-device verification, and store disclosures. Document self-hosting and select an open-source license before public distribution. |
| CR-16 | Tablet experience | Ship an iPhone/iPad app and an Android app that supports tablets. Verify the user's iPad Air, portrait/landscape, resized windows where supported, readable map/list layouts, keyboard/pointer interaction, login, planning, file import/export, and synced progress. Health imports depend on actual device/OS capabilities. |
| CR-17 | Desktop applications | Package installable applications for Windows, Linux, and macOS. Support the same account, provider connections, maps, planning, file import/export, subscription access, and live progress refresh. Desktop health-store imports are not a parity requirement. |
| CR-18 | Platform distribution | Define supported OS/CPU combinations, automate platform builds, protect release credentials, sign/notarize where applicable, document install/uninstall and updates, and verify installation/login/map performance on each supported OS. A package build alone is not release acceptance. |
| CR-19 | Activity explorer and contributions | Paginated/searchable activities, date/type/source filters, selected-activity map, source/import status and contributing activities per street. Preserve source timestamps; missing metadata is shown as unknown. Define chronological attribution before displaying new coverage per run. Distinguish period and lifetime coverage. |
| CR-20 | Manual completion and corrections | Mark/undo a street as manually completed with a reason; keep manual and GPS-derived status separate. Report inaccessible nodes or map errors without inventing activity GPS. Account deletion removes overrides. |
| CR-21 | In-app reports and support | `/support/reportIssue` collects expected/actual behaviour, reproduction steps, version and permitted diagnostics. Reports/attachments are private and owner-scoped; raw tracks/tokens/provider responses are excluded by default. Users track progress, verify a preview or report still broken without a GitHub account. |
| CR-22 | Agent fix, preview and automatic landing | Eligible code defect is reproduced in isolated compute, fixed with regression evidence and presented as a specific preview commit/build. Reporter verification, required tests and independent trusted scope checks gate auto-merge. New revisions invalidate approval. Sensitive changes require maintainer review; no production data/secrets reach generated code. Show merged/deployed/native-release status separately and support reopening. |

OSM node coverage is approximate. GPS tracks show recorded positions, not exact ground truth; node completion does not establish precisely how much of each street's length was traversed. Experimental eligibility/grouping and boundary rules are documented in [POC.md](POC.md). Production rules and manual exclusions still need validation before release.

## Automatic and live sync

Three different behaviours must be distinguished:

- Provider sync: automatic updates after an activity is saved and reaches Strava/Garmin. It continues when our phone app is closed, where the provider API permits it.
- App refresh: show changed progress in an open app through a lightweight status stream or short foreground refresh; reload on reconnect/foreground.
- During a run: current location over cached missing nodes is first-release scope. Recording a workout or continuously importing another tracker during recording is not agreed scope.

Proposed acceptance target: 95% of ordinary new activities become visible within two minutes after a usable provider event reaches our server, under a documented beta workload and without provider throttling/outage. Measure provider arrival, queue delay, processing time, and app refresh separately. This is a target to benchmark, not a provider guarantee or a backfill deadline. New events must not wait behind historical imports.

## Provider constraints and launch gates

Verified against official sources on 5 October 2026. These are external dependencies, not completed approvals.

| Provider | Required behaviour | Current constraint |
| --- | --- | --- |
| Strava | OAuth, permitted history, activity events, edits/deletions, token refresh | Technical webhooks are available. Published policy limits caching to seven days and prohibits persistent indexes containing Strava or derived data. Our interpretation is that permanent lifetime progress needs explicit permission/clarification. Capacity increases also require review. Mandatory integration stays a launch gate; do not quietly defer it. |
| Garmin | OAuth, activity GPS/files, notification-driven import, verified backfill | Business program approval and actual historical access must be established. Test Activity API access before promising Garmin support. |
| Apple Health | Available historical workout routes and subsequent updates | Routes can be delayed, changed, or absent. Device execution determines when updates can be imported; no unconditional background latency promise. |
| Health Connect | Available routes, visible foreground sync, explicit consent | Android prevents background reads of exercise routes created by other apps. Cloud providers supply automatic sync for those users where available. |
| Other providers | Candidate adapters for COROS, Polar, Suunto, Wahoo | Rank demand and confirm API access, history, notifications, and retention before scheduling implementation. No popularity ranking or access assumed. |

The Garmin PoC may use the unofficial personal-account connector experimentally. It does not establish approved production access. Strava storage permission and approved Garmin access must be investigated before committing to full development. If mandatory access cannot be obtained, revisit product viability. A file-only launch would not satisfy the agreed product.

## Backend and data

Use React Native/TypeScript for phones and tablets, native Swift/Kotlin health bridges, local SQLite, and platform credential storage. Recommended desktop approach: a React/TypeScript interface bundled with Tauri, using MapLibre GL JS. Share generated API types and client logic; keep completion rules on the backend and adapt layouts, maps, storage, and platform integrations. The revised recommendation is to retain Python/FastAPI for API and worker processes instead of the earlier proposed TypeScript rewrite, reusing the spike's matching code and tests. PostgreSQL/PostGIS stores geography, visits, derived summaries, and durable jobs; private S3 holds permitted source files and database backups. Run Caddy, API, worker and database as systemd services on one Lightsail machine. Tile and pedestrian routing services are external dependencies to select and budget; avoid a global routing engine or full OSM planet import on the initial VM. Detailed modules, processing and map-query changes are in [BUILD_PLAN.md](BUILD_PLAN.md).

## Platform delivery

| Platform | Delivery stage | Recommended client and package |
| --- | --- | --- |
| Android phones | First priority | React Native and MapLibre Native; Google Play application |
| iPhone | First priority | React Native and MapLibre Native; App Store application |
| iPad | Mobile release | Same Apple app with tablet layouts; test on the user's iPad Air rather than only an iPhone simulator |
| Android tablets | Mobile release | Same Android app with tablet layouts and device testing |
| Windows | Required desktop stage | React/Tauri and MapLibre GL JS; Windows installer |
| macOS | Required desktop stage | React/Tauri and MapLibre GL JS; application/DMG with release signing and notarization |
| Linux | Required desktop stage | React/Tauri and MapLibre GL JS; initial AppImage/deb packages on a documented distro/architecture matrix |

Desktop capabilities require actual end-to-end verification: browser-based Google/provider authorization returning to the app, secure session storage, drag/drop and file access, keyboard/pointer planning, maps in each system webview, reconnect, updates, and install/uninstall. All devices share one account and subscription. Cloud activity sync happens in the backend and does not depend on keeping a particular app open. Expose native health features only where supported; iPad and desktop users can use cloud/file imports and already-synced progress. Do not promise during-run GPS features on devices without suitable location capabilities.

Store shared cities, streets, OSM nodes, street-node relations, and map versions once. Store accounts, login identities, provider connections, activities with source provenance, source contributions, activity-node hits, progress summaries, saved routes, manual overrides, and entitlements separately. Treat summaries as rebuildable. Keep permitted normalized GPS inputs for rule changes, subject to source retention requirements; do not store every GPS sample as a separate SQL row.

Keep equivalent activities linked to their source contributions: deleting a Strava contribution must not erase a separately authorized file/health contribution, and must not leave Strava-derived data behind. File hashes/provider IDs handle exact duplicates; cross-provider duplicates require explicit matching and tests. Revocation/deletion follows the source policy rather than a universal keep-data rule.

Processing: receive upload/event, persist a job, acknowledge quickly, fetch/parse the source, normalize/deduplicate, query nearby indexed OSM nodes, write visits, rebuild affected counts, publish progress status. Retry safely; display source attribution, last successful sync, queue state, and actionable failures. Apply OSM updates as versioned datasets and rebuild affected progress with an explanation of changed denominators.

Google account login and Stripe web payment satisfy different needs from workout OAuth. Add a minimal web account/billing surface for Stripe Checkout and subscription management, alongside the desktop application interface. A general browser map app is not a separate launch requirement. App Store and Google Play purchase rules vary by region and distribution; choose a compliant in-app purchase or permitted external-payment approach before store submission. Stripe alone is not an assumption of worldwide native checkout compliance.

## Delivery and unresolved decisions

Start provider feasibility alongside an activity/city/street explorer, then production identity/jobs/PostGIS, both phone apps with automatic provider sync, and a free mobile/tablet public beta with planning, overrides and private issue reporting. Add previews and trusted verification before enabling the agent/automatic merge workflow. Required Windows/Linux/macOS packages and paid features follow the mobile core; they can proceed independently. Route planning is required for public beta, and mandatory provider access must be verified before public launch. The remaining personal Garmin experiment does not block independent product work.

Remaining decisions: providers beyond mandatory Garmin/Strava, initial supported regions, production street eligibility/grouping, routing/tile vendors, sync benchmark workload, premium features, trial start, final price, license, supported OS/CPU and Linux distro versions, desktop implementation validation, store payment strategy, agent runtime/vendor, preview distribution and automation budgets. GitHub is selected for code hosting; PR/CI automation is not configured. Jira is unnecessary at this stage; requirements and backlog are the current source of truth.

## Sources

- [Google backend authentication](https://developers.google.com/identity/sign-in/android/backend-auth)
- [Apple login requirements, guideline 4.8](https://developer.apple.com/app-store/review/guidelines/#login-services)
- [Strava webhooks](https://developers.strava.com/docs/webhooks/)
- [Strava policy, sections 5.5 and 6.2](https://www.strava.com/legal/api_policy)
- [Strava capacity and review](https://developers.strava.com/docs/getting-started/)
- [Garmin program access](https://developer.garmin.com/gc-developer-program/program-faq/)
- [Garmin Activity API](https://developer.garmin.com/gc-developer-program/activity-api/)
- [HealthKit workout routes](https://developer.apple.com/documentation/healthkit/reading-route-data)
- [Android exercise routes](https://developer.android.com/health-and-fitness/health-connect/features/exercise-routes)
- [Stripe subscription Checkout](https://docs.stripe.com/payments/checkout/build-subscriptions)
- [Stripe webhooks](https://docs.stripe.com/webhooks)
- [Google Play payments policy](https://support.google.com/googleplay/android-developer/answer/9858738?hl=en)
- [React Native platform support](https://reactnative.dev/docs/out-of-tree-platforms)
- [MapLibre React Native mobile setup](https://maplibre.org/maplibre-react-native/docs/setup/getting-started/)
- [Tauri packaging and distribution](https://v2.tauri.app/distribute/)
