# Pedestrian route preview and saved routes

`POST /api/routes/preview` uses the FOSSGIS OSRM foot profile only when
`ROUTING_PROVIDER=fossgis` is explicitly configured. Routing is disabled by
default. The adapter uses the fixed HTTPS endpoint from the [FOSSGIS route
frontend](https://raw.githubusercontent.com/fossgis-routing-server/osrm-frontend/master/src/leaflet_options.js),
an identifiable User-Agent, an eight-second timeout, no retries, and a 2 MiB /
10,000-position response bound. The provider's response is checked for a route,
ordered waypoint matches within 100 m, finite WGS84 geometry, and finite
nonnegative distance and duration.

The PostgreSQL quota row permits one FOSSGIS request per second across API
processes. Quota refusals return 429 and `Retry-After`; provider no-route or
unmatched-waypoint results return 422, timeouts return 504, and invalid or
unavailable responses return 503. Preview geometry is stateless. Saved routes
persist only server-calculated geometry. Editing an unchanged ordered waypoint
list reuses its stored geometry and metrics without calling the provider.

Show the returned OpenStreetMap attribution, Fix the map link, and waypoint
privacy notice with route previews. FOSSGIS states that route coordinates are
sent to its server and saved in its logs; its usage policy requires attribution,
a Fix the map link, a valid User-Agent, no more than one request per second, and
no heavy usage. See [the service policy](https://routing.openstreetmap.de/about.html).
Therefore the public service remains a development provider; production stays
disabled until an operator selects a provider after capacity, terms, and privacy
review.

The custom [FOSSGIS foot profile](https://raw.githubusercontent.com/fossgis-routing-server/cbf-routing-profiles/master/foot.lua)
excludes several `access` and `foot` restrictions, but the deployed profile is
not pinned and cannot promise avoidance of every closure or inaccessible path.
Route geometry is a plan, not a recorded activity. GPX export uses `<rte>` and
contains no fabricated timestamps; route requests never create activities,
sources, jobs, visits, or street completion.

Tests inject synthetic OSRM-shaped responses through the test-only
`create_app(routing_provider=...)` hook. The same response validator runs for
injected results. Tests do not call FOSSGIS or use real route coordinates.

## Web route following

Preview a walking route or open a saved route in **Routes**, then choose **Follow
route** on the map. The marker follows fresh browser GPS fixes; the map follows
the marker until you pan. **Recenter route** restores camera following. The map
shows **On route**, **Off route** with distance to the route, or an uncertain
position when the reported accuracy overlaps the 50 m route corridor. Distance
uses the nearest bounded great-circle segment, including endpoints; it does not
infer progress along loops or crossings.

Following requires browser location permission and HTTPS or localhost. Fixes
must be finite, within the map's latitude limits, no more than 15 seconds old,
and accurate to 50 m or better. Invalid, coarse, unavailable or overdue positions
hide the marker and route classification. The existing watch can recover on a
fresh fix; **Retry GPS** replaces it. Permission denial clears the watch and
provides an explicit retry. Out-of-order fixes and callbacks from stopped watches
are ignored. See the [browser geolocation contract](https://www.w3.org/TR/geolocation/).

A hidden page or `pagehide` pauses following and clears its watch and position.
Returning requires **Resume following**. Leaving Routes, changing/editing the
preview, logging out, replacing the account session or reloading stops following.
Logout clears GPS immediately, including while server revocation is pending.
**Stop following** clears the watch and marker. Following uses the already-loaded
route geometry; offline GPS updates can continue while that page stays visible,
although map tiles and coverage refresh still depend on connectivity.

Positions stay in memory, and automatic following camera moves do not replace
remembered viewport preferences. Ordinary map/tile viewport requests continue.
Following creates no activity, recording or GPS coverage and requests no new
route calculation. Native route-following and device acceptance remain D29 work
for the separate device team.
