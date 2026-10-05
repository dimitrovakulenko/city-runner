# Garmin street coverage PoC

Experimental, single-user, local browser application. The first milestone is your Garmin history → discovered cities → visited/missing OSM nodes and completed/partial streets. Production requirements remain in [PROJECT.md](PROJECT.md); progress is tracked in [BACKLOG.md](BACKLOG.md).

## Run

To test the UI without Garmin, seed the isolated synthetic demo and start it:

```sh
.venv/bin/python -m poc.demo
POC_DATA_DIR=.poc-data/demo POC_PASSWORD=demo .venv/bin/python -m uvicorn poc.app:app --host 127.0.0.1 --port 8000
```

Open [localhost:8000](http://127.0.0.1:8000), username `poc`, password `demo`. Select Brussels or Ghent, pan/zoom, and click colored streets. Each city has one completed, one partial, and one unvisited synthetic street; percentages and node hits are calculated by the actual matching engine. These streets and tracks are illustrative fixtures, not real OSM/Garmin data. Garmin sync is disabled in demo mode. Stop this server before starting the real-account PoC below.

Requires Python 3.12 or newer and internet access. Run from this repository:

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements-poc.txt
.venv/bin/python -m poc.garmin_sync login
.venv/bin/python -m uvicorn poc.app:app --host 127.0.0.1 --port 8000
```

The login command prompts for your Garmin email, hidden password, and MFA when requested. Enter them locally. Your password is not persisted; session tokens are saved in the ignored `.poc-data/garmin/` directory. Use a separate data directory for a different account.

Open [localhost:8000](http://127.0.0.1:8000). Browser username: `poc`. Read the generated browser password locally from `.poc-data/app-password`, or set `POC_PASSWORD` before starting the server. This password is separate from Garmin. Keep the server bound to localhost; this PoC is not a hosted multi-user service.

Select **Sync Garmin now**. Saved Garmin sessions also trigger automatic sync at startup and every five minutes while the server runs. Your watch must upload to Garmin Connect first. The browser refreshes status every five seconds.

Initial sync paginates the activity history exposed by Garmin Connect, downloads running/walking/hiking GPX files, then discovers cities and matches streets. Other activities and workouts without suitable GPS are counted separately. Source files, city geography, and progress are cached locally; repeated imports use Garmin activity IDs. Failed downloads are retried, and interrupted matching resumes. Large histories and the first downloads of city geography can take a long time.

The map shows recorded tracks, completed/partial/unvisited streets, city percentages, and visited/missing nodes at zoom 14 or closer. Select a city to zoom to it, or **Show all cities** to fit the discovered cities. Click a street for its visited/total node count. Tracks are simplified only for display.

**Import GPX** loads a local activity export up to 10 MB, zooms to its GPS track, and processes street coverage in the background. The same file content is imported only once, including if renamed. Invalid files and files without GPS track points are rejected. If OSM matching fails, the GPS track remains saved and visible; import the file again to retry matching. Strava GPX exports can be imported this way; this is a manual file import, not Strava account integration. In demo mode the initial geography and tracks remain synthetic, while files you import contain their own GPS coordinates.

For terminal-only sync, stop the web server and run:

```sh
.venv/bin/python -m poc.garmin_sync sync
```

## What this experiment proves

- Connect your personal Garmin account and import the available historical GPS activities.
- Discover cities from the actual GPS coordinates rather than a predefined single-city demo.
- Match actual samples to existing OSM nodes within 25 metres. No interpolation across GPS gaps.
- Group named eligible highway ways by name within each city. A street is completed at 90% of its nodes; streets with fewer than ten nodes require every node.
- Preserve per-activity node contributions and recheck older tracks when another city's nodes are added.
- Import a new Garmin activity automatically and update the same saved map after a restart.

This is approximate node coverage, not precise traversed street length. Public roads/paths must have an OSM name; motorway/trunk, construction/proposed/raceway, private/no-access, and foot-prohibited ways are excluded. Same-name disconnected streets within a city are grouped together for this experiment.

## Architecture and limits

`poc/garmin_sync.py` handles personal Garmin login, history, and polling work. `poc/core.py` handles GPX, SQLite/RTree storage, OSM discovery, and matching. `poc/app.py` serves the password-protected API and `poc/index.html` MapLibre map. One Python process and one local database are enough for this PoC. The production FastAPI/PostGIS/Terraform plan is not required to demonstrate the algorithm.

The personal connector uses [python-garminconnect](https://github.com/cyberjunky/python-garminconnect), an unofficial integration that can break when Garmin changes its service. Production Garmin support still requires investigating the [approved Activity API](https://developer.garmin.com/gc-developer-program/activity-api/). No approved partner access or real-account sync has been verified in this repository.

City discovery uses OSM administrative boundaries, default `admin_level=8`, rather than a worldwide canonical city catalogue. Administrative levels differ between countries. GPS samples without a supported boundary remain visible in the status as unmapped; they do not establish global city coverage. Exact unresolved coordinates are cached. A long route outside supported boundaries can require many queries and be slow.

Configuration:

| Variable | Default | Purpose |
| --- | --- | --- |
| `POC_DATA_DIR` | `.poc-data` in the repository | Private local database, downloaded GPX, Garmin tokens, and browser password |
| `POC_PASSWORD` | Generated local password | Browser authentication |
| `POC_ADMIN_LEVELS` | `8` | Comma-separated boundary levels, in preference order |
| `OVERPASS_URL` | `https://overpass-api.de/api/interpreter` | Public OSM query endpoint |

If you change the boundary levels, use a fresh `POC_DATA_DIR` and reconnect Garmin so existing city assignments do not mix different definitions. To retry previously unmapped coordinates without changing the definitions, stop the server, run `.venv/bin/python -m poc.garmin_sync reprocess`, then restart it.

Public Overpass services can timeout or throttle. A matching failure pauses processing; retry sync later or configure another permitted endpoint from the [OSM instance list](https://wiki.openstreetmap.org/wiki/Overpass_API#Public_Overpass_API_instances). Overpass receives coordinates for city discovery. The map loads MapLibre from unpkg and tiles from OpenFreeMap; these providers receive normal map requests. Internet access is required.

One private exported GPX has been imported and matched against cached geography; samples outside cached cities remain unassigned. The activity file, screenshots and personal location details are excluded from this repository. The local `location_lookup_pending` state prevents further city-coordinate queries in both GPX and Garmin processing. Obtain explicit consent before sending private location data to an external geography service; then clear this state and run `reprocess` before retrying so the provisional unresolved-coordinate cache is cleared.

The PoC does not reconcile Garmin edits/deletions or discover arbitrarily old activities added after the initial backfill once they fall outside the incremental scan. To rescan history, use a fresh data directory. It has no Google login, Stripe/trial, Strava, planner, phone apps, desktop packages, OSM updates, or production scaling guarantees. Those remain later project requirements.

## Acceptance and evidence

The PoC is accepted only when your real account demonstrates:

1. Login/MFA, all available history imported, and failures/missing-GPS/unmapped points accounted for.
2. Several cities from your actual history, with manually checked completed and incomplete streets.
3. Repeat sync and server restart without duplicate activities or lost progress.
4. A new watch activity uploaded to Garmin, automatically imported, and visible on the map. Record actual latency.
5. Initial sync time, activity/GPS/node counts, database/source-file sizes, and map response time recorded in the backlog.

Automated verification uses `.venv/bin/python -m unittest discover -s tests -v`. Twelve fixture-based tests pass: distance/completion rules, GPX gaps, duplicate imports, cross-city names, boundary holes, new-city border nodes, authentication/API validation, failed historical imports, paginated history followed by a new activity updating coverage, GPX uploads updating coverage with file validation/deduplication, OSM geometry/access filtering, and offline matching while consent is pending. The browser map was verified using synthetic data and your exported Strava GPX in Chrome. Real Garmin acceptance remains pending.
