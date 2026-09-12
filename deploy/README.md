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

Never paste values into command arguments, logs, evidence, or Git. First create
a clean detached worktree at the reviewed target SHA. `EURITH_DEPLOY_ASSET_ROOT`
must name that exact worktree; an attached branch, dirty tree, different SHA,
symlinked path, or overlay outside it is rejected. This lets first-use provisioning
consume target Caddy assets and the target API build context while preserving the
production `SOURCE_DIR` through backup, restore, and target rehearsal; the deployer
later checks out the approved rollback candidate for its separate rehearsal:

```bash
sudo CADDY_IMAGE_REF=caddy:2.11.4@sha256:<approved-digest> \
  /opt/eurith/deploy-run/<backend-full-sha>/deploy/provision-release-host.sh \
  --secret-source-dir /etc/eurith/release-secret-source \
  --base-compose /opt/eurith/compose.yaml \
  --release-overlay /opt/eurith/deploy-run/<backend-full-sha>/deploy/compose.release.yml \
  --deploy-asset-root /opt/eurith/deploy-run/<backend-full-sha> \
  --target-sha <backend-full-sha>
```

The same command is the existing-host verification command. It verifies rather
than overwrites `/etc/eurith/api-release.env`,
`/etc/eurith/release-cleanup.env`, and `/etc/eurith/release-deploy.env`.
It resolves the API UID and `eurith-releases` GID, enforces setgid directories,
creates the dedicated final-artifact view, and proves API atomic finalize plus
Caddy read-only and `nosymfollow` behavior. Caddy is exactly `caddy:2.11.4`;
resolve and approve its immutable digest before deployment. The deployer pulls,
validates, and starts `caddy:2.11.4@sha256:<approved-digest>`; the mutable tag is
never used to start the release service.

Credential rotation is one credential at a time: pause publication, withdrawal,
mandatory changes, and cleanup; update the named external consumer and protected
source together; atomically replace the complete API env file as root; run
provisioning verification and negative-auth canaries; then resume. Never silently
regenerate a partial or unexpected protected installation.

Create `/etc/eurith/release-canary-ids.env` as root-owned `root:root` mode `0400`
with exactly these three known records:

```text
missing_release_id=<uuid-known-not-to-exist>
withdrawn_release_id=<existing-withdrawn-direct-uuid>
non_direct_release_id=<existing-eas-or-play-uuid>
```

These rows are operational fixtures retained in the registry; the canary never
creates or changes a release. On a genuinely empty category only,
`withdrawn_release_id` or `non_direct_release_id` may be the literal
`not_applicable`. The canary accepts that sentinel only after its authorized
server-side registry query proves zero rows in the exact category, and emits
`withdrawn_release=not_applicable` or `non_direct_release=not_applicable`.
If a matching row exists, the sentinel is fatal and its retained UUID is required.
The ordinary public probe is fixed to the
allowlisted `/openapi.json` route with expected status `200`; arbitrary paths or
expected statuses are rejected.

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
The database URL file is root-owned mode `0400` and contains exactly
one `DATABASE_URL=postgresql+asyncpg://.../eurith_restore_*` line for a unique
loopback-only database. Raw URL-only files are not accepted for deployment
rehearsal because Compose must inject this isolated URL after the production
environment file. Deployment opens it through trusted, non-symlink ancestor
descriptors exactly once, copies the validated bytes into an invocation-private
root-owned snapshot, and uses only that snapshot for restore and rehearsal.

## Exact-SHA deployment

Export only non-secret runtime paths and the guarded disposable Caddy integration
database URL. Create that unique local `fitpilot_task_caddy_*` database before
running the deployment and drop it afterward. The deploy script refuses a dirty
checkout, non-full SHA, unapproved remote commit, multiple Alembic heads,
unapproved migration identity, missing Caddy digest, failed restore, skipped required
runtime test, or failed canary.

Create a clean detached runner worktree at the target SHA first. This preserves
the exact previous production checkout while the new versioned deployment code
runs; invoking the old checkout's script or pre-switching production source is
rejected.

Review the exact ordered Alembic path and every migration file byte, then create
a root-owned `root:root` mode `0400` approval file outside the checkout:

```text
old_backend_sha=<previous-backend-full-sha>
target_backend_sha=<backend-full-sha>
rollback_backend_sha=<reviewed-rollback-backend-full-sha>
old_alembic_head=<production-head>
target_alembic_head=<target-head>
migration_path_sha256=<sha256-of-ordered-revision-path-and-exact-file-bytes>
rollback_compatible=true
approval_identity=<reviewer-id>
```

The file must contain exactly those eight lines. Its identity hash is calculated
from the ordered migration path and the exact bytes of each path file; any byte,
head, backend SHA, permission, owner, symlink, line, or reviewer-identity mismatch
is fatal. This is an explicit human audit decision, not a semantic source-code
classifier. After the approval is validated, deployment upgrades the isolated
restored database, runs the target health and complete ORM table/column schema
probe, then checks out and builds the separately reviewed rollback backend
through the hardened target release overlay and runs the same versioned probe
against that upgraded isolated database. The rollback SHA may equal the incumbent
SHA, but it is approved independently and must be an ancestor of both the target
and the approved remote ref. Only explicit
`rollback_compatible=true` plus both successful rehearsals enables automatic
application rollback. A failed or missing rehearsal stops before production
migration. A partial production attempt is recorded as `migration_state=unknown`
and requires manual database investigation; it never triggers automatic restore.

```bash
git -C /opt/eurith/backend fetch --prune origin
git -C /opt/eurith/backend worktree add --detach \
  /opt/eurith/deploy-run/<backend-full-sha> <backend-full-sha>
sudo RELEASE_MUTATIONS_PAUSED=1 \
  EURITH_ROLLBACK_SHA=<reviewed-rollback-backend-full-sha> \
  SOURCE_DIR=/opt/eurith/backend \
  EURITH_DEPLOY_ASSET_ROOT=/opt/eurith/deploy-run/<backend-full-sha> \
  EURITH_BASE_COMPOSE=/opt/eurith/compose.yaml \
  EURITH_PUBLIC_API_URL=https://api.eurith.app \
  EURITH_APPROVED_CADDY_DIGEST=sha256:<reviewed-64-hex-digest> \
  EURITH_MIGRATION_APPROVAL_FILE=/etc/eurith/migration-approval-<backend-full-sha>.env \
  EURITH_RESTORE_DB_URL_FILE=/etc/eurith/restore-database-url \
  EURITH_RESTORE_VOLUME_ROOT=/opt/eurith/restore-drill/<unique-empty-generation> \
  TEST_DATABASE_URL=postgresql+asyncpg://localhost/fitpilot_task_caddy_<unique> \
  /opt/eurith/deploy-run/<backend-full-sha>/deploy/deploy.sh \
  <backend-full-sha> <mobile-candidate-full-sha>
```

Before production migration, the target API image and all Caddy assets come from
the validated detached target root. The compatibility image alone uses the exact
rollback checkout as `EURITH_RUNTIME_SOURCE_ROOT`, while still using the target
overlay and target `EURITH_RUNTIME_ASSET_ROOT`; operators do not set those
internal variables.
After both target and rollback-candidate rehearsals pass, the deployer checks out the exact
target in `SOURCE_DIR`, proves the runtime overlay/Caddy bytes and aggregate hash
match the detached root, then points final Compose binds at `SOURCE_DIR`.

The sequence is: provenance and pause gates; paired backup; isolated restore;
Compose and Caddy fmt/adapt/validate; required real loopback Caddy integration;
exact migration-path identity/approval; exact API build; isolated target-upgrade
and rollback-candidate compatibility rehearsal; prior Caddy identity capture;
one production migration; coordinated
API+Caddy switch with image pulls disabled; immediate zero-restart identity
capture; bounded readiness retries; public canaries; Compose-native bounded log
review; final identity/restart proof; mobile gate. Application rollback checks
out the approved SHA and builds it through the hardened target overlay, while
Caddy rollback injects and verifies the exact immutable image ID used by the
prior Caddy container. If Caddy was not
part of the prior running topology, rollback removes the newly introduced Caddy
container and restores the approved rollback API through the target overlay.

## Boot-view inspection, recovery, and reboot gate

The deployment records `boot_mount_assets=verified` only after the installed
helper, unit, and Docker drop-in are regular non-symlink root-owned files with
exact modes `0755`, `0644`, and `0644` and SHA-256 values matching the detached
target assets. It records `boot_mount_unit=enabled` only after `systemd` reports
the unit as persistently `enabled` (not `enabled-runtime`) and active/successful.
It records `boot_mount_runtime=verified` only after the installed helper reports
`boot_mounts=verified`. Inspect the live unit, dependency, and both mounts with
these exact commands:

```bash
systemctl is-enabled eurith-release-views.service
systemctl is-active eurith-release-views.service
systemctl is-enabled docker.service
systemctl status --no-pager eurith-release-views.service
systemctl status --no-pager docker.service
systemctl cat eurith-release-views.service
systemctl cat docker.service
findmnt -n -o SOURCE,TARGET,VFS-OPTIONS --mountpoint /opt/eurith/release-caddy-view/android/sha256
findmnt -n -o SOURCE,TARGET,VFS-OPTIONS --mountpoint /opt/eurith/release-caddy-view/.probe
```

The final view must report source `/opt/eurith/releases/android/sha256`, its
exact target, and `ro,nosymfollow`; the probe must report source
`/opt/eurith/release-caddy-probe-source`, its exact target, and the same options.
If an unexpected host mount entry or option is present, recover in this order:

1. Repair the unexpected host entry without changing the approved target assets.
2. Restart `eurith-release-views.service`.
3. Run both exact `findmnt` commands above and verify both mounts.
4. Only then start or restart Docker and run the bounded API health, Caddy health,
   and public canary checks.

The first production rollout has a separate controlled reboot gate. After the
deployment gate passes, reboot the production host in a controlled window and
prove the release-view unit completed before Docker, both exact mounts remain
`ro,nosymfollow`, API and Caddy container identities have zero restart-count
increase, and all public canaries pass. Until that evidence is reviewed, mobile
`main` and APK construction remain blocked. Production distributes APK only;
never build an AAB or publish through Play Store for this release path.

Run canaries independently with the same protected inputs:

```bash
sudo EURITH_PUBLIC_API_URL=https://api.eurith.app \
  EURITH_CANARY_IDS_FILE=/etc/eurith/release-canary-ids.env \
  ./backend/deploy/canary-release-delivery.sh
```

The canary checks health/database, `Cache-Control: no-store`, absent and wrong
publisher/operator credentials, bad webhook signature, missing/withdrawn/non-direct
downloads, internal-path denial and header leakage, unrelated API behavior,
container identity and restart-count delta, free space, and—when one already exists—full and one-byte range
delivery of a published direct APK. With no published direct APK it records
`existing_direct_apk=not_applicable` and inserts nothing.

Keep `EURITH_EVIDENCE_ROOT` outside both the checkout and release storage, owned
by `root:root` mode `0700`. `MOBILE_GATE_FILE` must be an absolute, nonexistent
file directly below that directory. The deployer fsyncs the completed evidence,
records named stage failures, rollback outcome, UTC completion timestamp, and
evidence SHA-256, then publishes the root-only gate exactly once with an atomic
exclusive link. A pre-existing gate or failed durability step blocks mobile.

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
