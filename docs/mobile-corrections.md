# Mobile corrections

Activity deletion requires an explicit confirmation. A successful `204` removes the activity and queues cleanup of its private source file; file cleanup is asynchronous. If the request fails, the confirmation remains available for retry.

Street detail shows GPS status separately from effective status. Marking a street manually complete requires a trimmed reason from 1 to 500 characters. The label and reason are visible in detail, and manual streets use a distinct map color. Manual completion does not change recorded GPS visits or remaining-node coordinates. Undo removes the label; GPS coverage remains unchanged.

After either correction, the app refreshes activity history, viewport/progress, and the selected city, street, missing-node, and contribution views. Account changes abort outstanding correction requests before network dispatch and fence their responses. Both actions require a verified signed-in session and are unavailable in fixture mode.

Run the mobile checks from `apps/mobile`:

```sh
rtk proxy npm run typecheck
rtk proxy npm test
rtk proxy npm run api:types:check
```

Native iOS and Android acceptance is tracked separately; JavaScript tests and typechecking do not verify native UI execution.
