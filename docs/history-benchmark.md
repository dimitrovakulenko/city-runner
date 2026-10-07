# Synthetic history benchmark (D33)

Measured on 7 October 2026 against the production API/worker on local loopback: macOS 26.6.2 arm64, 16 logical CPUs, 64 GiB RAM, Python 3.13.13, PostgreSQL 16.15 and PostGIS 3.4.6. No VM, native device, provider event or public-network acceptance is claimed.

The disposable fixture has two synthetic cities, 200 streets and 2,000 original geography nodes. The heavy account starts with 5,000 activities / 5 million GPS samples. Nineteen other accounts have 100 activities each. Every 1,000-sample recording cycles ten distinct nodes: this stresses repeated track geometry and overlapping history, not millions of distinct contribution rows. Final heavy counts are 5,020 activities / 5.02 million GPS samples / 50,200 source contributions; all accounts have 69,200 contributions.

Fixture loading uses the production GPX parser and private source storage, then seeds revision-qualified source/job/run/contribution records and rebuilds summaries. Its 73.5-second loading time is **not import throughput**. Twenty additional files go through actual sequential HTTP upload, ingestion and matching while twenty authenticated browsing threads run. Each warm case has five bursts of twenty requests (100 samples), first across distinct owners, then all against the heavy account. Browsing during imports rotates cases with a 100 ms think time.

## Measured fix

The [before report](benchmarks/d33-before.json) found every overview request failing: `ST_Intersection` spent the 1.5-second work budget noding repeated GPS segments, and PostGIS raised a GEOS interruption as an internal error. The track display query now clips its rectangle with `ST_ClipByBox2D` before simplification. Original activities, segments, timestamps, matching and cached source geometry remain unchanged. GEOS work-limit interruptions return safe retry guidance. A PostGIS regression checks 51 recordings with two 500-point segments, bounded clipped output and exact unchanged activity detail.

The [after report](benchmarks/d33-after.json) records zero HTTP/server errors and both viewport budgets passing:

| Map case | Distinct users p50 / p95 | Heavy account p50 / p95 | During imports p50 / p95 |
| --- | --- | --- | --- |
| Overview | 70 / 102 ms | 102 / 158 ms | 30 / 84 ms |
| Close viewport | 53 / 65 ms | 60 / 77 ms | 12 / 40 ms |
| Selected activities | 62 / 74 ms | 73 / 97 ms | 15 / 46 ms |

Largest transferred map body: 69,671 bytes, with actual `Content-Encoding: identity`. The report separately records theoretical gzip size; it is not presented as network transfer. Map truncation remains explicit. Progress, activity lists and sorted streets also returned 200 throughout; their worst warm p95 was 126 ms.

Actual imports: upload acceptance p95 22 ms; ingest queue / handler p95 94 / 22 ms; match queue / handler p95 19 / 38 ms; upload-to-ready p95 305 ms, including the 100 ms polling interval. This is one sequential importer with concurrent browsing, not twenty simultaneous history importers.

Final originals occupy 542,306,170 bytes (seed originals: 540,722,700); the database occupies 85,523,479 bytes. Sampled peak RSS: API 101 MiB, worker 79 MiB, database backends 777 MiB. Sampled CPU: API 8.61 s, worker 0.62 s, database backends 2.15 s. Driver CPU including fixture construction: 32.68 s. Database RSS sums shared pages repeatedly; its CPU excludes cluster background workers and short-lived processes between one-second samples. These are process measurements, not a production VM memory budget.

## Reproduce

Use the existing Python/PostGIS dependencies. The command creates only UUID-named disposable local databases, its own source store and deletion ledger, starts its own API/worker, then removes them. It rejects remote administrator URLs and bounds fixture size. It never reads private PoC files.

```sh
rtk proxy env POSTGIS_ADMIN_DATABASE_URL=postgresql+psycopg://USER@127.0.0.1:PORT/postgres .venv/bin/python scripts/dev/history-benchmark.py --output /tmp/history-benchmark.json
```

Use `--activities 50 --samples 100 --users 2 --secondary-activities 10 --import-files 2 --repeats 1` for a smoke run. Full regional geography, dense distinct-node histories, multiple simultaneous backfills and hosted/device/network measurements remain separate release-capacity checks.
