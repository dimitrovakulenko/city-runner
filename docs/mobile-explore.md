# Mobile Explore and GPX import

The Explore screen reads lifetime progress plus the current map viewport from the authenticated backend. It displays stored activity tracks, eligible streets, and missing OSM nodes at zoom 16 and above. Normal progress means 90% of eligible nodes, or every node on streets with fewer than ten eligible nodes; strict mode requires every node. Null counts remain pending rather than appearing as zero.

The development map defaults to OpenFreeMap Liberty, matching the existing PoC, and keeps MapLibre attribution visible. Set `EXPO_PUBLIC_MAP_STYLE_URL` to override the development style. The production basemap vendor, service expectations, and terms remain an owner decision. Fixture mode uses local activity samples and a demo map style; it does not provide exploration coverage or upload behavior.

Set `EXPO_PUBLIC_API_BASE_URL` to the backend address reachable from the app:

| Client | Development API URL |
| --- | --- |
| Android emulator | `http://10.0.2.2:8001` |
| iOS simulator | `http://localhost:8001` |
| Physical phone | `https://<development-host>:8001` with a certificate trusted by the phone |

Keep provider credentials and bearer tokens out of Expo public variables. Rebuild the native development app after adding Expo native modules. If the map provider cannot load, the UI displays a retry action; API progress and upload status remain separate.

Choose a `.gpx` file with the system picker. The app rejects files larger than 10 MiB before upload; the backend also enforces its request and track-point limits. Duplicate uploads are labeled. Queued and processing source status is checked for at most 30 foreground polls; use Refresh to resume checks after polling pauses or after returning later. A succeeded upload means ingestion completed. Coverage matching is separate and may remain pending; the app refreshes map and progress during bounded foreground checks and leaves pending counts blank until the backend reports them.

`EXPO_PUBLIC_FIXTURE_MODE=true` is opt-in sample-activity mode. It does not create an authenticated session or fabricate coverage results.
