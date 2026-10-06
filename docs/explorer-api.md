# City and street browsing API

All routes require the account bearer token and an explicit `dataset_id`. IDs backed by BIGINT are JSON strings. Queries use one repeatable-read snapshot and a 1.5-second PostgreSQL statement timeout. Dataset state (`active`, `importing`, or `retired`) is always returned; an explicitly requested old/staged version is queried only within that exact dataset. Its coverage is ready only when every current successful source has a matching successful run and job for that version.

Common query parameters are `page` (1-based, default 1, maximum 1,000,000), `page_size` (default 50, maximum 100), and `q` (case-insensitive literal substring, maximum 200 characters). Ordering is stable by city name/ID, street name/ID, or activity date/ID. `%` and `_` in `q` are ordinary characters.

## `GET /api/cities`

Requires `dataset_id`; accepts `rule=normal|strict` (default `normal`), paging, and `q`. Returns dataset state, account coverage readiness and revision, and paged cities. Ready city summaries contain distinct `visited_nodes` and `eligible_nodes`, plus `completed_streets` and `eligible_streets` under the requested rule. All four progress values are null while coverage is pending or failed.

## `GET /api/cities/{city_id}/streets`

Requires `dataset_id`; accepts `rule=normal|strict`, `filter=all|incomplete|partial|completed` (default `all`), paging, and `q`. Zero-eligible streets are excluded. In ready coverage, states are disjoint: `complete` meets the selected threshold, `missing` has zero visited nodes, and `partial` has at least one visited node but misses the threshold. `incomplete` includes `missing` and `partial`; `partial` excludes unvisited streets. Normal completion visits every node when a street has fewer than 10 nodes, otherwise at least 90%; strict completion visits every node.

When coverage is pending or failed, street progress fields are null. `filter_applied` is false for a requested non-`all` filter because its result cannot be determined yet; the page then contains all matching streets by name, and does not imply that any are unvisited.

## `GET /api/streets/{street_id}`

Requires `dataset_id`; accepts `rule` and paging parameters. Returns street progress plus a page of actual remaining original OSM node IDs and coordinates. `remaining_nodes_page.total` counts missing nodes, independent of the completion threshold: a normally complete street may still have missing nodes. The node list and total are null until coverage is ready. Pending coverage never labels nodes as missing.

## `GET /api/streets/{street_id}/contributions`

Requires `dataset_id` and paging parameters. Returns distinct owner activities with current successful source/run/job support for eligible nodes in the street. Overlapping source records for one activity appear once; results contain activity metadata and supported-node counts, never raw GPS. While coverage is pending or failed, `activities_available` is false and `total` is null.
