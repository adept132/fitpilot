# Eurith production deployment

Production is deployed only from two reviewed, full 40-character commit SHAs:
the backend commit being installed and the frozen mobile candidate it unlocks.
A successful push is not deployment. A successful backend deployment is not APK publication.
Mobile remains blocked until `backend_gate=passed` is written after
all runtime canaries and the bounded log review pass.

## Protected inputs and first use

Install four independent root-owned `0400` source files under
`/etc/eurith/release-secret-source`: `publisher-token`, `operator-token`,
`webhook-secret`, and `cleanup-database-url`. The three tokens are independently
generated 64-character lowercase hexadecimal values. They are consumed as follows:

- publisher: server API and protected GitHub production environment only;
- operator: server API and the operator vault only;
- webhook: server API and the GitHub webhook Secret field only;
- cleanup database URL: host cleanup/backup tooling only.

Never paste values into command arguments, logs, evidence, or Git. Run first-use
provisioning from the reviewed checkout:

```bash
sudo ./backend/deploy/provision-release-host.sh \
  --secret-source-dir /etc/eurith/release-secret-source \
  --base-compose /opt/eurith/docker-compose.yml \
  --release-overlay /opt/eurith/backend/deploy/compose.release.yml
```

The same command is the existing-host verification command. It verifies rather
than overwrites `/etc/eurith/api-release.env`,
`/etc/eurith/release-cleanup.env`, and `/etc/eurith/release-deploy.env`.
It resolves the API UID and `eurith-releases` GID, enforces setgid directories,
creates the dedicated final-artifact view, and proves API atomic finalize plus
Caddy read-only and `nosymfollow` behavior. Caddy is exactly `caddy:2.11.4`;
record its immutable digest at deployment.

Credential rotation is one credential at a time: pause publication, withdrawal,
mandatory changes, and cleanup; update the named external consumer and protected
source together; atomically replace the complete API env file as root; run
provisioning verification and negative-auth canaries; then resume. Never silently
regenerate a partial or unexpected protected installation.

Create `/etc/eurith/release-canary-ids.env` as root-owned `0400` (or `0640`) with
known existing records and one unrelated safe public route:

```text
missing_release_id=<uuid-known-not-to-exist>
withdrawn_release_id=<existing-withdrawn-direct-uuid>
non_direct_release_id=<existing-eas-or-play-uuid>
unrelated_public_path=/openapi.json
```

These rows are operational fixtures retained in the registry; the canary never
creates or changes a release.

## Backup and isolated restore

Keep publisher/withdrawal operations paused and stop the cleanup timer. Back up
the database and complete release volume as one generation, then restore that
same generation into an empty isolated volume and a new local
`eurith_restore_*` database:

```bash
sudo RELEASE_MUTATIONS_PAUSED=1 ./backend/deploy/backup-release-state.sh \
  <previous-backend-full-sha> /opt/eurith/backups/releases
sudo ./backend/deploy/verify-release-restore.sh \
  /opt/eurith/backups/releases/<generation> \
  /etc/eurith/restore-database-url \
  /opt/eurith/restore-drill/<generation>
```

Both commands must report matching manifest SHA-256 and zero missing or mismatched
artifacts. A database dump and release archive are never restored separately.

## Exact-SHA deployment

Export only non-secret runtime paths and the guarded disposable Caddy integration
database URL. Create that unique local `fitpilot_task_caddy_*` database before
running the deployment and drop it afterward. The deploy script refuses a dirty
checkout, non-full SHA, unapproved remote commit, multiple Alembic heads,
destructive migration, missing Caddy digest, failed restore, skipped required
runtime test, or failed canary.

Create a clean detached runner worktree at the target SHA first. This preserves
the exact previous production checkout while the new versioned deployment code
runs; invoking the old checkout's script or pre-switching production source is
rejected.

```bash
git -C /opt/eurith/backend fetch --prune origin
git -C /opt/eurith/backend worktree add --detach \
  /opt/eurith/deploy-run/<backend-full-sha> <backend-full-sha>
sudo RELEASE_MUTATIONS_PAUSED=1 \
  SOURCE_DIR=/opt/eurith/backend \
  EURITH_BASE_COMPOSE=/opt/eurith/docker-compose.yml \
  EURITH_PUBLIC_API_URL=https://api.eurith.app \
  EURITH_APPROVED_CADDY_DIGEST=sha256:<reviewed-64-hex-digest> \
  EURITH_RESTORE_DB_URL_FILE=/etc/eurith/restore-database-url \
  EURITH_RESTORE_VOLUME_ROOT=/opt/eurith/restore-drill/<unique-empty-generation> \
  TEST_DATABASE_URL=postgresql+asyncpg://localhost/fitpilot_task_caddy_<unique> \
  /opt/eurith/deploy-run/<backend-full-sha>/deploy/deploy.sh \
  <backend-full-sha> <mobile-candidate-full-sha>
```

The sequence is: provenance and pause gates; paired backup; isolated restore;
Compose and Caddy fmt/adapt/validate; required real loopback Caddy integration;
one-head and migration-path gate; exact API build; one migration; coordinated
API+Caddy switch; public canaries; bounded log review; mobile gate.

Run canaries independently with the same protected inputs:

```bash
sudo EURITH_PUBLIC_API_URL=https://api.eurith.app \
  EURITH_CANARY_IDS_FILE=/etc/eurith/release-canary-ids.env \
  ./backend/deploy/canary-release-delivery.sh
```

The canary checks health/database, `Cache-Control: no-store`, absent and wrong
publisher/operator credentials, bad webhook signature, missing/withdrawn/non-direct
downloads, internal-path denial and header leakage, unrelated API behavior,
container restarts, free space, and—when one already exists—full and one-byte range
delivery of a published direct APK. With no published direct APK it records
`existing_direct_apk=not_applicable` and inserts nothing.

## Monitoring, rollback, and withdrawal

Alert on framing (`LocalProtocolError` or declared-length mismatch), handoff `502`,
artifact `503`, sustained download `5xx`, permission denial, unexpected `404` or
`413`, internal-header leakage, container restarts, failed range responses, and
release-volume free space below 20%.

On a failed post-switch check, deployment automatically restores the exact old
source/container revision only when the additive migration path is affirmed.
It retains the paired backup and never performs automatic database restore or
Alembic downgrade. A destructive change requires a separate expand/contract plan.

Before disabling Caddy delivery or storage for an affected APK, withdraw the
release through the protected endpoint so discovery stops advertising it and
downloads return `410`. Retain its row, artifact, and version code. Publish a fix
only with a larger version code. Resume publication and cleanup only after the
backend gate and the first real release closure (hash, size, ETag, Digest, signing
certificate, package, install, and smoke checks) all pass.
