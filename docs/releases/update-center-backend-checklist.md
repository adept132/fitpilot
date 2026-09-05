# Backend update-center verification checklist

Verification timestamp (UTC): `2026-09-05T01:21:03Z`

Implementation HEAD under test: `8cd8368cc9562a96e6953e01df8c7fd36fa963a9`

Environment: Windows 11, Python 3.13.7, PostgreSQL client 17.7, local PostgreSQL only.

This checklist contains no token, password, database URL, Firebase credential, or
artifact from a real release. Every database used by the checks had a unique
`fitpilot_task8_*` name and was dropped after the check. The local source
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
| Focused config/auth/storage/registry/cleanup/public API | `FIREBASE_CREDENTIALS=<external-test-fixture-json> python -m pytest tests/test_required_config.py tests/test_release_auth.py tests/test_release_migration.py tests/test_release_storage.py tests/test_release_registry.py tests/test_release_cleanup.py tests/test_main_release_routes.py -q` with synthetic required settings | PASS — 101 passed, 8 skipped, 15 warnings |
| Test database guards | `FIREBASE_CREDENTIALS=<external-test-fixture-json> python -m pytest tests/test_release_cleanup_guard.py tests/test_integration_database_guard.py -q` with synthetic required settings | PASS — 14 passed |
| Full non-integration suite | `FIREBASE_CREDENTIALS=<external-test-fixture-json> python -m pytest tests --ignore=tests/integration -q` with synthetic required settings | PASS — 1,943 passed, 8 skipped, 15 warnings |
| Current-like schema upgrade | `pg_dump --schema-only --no-owner --no-privileges <source-db> -f <temp-schema>; psql <task8-db> -f <temp-schema>; DATABASE_URL=<task8-url> alembic stamp 20260822_02; alembic upgrade head; alembic current` | PASS — restored schema upgraded through `20260830_01`, `20260830_02`, `20260902_01`; current is `20260902_01 (head)` |
| Release PostgreSQL integration | `FIREBASE_CREDENTIALS=<external-test-fixture-json> TEST_DATABASE_URL=<task8-url> python -m pytest tests/integration/test_app_releases_api.py tests/integration/test_release_cleanup_integration.py -vv` with synthetic required settings | PASS WITH PLATFORM SKIPS — 9 passed, 4 skipped |
| Release migration boundary | `DATABASE_URL=<task8-url> alembic downgrade 20260830_02; alembic upgrade head; alembic current` | PASS — returned to `20260902_01 (head)` |
| Protected publish/public API smoke | `FIREBASE_CREDENTIALS=<external-test-fixture-json> TEST_DATABASE_URL=<task8-url> python -m pytest tests/integration/test_app_releases_api.py::test_publish_latest_download_withdraw_cycle -q` with synthetic required settings | PASS — 1 passed; checks webhook target and CI binding, synthetic APK publication, latest, `X-Accel-Redirect`, local stored bytes and SHA-256, withdrawal, download `410`, and no update after withdrawal |
| Explicit-Russian legacy regressions | `FIREBASE_CREDENTIALS=<external-test-fixture-json> TEST_DATABASE_URL=<task8-url> python -m pytest <two-goal-primary-cases> <two-microcycle-conflict-cases> -q` with synthetic required settings after adding `Accept-Language: ru` | PASS — 4 passed |
| Full PostgreSQL integration suite | `FIREBASE_CREDENTIALS=<external-test-fixture-json> TEST_DATABASE_URL=<task8-url> python -m pytest tests/integration -q --maxfail=1` with synthetic required settings after current-like upgrade | INCOMPLETE — reached 29% with no failure after the four localization fixes, then made no progress for more than 90 seconds at the transition to `test_offline_idempotency.py::test_custom_exercise_idempotent`; manually interrupted |
| Compilation | `python -m compileall -q api app scripts` | PASS |
| Alembic graph | `DATABASE_URL=<synthetic-url> python -m alembic heads` | PASS — one head, `20260902_01` |
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
- **Native nginx validation: NOT RUN.** `nginx` is not installed on this workstation. Static inspection confirms `internal`, the `/srv/eurith/releases/` alias, `client_max_body_size 256m`, and `proxy_request_buffering off`; run `nginx -t` on the target Linux host before reload.
- No real GitHub webhook, CI run, EAS build, production token, release volume, or release publication was used.

## Static security, storage, and operations review

- Internal publisher, operator, and webhook boundaries have focused tests;
  non-ASCII and wrong-token cases are included.
- APK staging/finalization, path containment, digest layout, size/integrity,
  idempotency, withdrawal and unavailable-artifact paths are covered by the
  focused suite.
- Nginx exposes release files only via an internal alias; the upload limit leaves
  multipart overhead above the API's 250 MiB payload limit.
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
| Release volume mounts and permissions | NOT RUN | Verify actual image UID, shared host GID, API `rw` mount, nginx `ro` mount, setgid directories, staging `0600`, final APK `0640`, and service-user read access. |
| Nginx configuration | NOT RUN | Install/include the reviewed snippet, run `nginx -t`, then controlled reload and external access-denial/download checks. |
| Disk threshold/alert | NOT RUN | Configure and trigger-test the alert below 20% free space on the release volume. |
| Backup/restore drill | NOT RUN | Take a consistent DB + artifact backup, restore to an isolated target, and verify every restored APK SHA against the registry. |
| Production migration compatibility | NOT RUN | Back up production, verify its current Alembic revision/schema, rehearse upgrade and code rollback against a production snapshot, and retain the exact recovery procedure. |
| Linux cleanup and publication overlap | NOT RUN | Run the four cleanup PostgreSQL integration tests on Linux with a unique disposable DB and temporary volume. |
| Full PostgreSQL integration gate | BLOCKED | Diagnose the reproducible stall at the first offline-idempotency case, then rerun all 486 integration cases to completion. The four language-dependent legacy cases now explicitly choose `ru` and pass. |
| Fresh-database bootstrap | BLOCKED | Approve and implement a separate historical baseline/bootstrap design; this is not required for additive upgrade of the current production-like schema but is required for empty-DB provisioning via Alembic alone. |

Release publication and production deployment remain prohibited until every
applicable gate above has explicit evidence and operator approval.
