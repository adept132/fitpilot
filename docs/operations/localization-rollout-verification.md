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

Results: both exited `0`. There is exactly one head: `20260902_01`. Its ancestry includes `20260830_02` (`20260830_01 -> 20260830_02`), which adds nullable English exercise catalog fields.

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
