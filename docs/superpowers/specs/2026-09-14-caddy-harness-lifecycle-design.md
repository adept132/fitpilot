# Caddy Release Harness Lifecycle Design

## Outcome

The release-host integration test exercises the real public APK download route
through the exact production Caddyfile and pinned Caddy image. It starts and
stops deterministically, including after a failed startup, without crossing
asyncpg connections between event loops or leaving fixture rows behind.

This is a change to the test harness only. It does not change the production
API, the production database, the APK format, or deployment cutover behavior.

## Evidence and current failure

The guarded disposable database and real release router already work: the
latest rehearsal completed 18 non-runtime checks and created zero surviving
fixture rows. Caddy failed before serving a request because the pinned image
has no `Entrypoint` and its default `Cmd` begins with `caddy run`; the harness
overrode it with `run`, which Docker tried to execute directly. Shutdown also
logged an asyncpg connection being closed from a different event loop. The
restored rehearsal database remained at migration `20260908_01`, and the
production API remained on its incumbent revision.

## Approaches

1. **Chosen: one owner loop for the harness API and database.** A worker thread
   owns one asyncio loop from schema creation through API service and fixture
   deletion. The caller owns Docker, mount and HTTP probes. This removes the
   underlying loop-affinity mismatch and gives cleanup a single owner.
2. **Rejected: retain separate loops and dispose the engine at each handoff.**
   Smaller patch, but connection closure can itself run on the wrong or a
   closed loop, as the latest rehearsal showed. More handoffs make later
   failures difficult to audit.
3. **Rejected: mock the download route or use the live production API.** Either
   would stop testing the candidate handler against the isolated database.

## Lifecycle and ownership

`CaddyHarness.start_or_skip()` continues to validate the unique local
`fitpilot_task_caddy_*` URL before importing application or database modules.
The caller prepares temporary fixture files and protected bind mounts, then
starts one worker thread. The worker runs a single asyncio loop that:

1. creates missing tables from current models in the guarded disposable DB;
2. seeds only this harness instance's release fixtures;
3. constructs a minimal FastAPI app with the real public releases router and
   its localized error handler, without importing the full app or Firebase;
4. serves that app over localhost with Uvicorn until stop is requested;
5. in `finally`, deletes only this instance's fixture IDs and disposes its
   engine on the same loop, even if startup or serving failed.

The caller waits for API readiness or a worker failure before starting Caddy.
It then starts the pinned image using the explicit `caddy run --config
/etc/caddy/Caddyfile --adapter caddyfile` command and verifies the same mount,
symlink, permission, range, HEAD and content gates as today. On exit it removes
the test Caddy container, requests worker shutdown, waits for worker cleanup,
unmounts only its own temporary views, and removes its own temp directory.
Shutdown must not silently suppress failed row deletion or leave a worker
running; any such failure makes the release gate fail. The original startup
error and cleanup error must both remain visible, with protected values
redacted as in the existing subprocess diagnostics.

The `CADDY_TEST_IMAGE` digest remains fixed. The test asserts the Docker argv
used by the harness, including the executable `caddy`, and the host runtime
preflight confirms the pinned image is installed. No floating tag or secret
copy is introduced.

## Safety boundaries

- The database guard and production baseline checks are unchanged. No schema
  creation or fixture deletion may target the production or restored DB.
- The harness deletes only IDs it generated, including after a partial failure.
  A failure to prove cleanup is an error, not a skipped or passing test.
- The production Caddyfile and final-view security gates remain unchanged.
- This work does not run the production migration, switch the API container,
  publish an APK, or build an AAB.

## Verification and release gate

Tests first reproduce the wrong Docker executable and the cross-loop or
shutdown failure. Unit tests cover worker startup, normal stop, failure before
seed, failure after seed, failure during Caddy launch, and row cleanup.
Existing public-route and Caddy safety tests remain. The Linux release-host
test must run with `CADDY_INTEGRATION_REQUIRED=1` and the guarded disposable
URL; it must pass real GET, HEAD and range delivery through Caddy. After that,
the existing script may rehearse migration on the restored DB, compare all 41
original user-table fingerprints, prove rollback compatibility, and confirm
the live API/container and production schema did not change. No production
cutover occurs unless all these gates pass and the user separately authorizes
it.
