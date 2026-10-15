# Personal analytics datasets

D44 backend contract. The reviewed reader and atomic publisher are implemented.
The opt-in D45 identity/worker/lifecycle integration and D46 Insights entry are implemented; see [operations](analytics-operations.md).

All rows belong to the authenticated account through an immutable database role.
Only retained, successfully imported GPX/FIT activities qualify. A logical activity
with multiple file formats appears once. Activities with any unsupported provider
provenance are excluded, including their derived coverage. There are no raw tracks,
coordinates, original files, credentials or unrestricted application tables.

| Table | Grain | Meaning |
| --- | --- | --- |
| `activities` | Activity | Existing nullable recorded date/type/distance/duration/pace/statistics version; `has_gpx`/`has_fit` use EXISTS semantics |
| `dataset_status` | Selected dataset, or one missing-geography row | Dataset state, coverage status/progress revision and pending/failed source/import counts |
| `street_progress` | Dataset + street + rule | City/name, eligible/threshold/visited nodes, separate GPS/manual/effective completion |
| `exploration_history` | Activity + dataset + rule | Supported/new nodes and newly advanced/completed streets; provenance/date status |
| `activity_nodes` | Activity + dataset + opaque visited-node token | Distinct selected-period support, recorded date and first-visit attribution |
| `activity_street_nodes` | Activity + dataset + street + opaque visited-node token | Distinct selected-period street support/advancement |

Node tokens are HMACs bound to owner and dataset. They support equality/union only;
they expose no OSM node IDs, coordinates, visit order or unvisited geography. The
support tables contain data only when coverage is ready. They are required because
summing activity-level counts double-counts overlaps. They are not map datasets.

Use one explicit dataset and rule per progress/history query. Rules are `normal`
(all eligible nodes below ten, otherwise ceil(90%)) or `strict` (all eligible nodes).
Never sum both rule rows. Manual completion changes effective current completion
only, never GPS totals or historical credit. Importing/incomplete geography and
pending/failed coverage produce null GPS metrics, not zero. Missing geography
leaves activity statistics available.

Dates are validated ISO calendar dates. Periods use `[from_date,to_date)`; no
timezone or timestamp is invented. Unknown dates stay null. An unknown contributing
activity date makes all chronological/new attribution unavailable; an unknown
noncontributing activity affects only its own history row. Same-day attribution
uses numeric activity ID and is approximate. Historical growth is reconstructed
from retained current provenance, not an immutable observation log: backdated
imports, corrections, deletion and dataset changes can change it.

Distance is metres, duration seconds and pace seconds/km. Unknown statistics stay
null. Pace is not additive; weighted pace uses total duration / (total distance / 1000)
over rows with both known and positive distance. IDs are PostgreSQL BIGINTs; clients
must preserve them as strings. Join activity tables by activity ID and progress by
dataset/street/rule. Never join raw support rows into activity sums.

```sql
-- Recorded totals, with unknown-value counts kept visible.
SELECT count(*) AS activities, sum(distance_m) AS distance_m,
       count(*) FILTER (WHERE distance_m IS NULL) AS unknown_distance,
       sum(duration_s) AS duration_s,
       count(*) FILTER (WHERE duration_s IS NULL) AS unknown_duration
FROM analytics.activities
WHERE recorded_date >= DATE '2026-10-01'
  AND recorded_date < DATE '2026-11-01' AND has_gpx;

-- Supported during this period versus first visited during this period.
SELECT count(DISTINCT node_token) AS period_supported_nodes,
       count(DISTINCT node_token) FILTER (WHERE new_with_activity) AS period_new_nodes
FROM analytics.activity_nodes
WHERE dataset_id = 1 AND recorded_date >= DATE '2026-10-01'
  AND recorded_date < DATE '2026-11-01';

-- A street advanced by several activities still counts once in the period.
SELECT count(DISTINCT street_id) AS period_streets_advanced
FROM analytics.activity_street_nodes
WHERE dataset_id = 1 AND new_with_activity
  AND recorded_date >= DATE '2026-10-01' AND recorded_date < DATE '2026-11-01';
```

Read `dataset_status` and `history_status` alongside aggregates; an empty support
table or SQL `count(*)=0` does not establish readiness. If any selected history row
has unknown chronology, new-period totals are unavailable, even if SQL can sum
the remaining known values. Source filters select activity IDs before joining
support/history; first-visit attribution still compares against all earlier
retained approved history, not just the chosen source subset.

A snapshot reads one repeatable-read source version and records the existing sync
revision. Limits are 10,000 eligible activities, 10,000 eligible streets, 200,000
activity-node relations and 200,000 activity-street-node relations; overflow rejects
the whole snapshot. Each explicit query has a 1.5-second statement limit and the
snapshot a 30-second deadline. Publication must recheck account/dataset/source
revision and lease under the owner fence; stale or partially published data must
never be served. Raw parsing, matching and statistics computation remain in the
existing import pipeline.

Publication stages one owner transaction in the isolated database, then briefly holds
an exclusive application owner fence and shared geography-revision fence while
checking lease, selected dataset, generation and source revision. The external
commit precedes the application ready marker; a crash between them remains
unavailable. Every result-release validation takes the same source/geography
fences and checks both committed generation markers. Failure fences survive a
refresh. Policy provisioning commits separately, so normal refreshes hold no
policy DDL locks. Sixteen synthetic PostGIS tests cover metric parity, provider
exclusion, two-owner RLS, concurrent reads, and source/deletion publication races.
