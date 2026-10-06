# Mobile GPX/FIT batch imports

Explore accepts batches of up to 20 GPX or FIT files. The client creates the authenticated server manifest first, then submits each selected file in order as its own bounded multipart request. The list shows server counts and each file's queued, processing, imported, failed or deleted state; exact duplicates remain visible as accepted duplicate items.

The manifest is the durable recovery record. On sign-in and app restart, the client loads the newest 20 manifests and offers bounded pagination for older ones. Local file URIs stay in memory only. An item still awaiting upload asks the user to select that exact name and format again; each row invokes the picker separately, so identical basenames cannot attach bytes to the wrong manifest item. Already accepted items do not need another upload. Failed ingestions with a source offer the server retry operation. Deleted items remain terminal and cannot be selected, retried or reimported through that manifest.

**Stop uploading** pauses files the server has not accepted. The current request is allowed to settle, and any accepted source/job continues on the server. Resume sends only remaining awaiting items whose local selections are still available. If the app restarted, the user must select each unaccepted file again. Status refresh is foreground-only, bounded to 30 checks per manual refresh, serialized, and rotates across at most 20 active manifests per pass.

Fixture mode disables manifest and upload requests. Account changes abort in-flight requests and clear local URI references; request generations prevent delayed responses from the previous account from updating the new view. Newly imported activities refresh Explore coverage and activity history.

Software checks: `npm test`, `npm run typecheck`, and `npm run api:types:check`. iOS/Android JavaScript exports do not establish native picker, network or UI acceptance. Native testing remains blocked by the missing tooling recorded in [native testing](native-testing.md).
