# Production localization rollout

## Entry point

Use this single production deployment entry point with the exact reviewed full
40-character Git commit SHA (not a branch, tag, shortened SHA, or SHA-256
digest):

```bash
./backend/deploy/deploy.sh "$TARGET_SHA"
```

Do not run the migration, catalog backfill, image build, or API restart as
standalone production operations. The script keeps the previous API running
until the target image, migration, and localization catalog gate have all
succeeded.

## Preflight

Before starting, record the reviewed target SHA and confirm all of the
following on the production host:

- The server checkout is clean and the exact full 40-character commit SHA
  resolves to itself after the script's fetch.
- There is enough free disk space for a PostgreSQL backup and the target image.
- The pre-deploy backup created by the script is non-empty, passes
  `pg_restore --list`, and has a recorded SHA-256 checksum and path without
  exposing database credentials.
- Alembic reports exactly one migration head.
- The reviewed English catalog contains every default system exercise ID from
  76 through 269 (194 entries), with no missing IDs.
- Record the checksum of custom exercise content before the deployment and
  compare it after the catalog gate; user-provided content must not change.
- The approved mobile build's device-gate status is known before public smoke:
  production builds use only `https://api.eurith.app`, while HTTP is allowed
  only in development builds.

Never put tokens, secrets, or complete database URLs in the rollout record.

## What the deploy script does

The script performs this ordered sequence: verify a clean worktree and the
full target commit SHA, fetch and resolve that exact SHA, create and validate a
PostgreSQL backup (including SHA-256), check out the target, build its API
image, apply migrations, apply and check the reviewed localization catalog
using that newly built image, restart the API, then poll the public health
endpoint.

The catalog gate is transactional and applies only the reviewed default-system
catalog. It runs its apply pass and then its check pass. A missing catalog ID or
catalog drift makes the gate fail closed before the API restart.

## Failure and rollback rules

If build, migration, or catalog gate fails before the API restart, the script
restores the source checkout to the old commit and leaves the currently running
old API untouched. It prints the backup path and SHA-256. Additive migration
columns and catalog writes can remain in the database; this is expected rollback
scope and does not mean the database was restored.

If API startup or public health fails after the API restart attempt, the script
restores the old source checkout, rebuilds the old image, and restarts the old
API. A rollback build or restart failure is reported explicitly together with
the backup path and SHA-256. It does not perform an automatic schema downgrade
or automatic database restore. Additive migration columns and catalog writes
may remain. Escalate to the backup owner before any manual restore or downgrade
decision.

## Mandatory post-deploy smoke

After successful public health, complete and record both of these checks:

1. Bilingual public smoke: switch a test account between Russian and English;
   verify system exercise list, detail, and search use the reviewed localized
   content, while a custom exercise remains byte-for-byte unchanged.
2. Old-client compatibility smoke: use a client published before localization
   and verify login, exercise list/detail, and one read-only workout screen.
   Legacy `name`, `description`, string `detail`, `title`, and `body` fields
   must continue to work.

Stop and investigate if the custom-content checksum differs, any catalog ID is
missing, either smoke fails, or the health endpoint does not recover. Record
the exact SHA, backup path and checksum, migration head, catalog result, and
smoke outcomes without user content or credentials.
