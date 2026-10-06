# Backend contract: first production slice

D04, updated 6 October 2026. This contract describes planned tables/endpoints unless explicitly marked implemented. Activity list/detail have Pydantic response models in `backend/app/schemas.py`, exposed by OpenAPI. D05 identity/sessions, D09 jobs/worker, D10 private GPX uploads, D11 shared OSM import, D12 coverage matching and D13 map/progress endpoints are implemented. D06 generates mobile activity/auth types from OpenAPI; native login and secure session storage remain pending. Migrations form one chain through `0007_map`. Review schema changes before dispatching dependent work.

## Compatibility

Keep `/api/activities?page=1&page_size=20&q=...` and `/api/activities/{id}`. Page size is 1–100; search is at most 200 characters, case-insensitive across name/date/type. Preserve current unknown-date-last ordering, then descending date/id. Unknown date/type is `"unknown"`. Detail IDs remain strings; the path accepts an integer, including legacy negative and very large IDs. Coordinates are `[longitude, latitude]`; segmented tracks and aligned nullable timestamp strings survive round trips. Bounds are two coordinates or null. Rendering clients may discard singleton segments; processing must keep their samples.

The original PostgreSQL migration allocates BIGSERIAL activity IDs. Future ingestion must use that internal sequence, never a provider's activity ID. Existing manually inserted positive IDs require sequence synchronization to the greatest positive ID before ingestion; negative fixtures remain valid. Provider IDs are stored separately as text. A provider's `123` for two accounts, or Garmin `123` and Strava `123`, cannot collide with internal IDs.

Errors retain FastAPI's existing `{ "detail": ... }` shape. Invalid query/body: 422; missing session: 401; absent/other-owner resource: 404. Structured domain error codes can be added by the feature task without removing `detail`. Existing endpoints must not expose owner identifiers, source credentials or complete histories through a map request.

## Accounts and sessions: D05 / migration 0002

Use additive tables. Account IDs remain strings compatible with existing `activities.user_id`; new accounts use server-generated UUID strings. Insert an account for each distinct legacy `activities.user_id`, then add its foreign key. Never reinterpret seeded `alice`/`bob` as a real Google subject.

| Table | Minimum fields / constraints |
| --- | --- |
| `accounts` | `id VARCHAR(255) PRIMARY KEY`, `created_at TIMESTAMPTZ NOT NULL` |
| `login_identities` | `provider`, `subject`, `account_id` FK; primary key `(provider, subject)`; provider limited to Google/Apple; do not use email as identity |
| `login_challenges` | random `id`, `provider`, cryptographically random 64-hex-character `nonce`, `expires_at`, `consumed_at`; one successful exchange, five-minute lifetime |
| `sessions` | `token_digest CHAR(64) PRIMARY KEY`, `account_id` FK, `created_at`, `expires_at`, `revoked_at`; index account; only SHA-256 digest persisted |

Transport: native clients send `Authorization: Bearer <opaque-session-token>`. Generate at least 32 random bytes; issue a server session for 30 days, configurable within bounded limits. No refresh token machinery yet. Expired/revoked sessions return 401 and the client signs in again. Future browser consumers can use explicit bearer transport initially; cookies require a separately reviewed CSRF policy. Do not put session tokens in URLs, logs or generated API examples.

| Endpoint | Request / response |
| --- | --- |
| `POST /api/auth/challenges` | `{provider:"google"|"apple"}` → `{id, nonce, expires_at}`. Nonce is the exact value native auth passes to the provider. Disabled/unconfigured provider rejects the request. |
| `POST /api/auth/exchange` | `{challenge_id, id_token}` → `{token, expires_at, account:{id}}`; verify signed provider identity, nonce, issuer, configured audience, expiry and nonempty subject before consuming challenge/creating session atomically |
| `GET /api/me` | Valid bearer → `{id}`; no client-provided account selector |
| `DELETE /api/auth/session` | Valid bearer → 204; revoke only that session, subsequent access is 401 |

For Google and Apple, pass the returned 64-hex nonce unchanged to the provider request. Apple's `expo-apple-authentication.signInAsync({nonce})` receives that exact string; do not add a Firebase-style raw-nonce/hash transformation. The signed ID-token `nonce` must equal the challenge value. D05 rejects missing/transformed nonce and D08 tests the same convention on device. Consume the challenge with an atomic unused-and-unexpired conditional update in the session-creation transaction; two concurrent exchanges cannot both succeed. Concurrent first logins for one provider subject must resolve to one account without orphan accounts. Google client flows use supported native identity SDKs or authorization-code/PKCE browser flow, with state validated by the client. No embedded browser/password collection and no client secret in the app. [Expo Apple nonce options](https://docs.expo.dev/versions/latest/sdk/apple-authentication/).

Use established JWT/OIDC verification libraries and provider-controlled HTTPS keys. Configure explicit allowed Google/Apple audiences; never choose trusted issuer/key URL from an unverified token. Missing configuration fails closed. Keep the old injected identity resolver only as an explicit test hook; ordinary deployment resolves every request from its bearer session. Regression fixtures test valid/invalid signatures, issuer/audience/expiry/nonce, replay, account isolation and revocation. Real-provider acceptance requires registered native app IDs/credentials and remains separate from signed synthetic tests. [Google OIDC](https://developers.google.com/identity/openid-connect/openid-connect), [Apple verification](https://developer.apple.com/documentation/signinwithapple/verifying-a-user).

D08 adds authenticated identity linking: verify a new challenge and identity before attaching it to the current account; reject identities owned by another account. Same emails never merge accounts. Account deletion and provider credential revocation belong to their later tasks.

## Durable jobs: D09 / migration 0003

One `jobs` table: allocated numeric `id`, `account_id` FK, `kind`, `dedupe_key`, JSON `payload`, `priority`, `status`, `attempts`, `max_attempts`, `available_at`, `lease_token`, `leased_until`, sanitized `last_error`, created/updated timestamps. Unique `(account_id, kind, dedupe_key)`; index claimable status/availability/priority. States: `queued`, `running`, `succeeded`, `failed`, `cancelled`.

New work priority 100; history 10. Claim atomically using PostgreSQL row locking/`SKIP LOCKED`; do not hold a transaction while executing a handler. Every claim gets a random lease token and increments attempts; default five attempts. Complete/fail/extend only with the current unexpired lease token. Expired leases become claimable only within the attempt limit; an exhausted crashed job becomes failed instead of living forever. Capped exponential backoff for transient failures; explicit permanent failure/cancellation. Tests cover two-worker claims, expired lease recovery, stale acknowledgements and attempts exhaustion.

Export plain enqueue/claim/complete/fail/cancel functions plus a worker module command. No Redis, generic distributed framework or provider handlers. A synthetic task handler may prove execution; do not report imports from it. D10 atomically creates upload metadata and enqueues `process_upload`. IDs in job payloads point to owned records, not file paths/credentials from arbitrary callers. Feature handlers load and compare the source revision before writing; queue deduplication alone cannot prevent stale resurrection.

## Sources and GPX ingestion: D10 / migration 0005

Migration `0005_sources` follows `0004_geography`; this reserves a single linear migration chain while the two modules are developed in parallel.

`activity_sources`: allocated `id`, `account_id` FK, `activity_id`, `source_kind`, `source_connection_id` nullable, external ID nullable, content hash nullable, revision, private object key nullable, status and timestamps. Add unique `(user_id,id)` on activities and composite FK `(account_id,activity_id)` to it, plus unique `(account_id,id)` on sources. Later provider connections use the same composite ownership constraint; independent account/activity FKs are insufficient. For connection imports enforce uniqueness of connection/external ID; for identical file uploads enforce account/file-kind/content hash uniqueness. Raw objects are private local files in development and private S3 in hosting. Generate object keys server-side; never accept absolute paths. Keep original sample input; geometry simplification only changes display output.

`POST /api/uploads` accepts one GPX multipart file (10 MiB, 100,000 points) and returns 202 `{id,status,job_id,duplicate}`; IDs are strings. Exact duplicate returns the same source/job for that account. `GET /api/uploads/{id}` returns owned queued/processing/succeeded/failed/cancelled state, activity ID when available and a safe error. Parser imports recorded tracks, preserves segments/aligned timestamps and records invalid/missing timestamps as null. Planned routes do not count as recorded activities. Final source/activity writes and completion share an unexpired-lease/source-revision check and transaction; ingestion leaves `activities.processed=false` until matching. See [GPX import](gpx-import.md) for storage, body bounds and crash-orphan limits. D09 jobs do not need to know source columns.

## Shared geography: D11 / migration 0004

| Table | Minimum fields / constraints |
| --- | --- |
| `map_datasets` | allocated `id`, region, source timestamp/checksum, eligibility-rule version, selected-city config hash, coverage mode/evidence, validated timestamp, `status` (importing/active/retired); unique active version per region |
| `cities` | allocated `id`, dataset FK, original OSM relation ID, name, boundary `MULTIPOLYGON` SRID4326; unique dataset/relation |
| `osm_nodes` | dataset FK, original OSM node ID, point `geography(Point,4326)`; primary key dataset/node; GiST spatial index |
| `osm_ways` | dataset FK, original OSM way ID, name, tags, segmented geometry; primary key dataset/way |
| `streets` | allocated `id`, city FK, normalized name, display name, eligible-node count; unique city/normalized name |
| `street_ways` / `street_nodes` | join street to original dataset/way or dataset/node; unique membership, no shared-node duplication |

Membership uses composite dataset foreign keys. Exact OSM names remain distinct per city; no case/whitespace normalization is applied under rule v1. Dataset identity includes sorted city selection and coverage mode. Sampled datasets stay staged and cannot supply complete-city denominators; complete mode records operator evidence, with structural validation and nonempty eligible streets. See [OSM import](osm-import.md) for input completeness, public snapshot evidence and unsupported formats.

Retain the PoC's documented eligible-way policy and city/name grouping initially; document disconnected names and border cases. Never synthesize nodes, copy shared geography per account or treat inaccessible ways as required without an explicit eligibility rule. Public regional import is independent of private GPS. Version IDs are returned with future coverage queries; unsupported regions remain pending. D11 provides indexed city/boundary and nearby-node lookup primitives, not activity matching. D11 may activate the first validated dataset before any user coverage exists. Replacement datasets remain importing until D12 has staged their contributions/summaries; one transactional active-version switch selects matching geography and progress. If a user's new-version progress is not ready, APIs explicitly return coverage pending and do not reuse old-version counts with new denominators.

## Coverage and maps: D12/D13

D12 adds unique `(account_id, source_id, source_revision, dataset_id, node_id)` contributions, with a composite `(account_id,source_id,source_revision)` FK to sources and `(dataset_id,node_id)` FK to shared nodes; summaries are rebuildable. Work commits visits/status/progress revision together and ignores obsolete source revisions. An activity is processed only after applicable matching finishes; missing geography is reported separately. Deletion removes only the relevant source contribution; account visits are the distinct union of remaining sources. Do not implement permanent Strava-derived storage without its approved policy outcome.

Exact current source/run/job state gates readiness; source deletion and revision changes invalidate cached summaries, and viewport/progress reads derive counts from current successful contributions. Upload ingestion atomically enqueues matching for active complete datasets; bounded CLI requeue can also match staged complete replacements. Replacement activation remains deferred. See [coverage matching](coverage.md).

Implemented: `GET /api/map?bbox=west,south,east,north&zoom=...` and `/api/progress`, with typed responses, account isolation, a consistent version snapshot and explicit work/output limits. See [map API](map-api.md). Future D15 endpoints: `GET /api/cities`, `/api/cities/{id}/streets`, `/api/streets/{id}`, `/api/streets/{id}/contributions`; their task supplies pagination/filter schemas before generating mobile clients. Date/type/source filters label period views separately from lifetime totals.

The existing `activities.location` point is not a replacement for segmented tracks or visited-node geography. D13 adds indexed `activities.track_geometry`, backfilled and maintained from separate track segments/singletons for display only. Original samples/timestamps remain unchanged. Activity dates remain compatible until ingestion adds a nullable typed start time; do not silently convert unknown dates into today's date.
