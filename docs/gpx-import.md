# GPX uploads

`POST /api/uploads` accepts one authenticated multipart file in the `file` field. It returns `202` with a string upload ID, string job ID, current state, and a `duplicate` flag. Re-uploading identical bytes returns the existing source for that account. `GET /api/uploads/{id}` is account-scoped and returns the state, activity ID after processing, and a sanitized error code on failure.

The development worker processes `process_upload` jobs. It accepts GPX tracks and preserves segment boundaries and point-aligned timestamps. Missing, date-only, malformed, or timezone-free timestamps are stored as null. Coordinates are checked for finite longitude/latitude ranges. Planned routes and waypoints are not imported as recorded activity samples. Missing names, dates, and types are stored as `Unknown activity` or `unknown`.

Uploads are limited to 10 MiB and 100,000 track points. The body limit covers chunked multipart requests before the parser consumes the complete request. XML DTDs and entities are rejected. Local originals live under `backend/.uploads` by default (`UPLOAD_STORAGE_DIR` overrides it); the directory is mode 0700 and files are mode 0600. Database metadata and the `process_upload` job are committed together. If that transaction fails, the newly written unreferenced file is removed. A process crash between durable file write and database commit can leave an unreferenced file; orphan cleanup is deferred.

On success the worker creates one normal activity, links it to the source revision, and completes the job in one lease-checked transaction. The activity remains `processed=false` until later matching work runs. Raw files and failure details are not returned by the API or written to logs.
