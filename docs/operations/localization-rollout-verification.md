# Backend localization rollout verification

Date: 2026-09-04
Worktree: `codex/ru-en-localization-backend`
Verification scope: `cfc1474^..1a8fa9c`
Current base at verification: `8e795c6c286cac601e94fb579f333b34680049e3`

## Scope integrity

The verified contiguous localization-only range contains these 16 commits:

```text
cfc1474 feat(i18n): persist and resolve profile language
f449eb9 fix(i18n): honor Accept-Language quality values
e300b19 feat(i18n): localize API errors and generated copy
a3d56d1 fix(i18n): preserve authenticated response locale
d1107f4 fix(i18n): localize report and structure responses
1c2d217 fix(i18n): localize phase labels and plan CTAs
3804d83 feat(i18n): render durable notifications by profile language
b041b7d fix(i18n): harden notification push rendering
79316da feat(i18n): add bilingual system exercise catalog
833559b fix(i18n): harden exercise backfill and ordering
baa67e3 fix(i18n): validate catalog and rank full search set
f9fad6e fix(i18n): localize plan comparisons and report history
b5d708e fix(i18n): canonicalize plan commands and legacy reports
f7691d3 fix(i18n): complete legacy report localization maps
434260d test(i18n): close server localization gaps
1a8fa9c fix(i18n): resolve pre-profile error language
```

`git diff --check cfc1474^..1a8fa9c` exited `0`.

## Focused tests

Command:

```powershell
$env:DATABASE_URL = '<synthetic local test URL; credentials omitted>'
& 'C:\Users\Admin\PycharmProjects\FitPilotBot\.venv\Scripts\python.exe' -m pytest tests/test_i18n.py tests/test_exercise_i18n.py tests/test_notification_i18n.py -q -p no:cacheprovider
```

Result: exit `0`; **89 passed**, 7 existing Pydantic deprecation warnings in 2.72 s.

The synthetic URL fingerprint was `postgresql+asyncpg://127.0.0.1:54329/fitpilot_test`. It was used only to satisfy import-time configuration; these focused unit tests made no database connection.

## Integration test status

Not run. `tests/integration/test_exercise_i18n.py` requires the `client`, `auth_headers`, and `db` fixtures and writes temporary exercises, so it must run against an explicitly designated disposable test database.

Safe preflight found `DATABASE_URL` absent and no `.env`, `.env.test`, `.env.example`, Docker Compose, or test-compose configuration in this worktree. No existing database configuration was read or used, and no production database was contacted.

**Blocker:** provide an explicit disposable/restored PostgreSQL test database URL before running the three integration cases. Their expected assertions cover both localized system response maps/English search and byte-for-byte preservation of custom exercise text.

## Migration graph

Commands, using the same synthetic local URL only for configuration loading:

```powershell
python -m alembic heads
python -m alembic history
```

Results: both exited `0`. There is exactly one head: `20260830_02`. It descends directly from `20260830_01` (`20260830_01 -> 20260830_02`), which adds nullable English exercise catalog fields.

## Reviewed catalog evidence

The static check of `api/data/exercise_localizations_en.json` exited `0`:

| Check | Result |
| --- | --- |
| Entries | 98 |
| Exact numeric ID set `76..173` | yes |
| Missing IDs | 0 |
| Extra IDs | 0 |
| Empty/non-string English names or descriptions | 0 |

## Constraints reviewed

- The supported UI/API locales are limited to `ru` and `en`.
- System translations come from the reviewed static JSON catalog; no runtime translation service is used.
- The integration tests explicitly expect custom exercise names and descriptions to remain original for English requests.
- Legacy `name` and `description` fields are asserted alongside additive localized response maps.

## Conclusion

The static checks, focused units, migration graph, and catalog validation pass. The rollout is **not yet integration-verified** until a safe disposable PostgreSQL test database is supplied; this is a release gate, not a passing result.

---

## Task 5: disposable-copy migration and backfill verification

Date: 2026-09-04
Worktree: `codex/localization-rollout`
Task base: `cfef388c24ca74fed9108c43e57f7ca9c4b2fea1`

### Safety boundary and artifacts

- The local source database was contacted only for a read-only reachability check and `pg_dump`. Its sanitized endpoint was `localhost:5432/fitpilot`; no migration, test, backfill, DDL, or data write was run on it.
- Private untracked artifact directory: `C:\\Users\\Admin\\AppData\\Local\\Temp\\fitpilot-localization-rollout-20260904-da7e013b`.
- Source custom-format dump: `source-fitpilot.dump`; size `279467` bytes; SHA-256 `373cc0d9cae4072b1dbe7376d7ff85a1b453b2c860b637bfeb001e0ba52a4541`.
- Both generated target names were confirmed absent (`0`) before creation, restored successfully, and are intentionally left in place for diagnosis: `fitpilot_localization_primary_20260904_066c4a9a` and `fitpilot_localization_cycle_20260904_42b6236b`.

### Primary-copy evidence

The restored primary started at Alembic revision `20260822_02`, with no English exercise columns. Its custom-content checksum before the migration was `255b5b27aa62d50d43db31eef6a049c5`.

With `DATABASE_URL` and `TEST_DATABASE_URL` explicitly set only in-process to the primary disposable copy:

| Operation | Exit | Sanitized result |
| --- | ---: | --- |
| `alembic upgrade head` | 0 | Reached `20260830_02`; English columns added. |
| Backfill `--check` before apply | 1 | Fail-closed catalog mismatch. |
| Backfill `--apply` | 1 | No update applied; fail-closed catalog mismatch. |
| Backfill `--check` after apply attempt | 1 | Same fail-closed catalog mismatch. |
| Post-operation invariant query | 0 | See counts below. |

Post-operation values (no user text was queried or recorded):

| Invariant | Result |
| --- | ---: |
| System exercises | 194 |
| Catalog-range system IDs `76..173` | 98 |
| System exercises outside reviewed catalog range | 96 |
| System rows missing an English name or description | 194 |
| Custom-content checksum after the failed apply | `255b5b27aa62d50d43db31eef6a049c5` |

### Blocking outcome

Task 5 is **blocked, not passed**. The reviewed JSON catalog contains exactly 98 IDs (`76..173`), while the restored source data has an additional 96 default/system exercises (`174..269`) without reviewed English catalog entries. The backfill correctly refused to write any row: `updates=0` and exit `1` on both check and apply.

Under the rollout constraint that every system exercise needs a reviewed English entry, no integration test or downgrade/upgrade cycle was run: proceeding would not make the copy valid and could be mistaken for a completed migration. The untouched second copy and dump are retained to repeat this task after the catalog is extended (or the data set is otherwise reconciled through a separately reviewed change). Production rollout remains blocked.

### Review remediation round 1 — completed catalog verification

The failing catalog gate above was investigated only on the retained primary disposable copy. A query restricted to `source = 'default'` established that the full local system set is the contiguous range `76..269` (194 rows); no custom/user row was extracted for catalog authoring. The reviewed static catalog was extended with English names and descriptions for `174..269` in commit `56db721c2b39d36a96619eaad4e8b0eef847f2c8`.

TDD evidence: changing the catalog test to expect exact IDs `76..269` and `194` entries first produced one assertion failure (`37 passed, 1 failed`); after adding the static entries, `tests/test_exercise_i18n.py` passed (`38 passed`, exit `0`). The relevant localization unit suite (`test_i18n.py`, `test_exercise_i18n.py`, `test_notification_i18n.py`, `test_workout_router_i18n.py`) passed: **95 passed**, exit `0`. `git diff --check` passed before the implementation commit.

The initial primary check then exited `1` as expected because all existing English fields were empty. Applying the extended catalog succeeded, followed by a clean check:

| Primary-copy operation | Exit | Sanitized result |
| --- | ---: | --- |
| Backfill `--check` before apply | 1 | `catalog=194 system=194 updates=0 custom_skipped=18`; expected unapplied drift. |
| Backfill `--apply` | 0 | `catalog=194 system=194 updates=194 custom_skipped=18`. |
| Backfill `--check` after apply | 0 | `catalog=194 system=194 updates=0 custom_skipped=18`. |
| `tests/integration/test_exercise_i18n.py` | 0 | **3 passed** against the explicit primary disposable database. |

The integration assertion was corrected in `f18d13fb59ae53d44e9dd890deb4b4792cee93eb`: full catalog search may legitimately return additional English matches, while the targeted exercise must be present. This is stronger than treating a valid complete catalog as a test failure.

Final primary-copy invariants:

| Invariant | Result |
| --- | ---: |
| Alembic revision | `20260830_02` |
| System exercises | 194 |
| System rows in exact range `76..269` | 194 |
| System rows outside that range | 0 |
| System rows missing English name or description | 0 |
| Custom-content checksum | `255b5b27aa62d50d43db31eef6a049c5` (unchanged before/after migration, backfill, and integration test) |

On `fitpilot_localization_cycle_20260904_42b6236b`, all required operations exited `0`: initial `upgrade head`, `downgrade 20260830_01`, second `upgrade head`, backfill `--apply` (`catalog=194 system=194 updates=194 custom_skipped=18`), and final `--check` (`updates=0`). The retained dump and both disposable databases remain available for Task 6; the source local database remains unchanged. This resolves the Task 5 catalog-coverage gate; no production mutation was performed.

### Review remediation round 2 — technique-copy correction

Quality review identified six static English descriptions whose movement sequence or grip/position cue needed greater fidelity to the reviewed Russian system content. Commit `82b5862ac86940fba695eb9d95205f04e32b2409` updates only IDs `205`, `206`, `229`, `242`, `255`, and `256`: Cuban press equipment and scarecrow start, alternating-curl supination, incline-cable-pullover phase order, scaption thumbs-up grip, squat clean and split-jerk reception, and squat clean/front-rack/press sequencing.

All checks used the same explicit disposable primary database where applicable and exited `0`:

| Check | Result |
| --- | --- |
| Catalog JSON parse | valid JSON |
| `tests/test_exercise_i18n.py` | **38 passed** |
| Relevant localization unit suite | **95 passed** |
| Backfill check before apply | exit `1`, expected drift limited to IDs `205, 206, 229, 242, 255, 256` |
| Backfill apply | exit `0`; `catalog=194 system=194 updates=6 custom_skipped=18` |
| Backfill final check | exit `0`; `updates=0` |
| `tests/integration/test_exercise_i18n.py` | **3 passed** |
| `git diff --check` | exit `0` before commit |

This review correction changed no Russian legacy field and no custom/user content. It was applied only to the primary disposable copy; the source database remains unchanged.

---

## Task 4: localization-only rollout branch verification

Date: 2026-09-04
Dedicated worktree: `C:\\Users\\Admin\\fitpilot-mobile\\.worktrees\\localization-rollout-backend`
Branch: `codex/localization-rollout`
Branch base: `1a8fa9c3fe07f4afcb85957ca98e9e2650e884ce`
Documentation cherry-pick: `2124fefac7e208ff6b4bc53e188f762e6a778326`

### Branch integrity

- The target worktree directory was absent and branch `codex/localization-rollout` did not exist before creation.
- `c79dd42` (the snapshot of the six pre-localization user changes) is an ancestor of `HEAD` (exit `0`).
- `1a8fa9c` (the end of the 16-commit localization range beginning at `cfc1474`) is an ancestor of `HEAD` (exit `0`).
- Forbidden update-center/security commits are not ancestors of `HEAD`: `dacaf7d` exit `1`, `0855711` exit `1`, `c938d9a` exit `1`. Exit `1` is expected for each negative ancestry check.
- `git diff --check 06e31f1...HEAD` exited `0`.

### Safe automated verification

The reusable backend virtual environment was `C:\\Users\\Admin\\PycharmProjects\\FitPilotBot\\.venv\\Scripts\\python.exe`. Unit tests used the synthetic, non-routable local database configuration `postgresql+asyncpg://127.0.0.1:54329/fitpilot_test`; no database connection was made by the passing unit tests. Firebase was initialized in-process with the SDK default credential only to avoid reading a missing local service-account file; no Firebase request was made.

| Command scope | Result |
| --- | --- |
| `tests/test_i18n.py`, `tests/test_exercise_i18n.py`, `tests/test_notification_i18n.py`, `tests/test_workout_router_i18n.py` | exit `0`; **95 passed**; 7 pre-existing Pydantic deprecation warnings |
| `python -m alembic heads` | exit `0`; exactly one head: `20260830_02` |
| `python -m alembic history` | exit `0`; `20260830_02` descends from `20260830_01` |
| `git diff --check 06e31f1...HEAD` | exit `0` |

The exercise tests cover static English catalog completeness and the preservation of legacy `name`/`description` alongside additive localization maps. The covered behavior keeps custom exercise text unchanged; no runtime translation service is present.

### Full-suite / integration gate

The full backend suite was attempted with only `DATABASE_URL` and `TEST_DATABASE_URL` set to the same synthetic URL above and `--maxfail=1`. It exited `1` at `tests/integration/test_account_lifecycle.py::test_purge_removes_every_trace`: the test fixture called `init_db()` and received `ConnectionRefusedError` for `127.0.0.1:54329`.

No production or unknown database was read or contacted. Therefore integration coverage (including `tests/integration/test_exercise_i18n.py`) remains **blocked** until an explicit disposable or restored PostgreSQL test database is provided. This branch is ready for review but is not approved for production rollout on the strength of unit tests alone.

---

## Task 6: local bilingual end-to-end smoke (fallback evidence)

Date: 2026-09-04
Backend SHA: `c76a16cd4910754e431bbad18d0ed4db6582ef12`
Mobile SHA: `70c448cedbfe14ed6024a6cf9e02ee5a5f9494c1`
Device: Android 13 / API 33, emulator `emulator-5554`

### Safety and service boundary

- The only database used by the local API and integration test was the retained disposable copy `fitpilot_localization_primary_20260904_066c4a9a`; the source and production databases were not contacted by migration, backfill, test, or API writes.
- A separate Uvicorn process served this copy on `0.0.0.0:8002`. `GET /health` returned `200` with `status=ok` and `database=connected`; `/openapi.json` exposed `/auth/me`, `/profile/settings`, and exercise routes.
- Metro was started from `C:\\Users\\Admin\\fitpilot-localization-worktrees\\mobile` on port `8082` with `EXPO_PUBLIC_API_URL=http://10.0.2.2:8002`; its status endpoint returned `packager-status:running`.
- The emulator had both `adb reverse tcp:8082 tcp:8082` and the pre-existing 8081 reverse. The fresh dev-client deep link targeted `http://10.0.2.2:8082`; Android log evidence shows the client attempting `127.0.0.1:8082`, so the 8082 bundle connection path was exercised.
- The temporary Firebase account was identified only by the sanitized marker `a24d940339cf`. Its generated password and token were never written to logs or this record. The account was deleted after the smoke.

### Direct local API results

| Check | Result |
| --- | --- |
| Profile language persistence `en` | PASS |
| Profile language persistence `ru` | PASS |
| System detail localized maps for IDs `76`, `77` (non-empty RU/EN name and description) | PASS |
| English search finds tested system ID `76` | PASS |
| Cyrillic custom exercise is byte-identical through English and Russian profile requests | PASS |
| Backend `tests/integration/test_exercise_i18n.py -q` on the disposable copy | PASS: **3 passed** |

### Mobile fallback results

| Check | Result |
| --- | --- |
| `localizedExercise`, API client localization, and API-origin tests | PASS: **28 passed** |
| Cached localized-exercise, exercise-cache, offline flow, and profile-language-sync tests | PASS: **41 passed** |
| Device online RU/EN catalog/search/detail/picker smoke | BLOCKED |
| Device offline cache smoke across app relaunch | BLOCKED |

The device checks could not be observed because the installed development build does not contain the native `ExpoLocalization` module. Once the fresh 8082 bundle was loaded, Android emitted repeated `Error: Cannot find native module 'ExpoLocalization'`; Expo Router then marked affected routes as lacking a default export. The same native-binary mismatch is corroborated by the test environment warning that MMKV/NitroModules are unavailable, which means cache persistence across a real relaunch cannot be represented by that old binary. This is a reproducible development-build defect, not a localization-code change made in this task.

Sanitized device captures (no credentials or user content) are retained outside the repository for diagnosis: `C:\\Users\\Admin\\AppData\\Local\\Temp\\fitpilot-task6-screen-local-8002.png` and `C:\\Users\\Admin\\AppData\\Local\\Temp\\fitpilot-task6-expo-localization-missing.png`.

### Final local state

The emulator network was restored and verified enabled after the attempted offline path (`airplane_mode_on=0`, Wi-Fi enabled). Existing processes on ports 8000 and 8081 were left untouched. The isolated 8002 API and 8082 Metro sessions are to be stopped after this record is committed. A new development build containing `expo-localization` and the current native MMKV dependencies is required before device UI/offline checks can move from BLOCKED to PASS.
