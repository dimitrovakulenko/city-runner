# Regional OSM import

`backend.app.geography` imports a local OSM XML extract into a versioned shared geography dataset. It stores public OSM IDs, tags, boundaries, ways, and original nodes; it does not import private activity coordinates or perform activity matching.

## Input and eligibility

Use a regional/city `.osm` XML extract containing each selected administrative relation, every boundary way/node referenced by its outer and inner members, and named highway ways with their referenced nodes. Select city relation IDs explicitly. Overpass may emit a way as both a full object and a `skel`; compatible copies with the same ordered node references are merged, while conflicting references/tags and conflicting duplicate node coordinates are rejected. The stored checksum is for the exact imported file. PBF is not supported.

The importer requires a valid administrative boundary multipolygon; it preserves holes and disconnected outer components and rejects incomplete or invalid rings. It accepts only named `highway` ways, excluding `access=private|no`, `foot=no`, and `motorway`, `motorway_link`, `trunk`, `trunk_link`, `construction`, `proposed`, and `raceway`. A way must intersect the city and have at least one original OSM node covered by its boundary. Boundary nodes count as covered; holes do not. No node is interpolated at a clipped intersection. Ways are grouped per city by their exact OSM `name` value; differently cased or spaced names remain distinct.

## Import and activation

Run from the repository root:

```sh
python -m backend.app.geography --file region.osm --region gent --city-relation 897671 \
  --coverage-mode sampled
```

Record the source snapshot timestamp from the extract metadata or pass `--source-timestamp ISO-8601`. Sampled is the default and never activates a dataset. To activate a first validated version, the operator must explicitly choose complete mode and provide source/query evidence:

```sh
python -m backend.app.geography --file region.osm --region gent --city-relation 897671 \
  --coverage-mode complete --coverage-evidence 'Source, query/scope, and snapshot establishing the selected extract coverage'
```

“Complete” records the operator’s claim; code verifies the boundary and that each selected city has eligible streets, but cannot prove an external extract’s coverage. A later version remains staged as `importing`; this slice does not switch active datasets. Identity includes region, file SHA-256, eligibility-rule version, coverage mode, and sorted selected relation IDs. Repeating the same identity is idempotent and does not reactivate a retired/staged version. Import runs transactionally; failures leave no partial dataset.

`find_cities` and `find_nearby_nodes` provide validated, capped WGS84 lookups using local GiST indexes. They are not suitable for unbounded export or private activity matching.

## Public validation snapshot

On 2026-10-05, this public Overpass query retrieved Gent relation 897671, its relation-member ways, named highway ways in the generated city area, then their referenced nodes:

```text
[out:xml][timeout:120];rel(897671)->.city;rel(897671);map_to_area->.area;(.city;way(r.city);way(area.area)["highway"]["name"];);out body;>;out skel qt;
```

The response OSM snapshot was `2026-10-05T20:39:51Z` (area snapshot `2026-10-04T19:59:06Z`). It contained 45 repeated way representations with identical node references and compatible tags; the importer merged those directly from the raw response. Raw response SHA-256: `747e0b7675ee5e81b082c126ee3f8654f78e11f066840da1fac92d406085b951`. The disposable PostGIS import validated 1 city, 9,748 eligible ways, 3,108 exact-name street groups, 56,678 street-node memberships, and 51,134 distinct original nodes. This tests one-city extract handling only; it is not regional or Belgian coverage, and mapped completeness still depends on OSM. The extract is not checked into the repository.

Attribute map/data views with “© OpenStreetMap contributors” and link the [OpenStreetMap copyright and licence page](https://www.openstreetmap.org/copyright), which identifies the ODbL. Preserve the source timestamp/checksum and query scope with the imported dataset.
