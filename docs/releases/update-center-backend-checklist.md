# Backend update-center verification checklist

Verification updated (UTC): `2026-09-06`

Baseline before the CI retry-binding delta: `000c974`. The delta described
below was verified from that baseline; its exact commit is recorded in Git
history rather than embedded in the commit itself.

Environment: Windows 11, Python 3.13.7, PostgreSQL client 17.7, local PostgreSQL only.

This checklist contains no token, password, database URL, Firebase credential, or
artifact from a real release. Every database used by the checks had a unique
`fitpilot_task8_*` or `fitpilot_task9_*` name and was dropped after its checks. The local source
`fitpilot` database was only read by `pg_dump --schema-only`; no source rows or
schema objects were changed.

### Test-only Firebase import prerequisite

Tests that import `api.main` require Firebase Admin initialization even though
they replace authentication and never call Firebase. Before each such command,
set `FIREBASE_CREDENTIALS=<external-test-fixture-json>`, where the value is a
synthetic service-account-shaped JSON object with a freshly generated temporary
RSA private key and an unmistakably test-only project/email. Generate it in
memory in the invoking shell (the verification used Windows/.NET RSA), pass it
only through the child process environment, and remove the environment variable
and key object in `finally`. Do not copy a real service account, write the
fixture into the checkout, commit it, print its JSON/key, or reuse it outside
this test process. An equivalent import-only fixture outside the repository is
acceptable if it has the same lifecycle and never weakens application auth.

Every sanitized pytest command below that says `with synthetic required
settings` therefore includes
`FIREBASE_CREDENTIALS=<external-test-fixture-json>` plus distinct synthetic
publisher/operator/webhook values. PostgreSQL integration commands also include
this Firebase fixture even when the row abbreviates the environment to
`TEST_DATABASE_URL=<task8-url>`.

## Automated evidence

| Area | Sanitized command | Result |
| --- | --- | --- |
| Focused config/auth/storage/registry/cleanup/public API | `FIREBASE_CREDENTIALS=<external-test-fixture-json> python -m pytest tests/test_required_config.py tests/test_release_auth.py tests/test_release_migration.py tests/test_release_storage.py tests/test_release_registry.py tests/test_release_cleanup.py tests/test_main_release_routes.py -q` with synthetic required settings | PASS — 130 passed, 8 skipped, 15 warnings; includes retry binding takeover/revocation, exact idempotency preflight auth/validation/no-leak contracts, nonblank exact EAS update-ID validation/storage/uniqueness, `min_supported_version_code` below/equal/above/null, explicit mandatory, withdrawn escalation, and public direct/EAS response contracts |
| Test database guards | `FIREBASE_CREDENTIALS=<external-test-fixture-json> python -m pytest tests/test_release_cleanup_guard.py tests/test_integration_database_guard.py -q` with synthetic required settings | PASS — 14 passed |
| Full non-integration suite | `FIREBASE_CREDENTIALS=<external-test-fixture-json> python -m pytest tests --ignore=tests/integration -q` with synthetic required settings | PASS — 1,972 passed, 8 skipped, 15 warnings |
| Current-like schema upgrade | `pg_dump --schema-only --no-owner --no-privileges <source-db> -f <temp-schema>; psql <task8-db> -f <temp-schema>; DATABASE_URL=<task8-url> alembic stamp 20260822_02; alembic upgrade head; alembic current` | PASS — restored schema upgraded through `20260830_01`, `20260830_02`, `20260902_01`, `20260906_01`; current is `20260906_01 (head)` |
| Release PostgreSQL integration | `FIREBASE_CREDENTIALS=<external-test-fixture-json> TEST_DATABASE_URL=<task-url> python -m pytest tests/integration/test_app_releases_api.py tests/integration/test_release_cleanup_integration.py -q` with synthetic required settings | PASS WITH PLATFORM SKIPS — 12 passed, 4 skipped; includes exact case-sensitive idempotency lookup of withdrawn/non-latest/cross-lane rows, both deterministic commit orders for a real PostgreSQL advisory-lock race between retry binding and old-run publication, same-SHA takeover/revocation, direct publication thresholds, and EAS mandatory behavior |
| Release migration boundary | `DATABASE_URL=<task8-url> alembic downgrade 20260902_01; alembic upgrade head; alembic current` | PASS — exact EAS update-ID expand/contract returned to `20260906_01 (head)` |
| Protected publish/public API smoke | `FIREBASE_CREDENTIALS=<external-test-fixture-json> TEST_DATABASE_URL=<task8-url> python -m pytest tests/integration/test_app_releases_api.py::test_publish_latest_download_withdraw_cycle -q` with synthetic required settings | PASS — 1 passed; checks webhook target and CI binding, synthetic APK publication, latest, `X-Accel-Redirect`, local stored bytes and SHA-256, withdrawal, download `410`, and no update after withdrawal |
| Explicit-Russian legacy regressions | `FIREBASE_CREDENTIALS=<external-test-fixture-json> TEST_DATABASE_URL=<task8-url> python -m pytest <two-goal-primary-cases> <two-microcycle-conflict-cases> -q` with synthetic required settings after adding `Accept-Language: ru` | PASS — 4 passed |
| Full PostgreSQL integration suite | `FIREBASE_CREDENTIALS=<external-test-fixture-json> TEST_DATABASE_URL=<task8-url> python -m pytest tests/integration -q --maxfail=1` with synthetic required settings after current-like upgrade | INCOMPLETE — reached 29% with no failure after the four localization fixes, then made no progress for more than 90 seconds at the transition to `test_offline_idempotency.py::test_custom_exercise_idempotent`; manually interrupted |
| Compilation | `python -m compileall -q api app scripts` | PASS |
| Alembic graph | `DATABASE_URL=<synthetic-url> python -m alembic heads` | PASS — one head, `20260906_01` |
| Patch whitespace | `git diff --check` | PASS |

The eight focused/non-integration skips are Windows/POSIX permission, symlink,
and directory-descriptor contracts. The four release PostgreSQL skips are the
cleanup mutation and real publication/cleanup overlap cases, because apply mode
fails closed unless Linux provides the required descriptor-relative unlink
primitives. They remain mandatory Linux CI/pre-production checks.

The warning categories are existing Pydantic v2 deprecations, one FastAPI 422
constant deprecation, and a Windows pytest temporary-directory cleanup warning
after successful test completion. No new update-center warning category was
observed.

## Open verification gaps

- **Fresh empty Alembic upgrade: FAIL.** `DATABASE_URL=<fresh-task8-url> python -m alembic upgrade head` reaches `20260822_01` and fails because the intentionally empty production baseline `20260821_01` does not create `day_blueprints`. The new release migration is not the failing revision. Do not use the historical chain to provision an empty database until a separately reviewed baseline/bootstrap design is added.
- **Full PostgreSQL integration: INCOMPLETE.** The four legacy localization
  assertions were corrected at the test boundary by explicitly requesting
  Russian; all four pass. A fresh full rerun then reached 29% with no failure
  and stalled for more than 90 seconds when collection order moved to
  `test_offline_idempotency.py::test_custom_exercise_idempotent`. The run was
  manually interrupted and its exact disposable database was dropped. Diagnose
  this pre-existing integration fixture/locking stall before using the entire
  suite as a production gate.
- **Linux cleanup mutation/concurrency: NOT RUN on this host.** Windows correctly skips the secure descriptor-relative deletion path.
- **Native Caddy validation: NOT RUN on the production host.** Before switching,
  run fmt/adapt/validate and the required loopback integration suite against the
  pinned `caddy:2.11.4` image and record its immutable digest.
- No real GitHub webhook, CI run, EAS build, production token, release volume, or release publication was used.

## Static security, storage, and operations review

- Internal publisher, operator, and webhook boundaries have focused tests;
  non-ASCII and wrong-token cases are included.
- APK staging/finalization, path containment, digest layout, size/integrity,
  idempotency, withdrawal and unavailable-artifact paths are covered by the
  focused suite.
- Caddy rejects direct `/_release_files/*` access, consumes only the exact
  successful internal handoff, and applies `256MiB` only to the direct APK
  publisher route while the API keeps its 250 MiB payload limit.
- `/.gitignore` and `/.dockerignore` use root-anchored `/releases/` patterns.
- The runbook separates API and cleanup environment files, documents distinct
  secrets, read/write versus read-only mounts, resolved UID/shared GID, setgid
  directories, final APK mode `0640`, dry-run-only scheduling, explicit apply,
  disk alerting, backup/restore with SHA verification, withdrawal/EAS rollback,
  and expand/contract requirements for future destructive migrations.

## Production gates — do not mark complete from local evidence

| Gate | Status | Required production evidence |
| --- | --- | --- |
| Publisher/operator/webhook token generation or rotation | NOT RUN | Generate three independent secrets, install protected environment files, configure the matching GitHub boundaries, and record rotation ownership/date without exposing values. |
| Release volume mounts and permissions | NOT RUN | Run `provision-release-host.sh`; verify actual API UID/shared GID, API `rw`, the dedicated Caddy view `ro,nosymfollow`, setgid directories, staging `0600`, final APK `0640`, and all negative mutation/symlink probes. |
| Caddy configuration and delivery | NOT RUN | Record the `caddy:2.11.4` digest; run fmt/adapt/validate, required real loopback tests, direct-prefix/header-denial checks, and public full/range checks when an existing direct APK is available. |
| Disk threshold/alert | NOT RUN | Configure and trigger-test the alert below 20% free space on the release volume. |
| Backup/restore drill | NOT RUN | Take a consistent DB + artifact backup, restore to an isolated target, and verify every restored APK SHA against the registry. |
| Production migration compatibility | NOT RUN | Back up production, verify its current Alembic revision/schema, rehearse upgrade and code rollback against a production snapshot, and retain the exact recovery procedure. |
| Linux cleanup and publication overlap | NOT RUN | Run the four cleanup PostgreSQL integration tests on Linux with a unique disposable DB and temporary volume. |
| Full PostgreSQL integration gate | BLOCKED | Diagnose the reproducible stall at the first offline-idempotency case, then rerun all 486 integration cases to completion. The four language-dependent legacy cases now explicitly choose `ru` and pass. |
| Fresh-database bootstrap | BLOCKED | Approve and implement a separate historical baseline/bootstrap design; this is not required for additive upgrade of the current production-like schema but is required for empty-DB provisioning via Alembic alone. |

Release publication and production deployment remain prohibited until every
applicable gate above has explicit evidence and operator approval.

## Caddy rollout and backend-before-mobile closure

On first use and every existing-host verification, run
`deploy/provision-release-host.sh` from a clean detached exact-target
`EURITH_DEPLOY_ASSET_ROOT`, passing `--deploy-asset-root` and `--target-sha`.
Provisioning and pre-switch validation set `EURITH_RUNTIME_SOURCE_ROOT` and
`EURITH_RUNTIME_ASSET_ROOT` internally. The production checkout stays unchanged
through backup, restore, and target rehearsal, then becomes the exact detached
rollback candidate only for its compatibility build through the target overlay.
After
rehearsal, deployment checks out the target in `SOURCE_DIR`, verifies the final
overlay and Caddy bytes/hash equal the detached root, and only then binds final
runtime assets from `SOURCE_DIR`. While release mutations and cleanup are
paused, create a paired generation with `deploy/backup-release-state.sh` and
prove it through `deploy/verify-release-restore.sh` in an empty volume and local
`eurith_restore_*` database. Run deployment only as
`deploy/deploy.sh <backend-full-sha> <mobile-candidate-full-sha>` and rerun
`deploy/canary-release-delivery.sh` independently before releasing the gate.

The production record must contain exact old/rollback/new backend SHA, frozen mobile SHA,
Compose hash, Caddy tag/digest, single Alembic head/path, backup-manifest hash,
canary and log-review results, and rollback result. It must contain no secret,
database URL, internal handoff path, or artifact filesystem path.

Resolve the approved Caddy image to a digest and run Compose with
`caddy:2.11.4@sha256:<approved-digest>` and pulls disabled during the switch.
Before migration, restore the paired backup in isolation and compare its reported
manifest hash to the backup result. Generate the deterministic ordered migration
path manifest (revision, file name, and SHA-256 of exact file bytes), review those
exact files, and install the exact eight-line root-owned mode `0400`
`EURITH_MIGRATION_APPROVAL_FILE` described in `deploy/README.md`, outside the
checkout. Missing/mismatched identity, SHA, Alembic head, path hash, owner, mode,
symlink status, rollback decision, or reviewer identity is fatal. The deployment
reads the loopback-only restore URL once through trusted ancestor descriptors and
uses only a private root-owned snapshot plus safely serialized Compose overlay,
then upgrades only the isolated `eurith_restore_*` database, runs the target
health/full ORM schema probe, and runs exact `EURITH_ROLLBACK_SHA` against that
upgraded isolated database through the hardened target overlay. That separately
approved SHA must be an ancestor of the target and approved remote ref, must
recognize the target Alembic head, and may equal the incumbent. Only explicit
`rollback_compatible=true` and successful target and rollback-candidate
rehearsals permit production migration and automatic code rollback. Any partial
production migration attempt is `migration_state=unknown`,
requires manual investigation, and forbids automatic database restore/downgrade.

The public URL must be a structurally valid HTTPS origin with no credentials,
query, or fragment. Canary IDs are an exact three-key root-owned mode `0400`
file. `withdrawn_release_id=not_applicable` and
`non_direct_release_id=not_applicable` are allowed only when an authorized
server-side count proves that exact category has zero rows; otherwise an exact
retained UUID is mandatory. The ordinary public probe is the fixed
`/openapi.json` status-`200` route.
After bounded readiness retries, verify exact parsed headers, at most 250 MiB
artifact metadata, container identity/restart delta, and Compose-native bounded
logs. Header ambiguity, redirects leaking an internal header, malformed headers,
restart delta, or timeout is fatal. Capture the new container identities and
zero restart counts immediately after the switch, verify them after readiness
and again after canaries/log review, and retain the exact prior Caddy image ID
so rollback both injects and verifies that immutable identity.
If the prior topology had no Caddy container, rollback must remove the newly
introduced Caddy container and restore only the old base-Compose API; an
overlay file merely present in the old checkout is not evidence that Caddy ran.

Store evidence outside checkout and release storage in a root-owned mode `0700`
directory. The mobile gate must be an absolute nonexistent direct child. Publish
it last, atomically and exclusively, only after fsync of evidence containing UTC
completion time, evidence hash inputs, named stage results, and rollback result.

Monitor framing failures, internal-handoff `502`, unavailable-artifact `503`,
download `5xx`, unexpected `404`/`413`, permission denials, restarts, range
failures, and free space below 20%. A failed check keeps mobile blocked and may
roll back only source/containers after additive schema compatibility is affirmed;
database restore and downgrade are always manual. Withdraw an advertised direct
release before disabling its delivery infrastructure.

Publisher, operator, and webhook rotation remains consumer-specific and one at
a time, with publication/cleanup paused and the full protected API environment
installed atomically. Successful push is not deployment. Successful backend
deployment is not APK publication. Advance mobile only after the evidence says
`backend_gate=passed`; close the first real APK release with byte/hash/size,
ETag/Digest, certificate/package, install, and smoke evidence.

## Release automation idempotency preflight

Before starting EAS or another native build, release automation must call
`GET /internal/app-releases/by-idempotency?key=<deterministic-key>` with the
publisher bearer token. The operator token and public requests are not valid
for this endpoint. Keys are 1–128 ASCII characters and match
`[A-Za-z0-9][A-Za-z0-9._:-]*`; the publication endpoints enforce the same
contract. A `404` means no release owns that exact, case-sensitive key and new
work may proceed using the current CI run ID. Any other non-`200` response is a
hard failure, not a reason to rebuild.

On `200`, automation must compare every immutable returned field other than the
stored original CI run ID with its persisted release manifest: identity, lane
and delivery method; source commit;
idempotency key; fingerprint and runtime; version code and name; exact EAS
build/update/update-group identifiers; publication/mandatory/minimum-version
state; exact bilingual `release_notes`; and direct-APK digest/size where
applicable. The stored original `ci_run_id` must be strictly validated and kept
as audit data, but it is deliberately excluded from equality comparison with
the current retry run. A new CI run may reuse the record only when every other
immutable manifest/idempotency field matches exactly. This reuse must return
the persisted result without calling EAS, issuing a publication `POST`, or
performing any other mutation.
Any compared-field mismatch is a hard conflict requiring operator review.
Withdrawn and older
non-latest releases are deliberately returned so retries can never republish a
key that already belongs to historical state. The response never contains the
artifact storage key, filesystem path, download URL, token, or secret. Release
notes are returned as the strict persisted `{ru, en}` object so a retry can
detect a changed publication tuple before starting a build.

## CI retry-binding delta — 2026-09-06

- Unit policy regression: `tests/test_release_registry.py` verifies that a new
  CI run can take over the unchanged expected SHA, the previous run is rejected
  on publish, and the newest run publishes successfully.
- Disposable PostgreSQL API regression:
  `tests/integration/test_app_releases_api.py::test_retry_binding_revokes_old_run_and_accepts_new_run`
  passed against a unique local `fitpilot_task9_*` database with synthetic
  Firebase and release credentials. The source database was not changed.
- The complete release API integration file passed: **11 passed**, with only
  the pre-existing Pydantic/FastAPI deprecation warnings and Windows pytest
  temporary-directory cleanup warning.
- The PostgreSQL concurrency regression instruments the real lane-lock call and
  uses bounded waits to prove both linearizations: bind-first rejects the
  blocked old publish without creating a row; publish-first commits the old
  release before the blocked retry takes over, leaving the new run bound.
- The endpoint remains protected by `RELEASE_PUBLISHER_TOKEN`; stale SHA binds
  remain `409`. Same-SHA retries are latest-attempt-wins under the same lane
  advisory lock used by publication.
