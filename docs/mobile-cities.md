# Mobile city and street browsing

Use the **Cities** tab after signing in. Select an active dataset from `/api/progress`, then search/paginate cities and streets. Street filters are **All**, **Incomplete**, **Partial**, and **Completed** under the selected normal or strict rule. The server preserves stable ordering and the client keeps dataset, city, street, activity, and node IDs as strings.

Coverage readiness is separate from geography. While matching is pending or failed, the screen labels counts as unavailable and does not treat null values as zero. Remaining OSM nodes appear only when coverage is ready; each row shows the original node ID and coordinates and can open that point in Explore at the same rule. Contribution pages show owner activities and supported-node counts only, then open the selected activity's existing detail screen.

The active dataset list is capped by the progress API. A selected dataset omitted from a truncated list remains selected and is revalidated by its city response before the client displays results. Retired or importing datasets are never presented as active geography.

Fixture mode has no city coverage and uses no authenticated city API. Device API addresses and sign-in setup are in [mobile Explore/import](mobile-explore.md) and [mobile login](mobile-login.md). Install locked dependencies with `rtk npm ci`; run `rtk npm run typecheck` and `rtk npm test` from `apps/mobile`.
