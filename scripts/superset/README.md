# D43 disposable Superset pilot

Synthetic fixtures only. Requires Docker Compose, Python 3.12+ and the web
Playwright dependencies. An internal fixture runs the real app session endpoints;
no provider setup or production database URL is used.

From the repository root:

```sh
rtk docker compose -f scripts/superset/compose.yaml up -d --build --wait --wait-timeout 180
rtk proxy python3 scripts/superset/probe.py --output /tmp/d43-privacy.json
rtk proxy node scripts/superset/browser_probe.mjs --output /tmp/d43-browser.json
rtk proxy python3 scripts/superset/bounds_probe.py --output /tmp/d43-bounds.json
```

Open <http://127.0.0.1:8090/pilot/> and choose synthetic account A or B.
The full Superset UI supports SQL Lab, Explore, virtual datasets and private saved
charts/boards. The ordinary City Runner Insights entry is D46.

The bounds probe intentionally fences A after discarding an oversized response.
Restart the pilot processes before the deliberate cancellation-failure probe:

```sh
rtk docker compose -f scripts/superset/compose.yaml restart superset gateway
rtk docker compose -f scripts/superset/compose.yaml up -d --no-deps --wait --wait-timeout 60 superset gateway
rtk proxy python3 scripts/superset/bounds_probe.py --failure-only --output /tmp/d43-failure.json
rtk proxy python3 scripts/superset/bounds_probe.py --provision-only --output /tmp/d43-provisioning.json
```

The failure probe temporarily denies the cancellation role, verifies B stays
fenced across workers, terminates its synthetic query as operator, and restores
the grant in `finally`. Restart Superset/gateway after this probe. Their restart
retains saved definitions and app sessions, and clears local failure fences. Inspect any remaining analytics connections before recovery.

`rtk proxy env PILOT_STOCK_SECURITY=1 docker compose -f scripts/superset/compose.yaml up -d --no-deps superset`
selects the original unsafe Superset permission/timeout baseline; the privacy probe
must fail on copied tableless query downloads. Restore the normal service without
that variable before further use.

Remove only this disposable project and its synthetic volume:

```sh
rtk docker compose -f scripts/superset/compose.yaml down -v
```

See [the implementation plan](../../docs/superset-analytics.md#d43-local-pilot--14-october-2026)
for limits, findings, metric semantics and outstanding production acceptance.
