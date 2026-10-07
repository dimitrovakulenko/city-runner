# Map and progress API

Both endpoints require the account bearer token. They use one repeatable-read database snapshot per request. IDs backed by PostgreSQL BIGINT are JSON strings.

## `GET /api/map`

Required query parameters:

```text
bbox=west,south,east,north&zoom=16
```

`bbox` uses WGS84 longitude/latitude. Coordinates must be finite and in range; west must be less than east (antimeridian-crossing boxes are rejected), south must be less than north, each dimension is at most 45 degrees, and bbox area is at most 500 square degrees. `zoom` is 0–24. `rule=normal|strict` selects street-completion threshold; default is `normal`.

The response contains owner tracks clipped to the viewport, active complete OSM city scope and street features clipped to both city boundary and viewport. At zoom 16+, ready datasets also include missing eligible OSM nodes. A street geometry may appear while its coverage status is pending, but its visited/eligible/completed values are null and no missing nodes are returned for that dataset. Unsupported viewport geography returns `geography_pending`; supported scope lists the actual intersecting cities rather than implying world coverage. Track geometry retains separate recorded segments and singleton points; no line is drawn across a recorded segment gap.

Each feature layer reports returned count, hard limit, and `truncated`. Fixed caps are 20 datasets, 100 cities, 50 activities, 200 street ways, 1,000 missing nodes, 20,000 combined display vertices, and 1 MB of geometry bytes (not a full-response byte cap). At most 51 track candidates and 201 street-way candidates enter exact intersection/clipping; features with over 20,000 source vertices are omitted and mark the layer truncated. A 1.5-second PostgreSQL statement timeout applies per statement, not per request; no p95 latency claim is implied. Track geometry is indexed on `activities.track_geometry` (GiST), backfilled from existing segmented `tracks` and maintained by a database trigger on future inserts/updates. The cache is display-only; source samples and timestamps remain unchanged.

After loading a snapshot, the importer runs `ANALYZE` on `map_datasets`, `cities`, `streets`, `street_ways`, `osm_ways`, `street_nodes`, and `osm_nodes` so viewport queries use current planner statistics. A local synthetic-account check on the public Gent snapshot (3,108 eligible streets, 51,134 nodes) measured 27–30 ms for the overview and 16–31 ms for close zoom across three repeats; these are sample timings, not a latency guarantee.

Example response shape:

```json
{
  "geography_state": "supported",
  "pending_imports": 0,
  "datasets": [{"id":"12","region":"gent","state":"ready","progress_revision":"7"}],
  "cities": [{"id":"4","dataset_id":"12","name":"Gent","bounds":[3.5,50.9,3.9,51.2]}],
  "tracks": [{"activity_id":"9007199254740993","name":"Run","date":"2026-10-05","geometry":{"type":"MultiLineString","coordinates":[]}}],
  "streets": [], "missing_nodes": [], "limits": {}
}
```

The actual response also carries bbox, zoom, node-layer state, all layer limits, and dataset coverage fields. Queued/processing imports appear as `pending_imports`, separate from matching `pending_sources`.

## `GET /api/progress`

`rule=normal|strict` is optional and defaults to `normal`. Response is bounded to 20 active complete datasets; `datasets_truncated` reports additional datasets. Each dataset includes active dataset ID, progress revision, readiness, source counts, visited-node count, and ready-only eligible/completed street and eligible-node totals. Counts and denominators are null while matching is pending or failed, so old progress cannot appear against a new dataset version. `unmapped_points` is the account activity total; `pending_imports` reports queued/processing imports separately.

Status meanings: `unsupported-geography` means no active complete dataset globally; `geography_pending` means no active city intersects this map viewport; `pending`/`failed` are matching states; `not-matched` means matching finished but produced no visited nodes despite unsupported samples; and `ready` includes a supported empty account. A ready dataset with zero sources reports zero counts. D12's current-source revision checks determine readiness; cached coverage summaries alone are not trusted.

## Activity selection

Activity list and map tracks share inclusive `date_from`/`date_to`, canonical `activity_type`, and `source=all|gpx|fit|unknown` filters. `/api/activities/filters` returns owner-scoped normalized type options. Map and progress echo canonical filters and `coverage_scope=lifetime|filtered` (default lifetime). Tracks always use the selection; lifetime coverage retains all GPS support. Filtered coverage unions selected live source/revision/dataset successful run/job contributions, restricting support to the selected source kind. Filtered effective completion equals GPS completion; manual labels never hide its missing nodes. Existing account-wide readiness and geometry limits remain. See [activity filters](activity-filters.md) for full behavior and compatibility.
