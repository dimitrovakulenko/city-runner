# Garmin and Strava feasibility

Researched 5 October 2026; this records public documentation, not approval. Required use: a user connects a provider after account creation; their lifetime GPS history becomes permanent OSM-node completion and street progress, visible only to them. The app is open source. Support agents use synthetic diagnostics only; provider data, derived progress, credentials and activity files stay out of agent context.

## Status

| Provider | Public facts | Account/evaluation or written checks | Current status |
| --- | --- | --- | --- |
| Garmin Connect Activity API | Business program, approval before evaluation, OAuth 2.0, FIT/GPX/TCX activity files, push or ping/pull, backfill tooling. | Acceptance and production access; portal paths/scopes, event/retry and token details; real account's oldest history/GPS availability; quotas and retention/deletion terms. | No approval or real-account sync verified. Personal-password connector experiment is not production access. |
| Strava API | Registered OAuth app, activity and stream endpoints, webhooks, single-player start, tiered capacity/rates. API Policy effective 1 June 2026. | Dashboard tier/scopes/quotas, owner-history completeness, app review, and written answer on permanent derived progress and agent boundary. | Published policy appears to conflict with permanent derived progress. No-go pending explicit written permission/agreement or policy change. |

## Garmin

**Access and API.** The Garmin Connect Developer Program is for business use and requires application review. Garmin says it confirms application status within two business days; evaluation access follows approval, and typical integration takes one to four weeks. Some metrics may incur fees or commercial minimums. Activity API is REST/OAuth 2.0, with configurable feeds, push or ping/pull, and FIT/GPX/TCX files. User consent and a Garmin Connect sync precede availability. Public docs omit endpoint paths, scopes, notification payloads and production quotas. [Program FAQ](https://developer.garmin.com/gc-developer-program/program-faq/), [Activity API](https://developer.garmin.com/gc-developer-program/activity-api/).

**History and lifecycle.** Garmin advertises tools for sample data, backfill and integration verification, but publishes no lifetime-history guarantee or backfill limits. After approval, test oldest date/count, GPS/file coverage, throughput, new activity delivery, edits/deletes, revocation and missed-event recovery with the owner's account. Public docs omit token expiry/refresh details, quotas and permanent retention rights; confirm them in the portal/agreement. Confirm deletion deadlines for source files, normalized GPS, node visits, summaries and backups after source deletion, disconnect and account deletion.

**Attribution.** Garmin's [API Brand Guidelines](https://developer.garmin.com/brand-guidelines/api-brand-guidelines/) require “Garmin [device model]” attribution on primary and detailed data views, including derived/combined uses materially influenced by device data; if model is unknown, show Garmin. This applies to progress views derived from Garmin activity. Follow the guidelines for exports and integrations too. Obtain production terms before release.

## Strava

**Access and endpoints.** Register an app in [Strava API settings](https://www.strava.com/settings/api); an active Strava subscription is a prerequisite. New apps are single-player. Dashboard upgrade supports 10 athletes; beyond that, review and increased access are discretionary. OAuth 2.0 scopes: `activity:read` for Everyone/Followers, `activity:read_all` also for Only Me. Read activities at `GET /api/v3/athlete/activities` (date filters and page), details at `GET /api/v3/activities/{id}`, streams at `GET /api/v3/activities/{id}/streams`. Request only read scopes; private owner-only maps likely require `activity:read_all`, to confirm in the registered app. [Getting Started](https://developers.strava.com/docs/getting-started/), [Authentication](https://developers.strava.com/docs/authentication/), [API Reference](https://developers.strava.com/docs/reference/).

**History and automatic updates.** Pagination and date filters are documented, but no guaranteed lifetime window or complete-history promise is published. After approval, compare API dates/counts and GPS streams against the owner's export; record missing/private/no-GPS cases. Webhooks notify supported activity create/update/delete and deauthorization changes; one subscription per app, validated callback within two seconds. Fetch current activity data after events and reconcile missed events. OAuth access tokens expire after six hours; refresh through `POST /api/v3/oauth/token` and persist each newest refresh token because rotation may invalidate the prior one. Revoke access and delete local credentials on disconnect. [Webhooks](https://developers.strava.com/docs/webhooks/), [Authentication](https://developers.strava.com/docs/authentication/).

**Rates and attribution.** Public defaults: overall 200 requests/15 minutes and 2,000/day; non-upload 100/15 minutes and 1,000/day. Upgraded 10-athlete tier lists 200/15-minute and 2,000/day read limits, 400/15-minute and 4,000/day overall. Dashboard and current response headers are authoritative; larger limits require review and are not guaranteed. Use webhooks rather than routine polling. Follow current [Strava brand guidelines](https://developers.strava.com/guidelines/) and “Connect with Strava” requirements.

**Retention gate.** Strava's [API Policy §§5.3–5.5, 6.2–6.3](https://www.strava.com/legal/api_policy), effective 1 June 2026, restricts AI use of Strava data and derived data, bars persistent indexes of either, limits cache to seven days, and requires user deletions to be reflected within 48 hours. It also limits display to the authenticated user. City Runner's permanent node hits/street progress are derived from GPS and stored for retrieval, so they appear incompatible with §§5.5–6.2 even when owner-only. That is our reading of the published policy, not an established exception; owner consent does not resolve it. Do not hash, aggregate, unlink or relabel the data to evade the restriction. Ask Strava for an explicit written permission/agreement naming fields, purposes, retention, deletion, owner-only display, open-source distribution and synthetic-agent boundary. Without it, do not promise permanent Strava-derived progress. The policy's MCP provision is for personal use and is not a third-party app integration substitute.

## Owner's submission checklist

Draft questions only; no provider was contacted, no application/dashboard was submitted or changed, and no credentials were entered. Never include secrets in support requests.

**Garmin**

- Is this open-source, user-facing walking/running street-exploration app eligible? What company, privacy and user-count information is required?
- Provide evaluation/production endpoint and scope docs, OAuth refresh/revocation, event payload/retries, quotas and any acknowledgement rules.
- For one consenting account, what is the oldest backfillable activity, total limit, GPS/file coverage and throughput limit? What update/delete/revocation events are supported?
- May we retain source GPS and permanent per-user OSM-node/street progress? State retention and deletion requirements for disconnect, source edits/deletion, account deletion and backups.
- Confirm fees/review steps and required Garmin/device attribution on activity and derived-progress screens and exports.

**Strava**

- Do §§5.5/6.2 allow permanent per-user OSM-node hits and lifetime street completion derived from imported GPS? Name permitted fields, use and retention in writing.
- If prohibited by current policy, can Strava grant written permission or an agreement overriding relevant limits? What review and deletion controls apply? Is this owner-only street-exploration app permitted under §5.2?
- Is the synthetic-only support workflow sufficient under §5.3 when no Strava data or derived progress enters agents, logs, screenshots or issue reports?
- Confirm permitted scopes/history/streams, current tier/rates, webhook and revocation behavior, deletion timing, and required attribution/app presentation.

## Go/no-go evidence

1. Keep provider decision, agreement/policy version, tier, production access, scopes, quotas and attribution requirements.
2. After approval, measure oldest/newest activity, counts vs owner export, GPS availability, event-to-fetch, token refresh/revocation, edit/delete and missed-event recovery.
3. Record written source/derived retention and deletion behavior for disconnect, source deletion, account deletion and backup restore. Strava remains no-go for permanent derived progress until the policy conflict is resolved in writing.
4. Keep support-agent fixtures and diagnostics synthetic and scrubbed.
