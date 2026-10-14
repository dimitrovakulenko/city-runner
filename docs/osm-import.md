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

“Complete” records the operator’s claim; code verifies the boundary and that each selected city has eligible streets, but cannot prove an external extract’s coverage. A later version remains staged as `importing`; this slice does not switch active datasets. Identity includes region, file SHA-256, eligibility-rule version, coverage mode, and sorted selected relation IDs. Repeating the same identity is idempotent and does not reactivate a retired/staged version. Import runs transactionally; failures leave no partial dataset. First activation also initializes durable matching of existing successful sources; staged/sampled versions never replace the live region.

`find_cities` and `find_nearby_nodes` provide validated, capped WGS84 lookups using local GiST indexes. They are not suitable for unbounded export or private activity matching.

## Durable additional regions

Use the [region import contract](region-imports.md) for additional public cities. Apply migration `0016_region_imports`, configure the same private `MAP_IMPORT_STORAGE_ROOT` for operator and worker, then submit an immutable bounded XML snapshot:

```sh
rtk .venv/bin/python scripts/dev/import-osm-region.py submit --file region.osm --region be/city --city-relation RELATION_ID --coverage-mode complete --coverage-evidence 'Public source, selected boundary and full named-highway query scope'
rtk .venv/bin/python scripts/dev/import-osm-region.py status --task-id TASK_ID
rtk .venv/bin/python scripts/dev/import-osm-region.py retry --task-id TASK_ID
```

The ordinary worker handles queued geography when account jobs are idle, then checkpoints existing-source matching in pages of at most 20. Retry resumes its durable phase/cursor; interrupted geography rolls back and restarts from stored original bytes. Task success means matching jobs were queued; each account's progress stays pending until its jobs complete. The browser's region catalog provides search/paging and city/region map focus. Existing direct imports also initialize automatic backfill. No external URL, automatic download, browser geography mutation or replacement activation is added.

## Public validation snapshot

On 2026-10-05, this public Overpass query retrieved Gent relation 897671, its relation-member ways, named highway ways in the generated city area, then their referenced nodes:

```text
[out:xml][timeout:120];rel(897671)->.city;rel(897671);map_to_area->.area;(.city;way(r.city);way(area.area)["highway"]["name"];);out body;>;out skel qt;
```

The response OSM snapshot was `2026-10-05T20:39:51Z` (area snapshot `2026-10-04T19:59:06Z`). It contained 45 repeated way representations with identical node references and compatible tags; the importer merged those directly from the raw response. Raw response SHA-256: `747e0b7675ee5e81b082c126ee3f8654f78e11f066840da1fac92d406085b951`. The disposable PostGIS import validated 1 city, 9,748 eligible ways, 3,108 exact-name street groups, 56,678 street-node memberships, and 51,134 distinct original nodes. This tests one-city extract handling only; it is not regional or Belgian coverage, and mapped completeness still depends on OSM. The extract is not checked into the repository.

Attribute map/data views with “© OpenStreetMap contributors” and link the [OpenStreetMap copyright and licence page](https://www.openstreetmap.org/copyright), which identifies the ODbL. Preserve the source timestamp/checksum and query scope with the imported dataset.

## Brugge validation snapshot

On 13 October 2026, the operator workflow loaded public Brugge municipality [relation 562654](https://www.openstreetmap.org/relation/562654) alongside the existing Gent preview. The [FOSSGIS Overpass endpoint](https://overpass-api.de/api/interpreter) returned this bounded query:

```text
[out:xml][timeout:120];rel(562654)->.city;rel(562654);map_to_area->.area;(.city;way(r.city);way(area.area)["highway"]["name"];);out body;>;out skel qt;
```

OSM snapshot: `2026-10-13T08:39:39Z`; area snapshot: `2026-10-12T15:16:01Z`. Exact response: 5,563,688 bytes, SHA-256 `c98bf6311a10d64a777b238b91478ef6b9e53589932323065dc23841a67b5082`. Validated eligible output: one city, 1,592 street groups, 6,181 ways and 30,823 distinct original nodes. Completeness evidence records the selected city/full named-highway query; this is two supported municipalities, not country/world coverage. The extract stays outside Git.

Actual HTTP imported a synthetic public-node activity before Brugge existed. A queued immutable region task survived API/worker/PostgreSQL restart, activated Brugge and automatically matched that retained recording without another upload. A second complete restart preserved dataset identity, public snapshot bytes, original GPX bytes/samples/timestamps, coverage and session. Gent retained its checksum, 3,109 streets and 51,154 nodes. The synthetic account/originals were removed through the account API; the default preview account remains empty.

## Antwerp automatic discovery snapshot

On 13 October 2026, a synthetic Antwerp GPX upload automatically selected Belgium from the cached public extraction catalog, discovered municipality [relation 59518](https://www.openstreetmap.org/relation/59518), fetched its complete boundary/named highways and activated the city. No operator city ID, private samples or derived activity bbox were sent to the provider. The city query proved the generated area existed before accepting the snapshot.

OSM snapshot: `2026-10-13T16:44:35Z`; immutable response SHA-256 `0f741fc6844656597a474d9426c71cc041d45e32689c1c50350a0d915b60432f`; eligibility version `named-highway-city-name-v1`. Validated output: one city, 3,513 street groups and 50,320 distinct original nodes. The snapshot stays outside Git.

Actual HTTP verified original GPX bytes/GPS/timestamps, automatic preparation/matching, absence of unrelated Gent/Brugge jobs, and unchanged existing datasets. Another synthetic activity reused the city without a provider request and matched an original public node. Full API/worker/PostgreSQL restart preserved the dataset, counts, private original and session. Chrome verified catalog search, street browsing, map focus and a 390 px layout. API account cleanup removed the synthetic private data and left shared Antwerp available with an empty default preview. This validates on-demand indexing for Antwerp; it does not establish all-country/large-city coverage or hosted execution.

## Portugal policy-expansion validation snapshot

On 13 October 2026, an isolated disposable PostGIS/HTTP setup retained a synthetic Portuguese GPX recording with the terminal unsupported checkpoint produced by the former six-country policy. The expanded worker automatically reconsidered its stored country candidates, requested Portugal's public level-7 municipal catalog and selected [Óbidos relation 3991130](https://www.openstreetmap.org/relation/3991130) locally. The activity supplied no city ID. The unchanged full-city query proved the generated area and imported 735 street groups and 17,397 distinct original nodes.

OSM snapshot: `2026-10-13T19:04:06Z`. Exact response SHA-256: `d621fc9443dffac696b2ff17e1d84b5ed008ab2c75b083930a71bb5e420a5a37`; eligibility `named-highway-city-name-v1`. Original GPX bytes, coordinates and timestamps were preserved. A second synthetic account reused the municipality without another provider request, matched an original public node and could not read the first account's activity. Actual API/worker restart preserved sessions, originals and readiness. Chrome verified the public `Óbidos, PT` label, city search/selection, street browsing and 390 px layout. API account deletion removed both synthetic owners' private originals, then the temporary services/database were removed. The normal Gent/Brugge/Antwerp preview was untouched. This validates one additional country and municipal level; the 32 shipped policies do not establish complete worldwide or large-city acceptance. Deployment remains deferred by the owner.

## Qualified UK and Canadian validation snapshots

On 14 October 2026, bounded public catalog checks validated the UK authority-code profile (341 candidates), the province-qualified Canadian profile (618 candidates) and the two narrow US exceptions: New York relation 175905 and District of Columbia relation 162069. Catalog admission is not full-city or nationwide acceptance. Canadian boundaries without positive municipal proof remain unsupported. The actual Canadian response exposed an Overpass derived-element format mismatch; the final query emits province relation tags and generated-area proof before each candidate block, terminated by an untagged scope marker. Parser fixtures now use that real response structure.

Separate disposable PostGIS/HTTP setups uploaded synthetic recordings without city IDs and recovered their retained unsupported checkpoints through the implemented shared worker:

| Municipality | Public relation | OSM snapshot | Streets | Original nodes | Exact XML SHA-256 |
| --- | --- | --- | --- | --- | --- |
| City of London, GB | [51800](https://www.openstreetmap.org/relation/51800) | `2026-10-14T06:45:06Z` | 663 | 10,503 | `a378e10eb420812eecd6620b627b86a03aa375ac617021913ec9d131fe6735db` |
| New Westminster, CA | [1377803](https://www.openstreetmap.org/relation/1377803) | `2026-10-14T08:55:21Z` | 371 | 8,321 | `0cb7aab8c7d46b68839ccf0c058dbc52870bd0be82ae7b6a63afc771bef1268f` |

Both use `named-highway-city-name-v1`. Actual HTTP verified original GPX bytes/coordinates/timestamps, final qualified relation validation, account isolation, second-account reuse without another provider request and API/worker restart. Chrome verified municipal labels, search/selection, street lists and 390 px layout. API account deletion removed private originals; temporary services/databases were removed. The normal Gent/Brugge/Antwerp preview was preserved. NYC/DC full-extract/large-city acceptance, complete Canadian/worldwide coverage, native execution and deployment are not established by these checks.
