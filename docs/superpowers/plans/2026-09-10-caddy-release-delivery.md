# Caddy Direct-APK Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve authorized production APK downloads through a pinned Caddy container, with fail-closed routing, asymmetric storage permissions, verified paired backups, and an exact-SHA deployment gate that blocks mobile release until backend canaries pass.

**Architecture:** FastAPI remains the release-state authority and returns an empty, size-validated handoff; Caddy v2.11.4 consumes only a successful internal-path handoff and serves bytes from a read-only final-artifact mount. Versioned Compose, host-provisioning, backup/restore, deployment, and canary scripts make the infrastructure reproducible without committing secrets or host-specific identifiers. The backend is landed and canaried before the existing mobile release plan may advance mobile `main` or build an APK.

**Tech Stack:** Python 3.12, FastAPI 0.135.1, Uvicorn 0.42.0 with h11, SQLAlchemy 2.0/PostgreSQL, pytest 9, Bash, Docker Compose, Caddy v2.11.4, curl, OpenSSL, pg_dump/pg_restore.

**Spec:** `docs/superpowers/specs/2026-09-10-caddy-release-delivery-design.md`

## Global Constraints

- Production Caddy is exactly `caddy:2.11.4`; record and deploy the reviewed immutable image digest in production evidence.
- The public success handoff contains `X-Accel-Redirect`, `Content-Type`, `Content-Disposition`, `ETag`, and `Digest`, but never the artifact `Content-Length`; FastAPI must still compare `path.stat().st_size` to the typed database size.
- Direct access to every `/_release_files/*` variant returns `404`; only upstream status `200` plus the exact `/_release_files/` prefix can reach `file_server`.
- Caddy copies only `Content-Type`, `Content-Disposition`, `ETag`, and `Digest`; it generates length, range, and last-modified metadata from the opened file and never exposes `X-Accel-Redirect`.
- The `256MiB` request ceiling applies only to exact `POST /internal/app-releases/android/direct-apk`; FastAPI retains its 250 MiB artifact limit.
- API mounts `/opt/eurith/releases:/var/lib/eurith/releases:rw`; host provisioning exposes the final subtree through `/opt/eurith/release-caddy-view` remounted `ro,nosymfollow`; Caddy mounts only that view at `/srv/eurith/releases/android/sha256:ro`; `.staging` is never visible to Caddy. Deployment blocks unless host and container probes confirm both mount flags, a regular read, external-symlink denial, and write denial.
- Host directories are setgid mode `2770`, finalized APKs are mode `0640`, and runtime UID/GID values are resolved rather than assumed.
- `/etc/eurith/api-release.env` and `/etc/eurith/release-cleanup.env` are root-owned mode `0640`; publisher, operator, and webhook secrets are independent, never printed, never committed, and exposed only to their named consumers. Non-secret Compose interpolation lives in `/etc/eurith/release-deploy.env`.
- A PostgreSQL custom dump, complete release-volume archive, and checksum manifest must be created and restored together in isolation before the first Caddy-enabled production switch.
- No force-push, schema downgrade, database reset, artifact-first deletion, version-code reuse, AAB build, Play Store action, mobile `main` advancement, release tag, EAS build, or APK publication occurs before the backend production canary gate passes.
- Every implementation task gets a fresh independent reviewer after its tests pass; all Critical, High, and Medium findings are fixed and re-reviewed before the next task.

## File map

- Modify `api/routers/releases.py`: preserve typed artifact-size validation while emitting an empty handoff without artifact length.
- Modify `tests/test_main_release_routes.py`: unit contract for headers, size mismatch, unsafe keys, and CR/LF fallback filename.
- Create `tests/test_release_handoff_uvicorn.py`: real Uvicorn/h11 framing regression over TCP.
- Create `deploy/caddy/Caddyfile`: ordered public denial, exact upload limit, response interception, header allowlist, file serving, and structured redacted access logs.
- Create `deploy/compose.release.yml`: versioned production overlay for API/Caddy environment, private networking, group membership, and asymmetric mounts.
- Create `tests/deploy/test_caddy_contract.py`: fast static contract checks for pinning, mounts, routes, headers, and secret isolation.
- Create `tests/deploy/caddy_harness.py`: disposable PostgreSQL release seeding, real Uvicorn lifecycle, fixture metadata, and controllable malformed upstream responses.
- Create `tests/deploy/test_caddy_integration.py`: Docker-backed full/HEAD/range, bypass, handoff, limit, and read-only permission tests.
- Create `deploy/lib/release_common.sh`: shared strict argument, path, SHA, permission, command, and redacted-status helpers.
- Create `deploy/provision-release-host.sh`: idempotent host group/directory/env installation and runtime identity probes.
- Create `tests/deploy/test_provision_release_host.py`: isolated fake-root and fake-command tests for first-use, existing-install, secret, and permission behavior.
- Create `deploy/backup-release-state.sh`: paused-state PostgreSQL/volume backup plus deterministic checksum manifest.
- Create `deploy/verify-release-restore.sh`: isolated paired restore and release-row/file integrity verification.
- Create `tests/deploy/test_release_backup_restore.py`: fake-tool unit contracts plus opt-in disposable PostgreSQL round trip.
- Modify `deploy/deploy.sh`: exact-SHA, versioned Compose, backup, migration, coordinated API/Caddy switch, rollback, and canary orchestration.
- Create `deploy/canary-release-delivery.sh`: public/auth/header/range checks with no production fixture insertion.
- Modify `deploy/README.md`: Caddy provisioning, secret-consumer coordination, backup/restore, deploy, canary, monitoring, rollback, withdrawal, and first-release procedure.
- Modify `docs/releases/update-center-backend-checklist.md`: replace nginx assumptions with exact Caddy and backend-before-mobile gates.
- Create `tests/deploy/test_release_deploy_contract.py`: shell/runbook ordering, exact-SHA, rollback, and mobile-gate contracts.

---

### Task 1: Correct the empty FastAPI handoff and prove h11 framing

**Files:**
- Modify: `api/routers/releases.py`
- Modify: `tests/test_main_release_routes.py`
- Create: `tests/test_release_handoff_uvicorn.py`

**Interfaces:**
- Consumes: `AppRelease.artifact_size_bytes: int | None`, `_artifact_path(root, storage_key, expected_sha256) -> Path | None`.
- Produces: `_download_headers(release: AppRelease) -> tuple[dict[str, str], int] | None`; `download_release(release_id: UUID, db: AsyncSession = Depends(get_db)) -> StreamingResponse` for both GET and HEAD, with no body and no `Content-Length`.

- [ ] **Step 1: Change the unit test before production code**

In `test_download_returns_accel_headers_for_existing_direct_apk`, replace the length assertion with:

```python
assert response.content == b""
assert "content-length" not in response.headers
assert set(response.headers) >= {
    "x-accel-redirect", "content-type", "content-disposition", "etag", "digest"
}
```

Keep `test_download_rejects_file_with_unexpected_size` unchanged so removal of the response header cannot remove size validation. Add `test_download_headers_return_typed_expected_size` asserting that `_download_headers(_release())` returns approved metadata without `Content-Length` plus integer `12`, and parametrize the success route over GET and HEAD so HEAD also returns no body and the same handoff metadata.

- [ ] **Step 2: Run the focused unit regression and observe RED**

Run: `python -m pytest tests/test_main_release_routes.py -q -k "download"`

Expected: `test_download_returns_accel_headers_for_existing_direct_apk` fails because the current response exposes `content-length: 12`; the truncated-artifact test remains green.

- [ ] **Step 3: Implement the typed handoff contract**

Change `_download_headers` to return `(headers, expected_size)` and omit `Content-Length`:

```python
def _download_headers(release: AppRelease) -> tuple[dict[str, str], int] | None:
    # validate sha256 and size exactly as today
    return ({
        "Content-Type": "application/vnd.android.package-archive",
        "Content-Disposition": f'attachment; filename="{_download_filename(release.version_name)}"',
        "ETag": f'"sha256:{sha256}"',
        "Digest": f"sha-256={base64.b64encode(bytes.fromhex(sha256)).decode('ascii')}",
    }, size)
```

In `download_release`, unpack `headers, expected_size`, compare `path.stat().st_size != expected_size`, then add only `X-Accel-Redirect`. Change the decorator to `@router.api_route("/app-releases/{release_id}/download", name="download_release", methods=["GET", "HEAD"])` and return `StreamingResponse(iter(()), status_code=200, headers=headers)`: a plain empty Starlette `Response` auto-populates `Content-Length: 0`, which does not satisfy the approved no-length contract. Update the module and endpoint docstrings from nginx-specific wording to proxy-neutral internal handoff wording.

- [ ] **Step 4: Add the real Uvicorn/h11 regression**

In `tests/test_release_handoff_uvicorn.py`, start `uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=free_port, http="h11", lifespan="off", log_level="warning"))` in a daemon thread. Override `get_db`, `_release_by_id`, and `release_storage_root` exactly as the unit test does, then request the real TCP URL with `http.client.HTTPConnection` and assert:

```python
assert response.status == 200
assert response.read() == b""
assert response.getheader("Content-Length") is None
assert response.getheader("X-Accel-Redirect") == f"/_release_files/{release.artifact_storage_key}"
assert not server_thread.is_alive() after server.should_exit is set and joined
```

Capture Uvicorn logs and assert they contain neither `LocalProtocolError` nor `Too little data for declared Content-Length`.

- [ ] **Step 5: Run unit and real-server tests and observe GREEN**

Run: `python -m pytest tests/test_main_release_routes.py tests/test_release_handoff_uvicorn.py -q`

Expected: all tests pass; the TCP response completes cleanly with an empty body and no promised artifact length.

- [ ] **Step 6: Commit and request independent review**

```bash
git add api/routers/releases.py tests/test_main_release_routes.py tests/test_release_handoff_uvicorn.py
git commit -m "fix(release): make APK handoff HTTP-safe"
```

Reviewer must independently verify the size comparison remains typed and active, mutate `expected_size` to prove the mismatch test fails, and rerun the Step 5 command.

### Task 2: Add the pinned Caddy routing and Compose contracts

**Files:**
- Create: `deploy/caddy/Caddyfile`
- Create: `deploy/caddy/release-view-gate.sh`
- Create: `deploy/compose.release.yml`
- Create: `tests/deploy/test_caddy_contract.py`
- Delete after Task 3 validation: `deploy/nginx/releases.conf`

**Interfaces:**
- Consumes: FastAPI success header `X-Accel-Redirect: /_release_files/android/sha256/<64-lowercase-hex>.apk`; base production Compose services named `api` and `postgres`.
- Produces: one-shot `release-view-gate` and Caddy HTTPS service `caddy`; Caddy cannot start unless the gate exits successfully after checking the protected view; defaults `EURITH_UPSTREAM=api:8000` and `RELEASE_FILE_ROOT=/srv/eurith/releases`; environment variables `EURITH_SITE_ADDRESS`, `EURITH_UPSTREAM`, `RELEASE_FILE_ROOT`, and `RELEASE_SHARED_GID`, with only the site address and group ID supplied by `/etc/eurith/release-deploy.env` in production.

- [ ] **Step 1: Write static contract tests first**

Create tests that load the files as text/YAML-compatible mappings and assert exact invariants: `caddy:2.11.4`, no published API port, `api` has only the full `:rw` release mount, `caddy` and `release-view-gate` have only the dedicated `/opt/eurith/release-caddy-view` `:ro` mount, every gate/Caddy host bind uses long syntax with `create_host_path: false`, the overlay carries the exact `ro,nosymfollow` host/container blocking-probe contract, all three services use the required `RELEASE_SHARED_GID`, only API reads `/etc/eurith/api-release.env`, Caddy depends on successful gate completion, and Caddy text contains `request_body { max_size 256MiB }`, exact publish method/path matching, exact internal-prefix denial before proxying, conjunctive `status 200` plus header matching, and no `copy_headers Content-Length`.

Include negative assertions for `publisher`, `operator`, `webhook`, `DATABASE_URL`, `.staging`, `nginx`, and broad `/internal/*` upload-limit matchers in the Caddy service/config.

- [ ] **Step 2: Run contract tests and observe RED**

Run: `python -m pytest tests/deploy/test_caddy_contract.py -q`

Expected: collection or file-existence assertions fail because the versioned Caddyfile and Compose overlay do not exist.

- [ ] **Step 3: Implement the ordered Caddyfile**

Use a single site block `{$EURITH_SITE_ADDRESS:https://api.eurith.app}` and explicit ordered `route` blocks. The first rejects the internal prefix. The exact publish route applies `request_body max_size 256MiB` and proxies to `{$EURITH_UPSTREAM:api:8000}`. The ordinary proxy defines a named response matcher whose `status 200` and exact-prefix `header X-Accel-Redirect /_release_files/*` predicates are documented as a security-sensitive conjunction; `handle_response` copies only the four approved metadata headers, rewrites from the handoff header, strips the prefix, roots at `{$RELEASE_FILE_ROOT:/srv/eurith/releases}`, and invokes `file_server`.

Add a separate malformed-handoff matcher that turns a non-qualifying upstream response carrying `X-Accel-Redirect` into a bodyless `502`. Strip `X-Accel-Redirect` on every other upstream response. Configure JSON access logs so Authorization, webhook signature, upstream internal header, release notes, bodies, and filesystem paths are never logged.

- [ ] **Step 4: Implement the versioned Compose overlay**

`deploy/compose.release.yml` must define:

```yaml
services:
  api:
    env_file: [/etc/eurith/api-release.env]
    group_add: ["${RELEASE_SHARED_GID:?set in /etc/eurith/release-deploy.env}"]
    volumes: [/opt/eurith/releases:/var/lib/eurith/releases:rw]
    expose: ["8000"]
  release-view-gate:
    image: caddy:2.11.4
    restart: "no"
    entrypoint: [/bin/sh, /usr/local/bin/release-view-gate.sh]
    read_only: true
    network_mode: none
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    group_add: ["${RELEASE_SHARED_GID:?set in /etc/eurith/release-deploy.env}"]
    volumes:
      - type: bind
        source: ./backend/deploy/caddy/release-view-gate.sh
        target: /usr/local/bin/release-view-gate.sh
        read_only: true
        bind: {create_host_path: false}
      - type: bind
        source: /opt/eurith/release-caddy-view
        target: /srv/eurith/releases/android/sha256
        read_only: true
        bind: {create_host_path: false}
  caddy:
    image: caddy:2.11.4
    restart: unless-stopped
    depends_on:
      api: {condition: service_started}
      release-view-gate: {condition: service_completed_successfully}
    group_add: ["${RELEASE_SHARED_GID:?set in /etc/eurith/release-deploy.env}"]
    volumes:
      - type: bind
        source: ./backend/deploy/caddy/Caddyfile
        target: /etc/caddy/Caddyfile
        read_only: true
        bind: {create_host_path: false}
      - type: bind
        source: /opt/eurith/release-caddy-view
        target: /srv/eurith/releases/android/sha256
        read_only: true
        bind: {create_host_path: false}
      - caddy_data:/data
      - caddy_config:/config
    ports: ["80:80", "443:443", "443:443/udp"]
volumes:
  caddy_data: {}
  caddy_config: {}
x-eurith-release-view-contract:
  source: /opt/eurith/releases/android/sha256
  view: /opt/eurith/release-caddy-view
  required_vfs_options: [ro, nosymfollow]
  blocking_probes:
    - host-regular-file-readable
    - host-external-symlink-denied
    - container-vfs-ro-nosymfollow
    - container-external-symlink-denied
```

`release-view-gate.sh` reads only `/proc/self/mountinfo` and two fixed probe entries installed by Task 4. It requires `ro,nosymfollow`, reads the regular marker, verifies the external marker is a symlink to `/etc/passwd`, and fails if that symlink can be opened. It logs only `release_view_gate=passed|failed`. Do not add API host ports or any secret environment to Caddy or the gate. The production command always supplies the existing base Compose file first and this overlay second.

- [ ] **Step 5: Validate text and pinned Caddy syntax**

Run:

```bash
python -m pytest tests/deploy/test_caddy_contract.py -q
docker run --rm -v "$PWD/deploy/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2.11.4 caddy fmt --diff /etc/caddy/Caddyfile
docker run --rm -v "$PWD/deploy/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2.11.4 caddy adapt --adapter caddyfile --config /etc/caddy/Caddyfile --pretty >/dev/null
docker run --rm -v "$PWD/deploy/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2.11.4 caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile
```

Expected: pytest passes, `fmt --diff` emits no diff, and adapt/validate exit `0`. Record `docker image inspect caddy:2.11.4 --format '{{index .RepoDigests 0}}'` as review evidence, not in source unless it is the production-approved digest.

- [ ] **Step 6: Commit and request independent security review**

```bash
git add deploy/caddy/Caddyfile deploy/caddy/release-view-gate.sh deploy/compose.release.yml tests/deploy/test_caddy_contract.py
git commit -m "feat(release): add pinned Caddy delivery contract"
```

Reviewer must attempt matcher reordering, a broad upload matcher, a writable Caddy mount, a direct mount of `/opt/eurith/releases/android/sha256`, omission of the `nosymfollow` gate contract, removal of `create_host_path: false`, removal/bypass of `service_completed_successfully`, and a copied upstream length; each mutation must make `test_caddy_contract.py` fail.

### Task 3: Prove delivery with real Uvicorn, PostgreSQL, and Caddy

**Files:**
- Create: `tests/deploy/caddy_harness.py`
- Create: `tests/deploy/test_caddy_integration.py`
- Modify: `tests/integration/database_test_guard.py`
- Delete: `deploy/nginx/releases.conf`

**Interfaces:**
- Consumes: explicit local `TEST_DATABASE_URL` naming a unique `fitpilot_task_caddy_*` database; Docker; `deploy/caddy/Caddyfile`; Caddy v2.11.4.
- Produces: pytest marker `caddy_integration`; `CADDY_TEST_IMAGE` defaulting to `caddy:2.11.4`; harness control routes available only in the test process for absent/malformed/non-200 handoffs.

- [ ] **Step 1: Extend the disposable-database guard and write failing harness tests**

Allow only localhost database names matching existing accepted prefixes or `fitpilot_task_caddy_*`; retain rejection of `fitpilot`, `eurith`, remote hosts, missing URL, and ambiguous names. Add a unit test for each accepted/rejected case before changing the guard.

Create integration tests for full GET, HEAD, `Range: bytes=2-5`, direct and percent-encoded internal paths, `../` traversal variants, upstream status/header matrix, missing/wrong-size artifact, header injection, upstream false length, exact-route 256 MiB+1 rejection, unrelated POST passthrough, and Caddy read-without-write/rename/chmod/delete. Before serving, assert `ro,nosymfollow` on the temporary host view and inside the Caddy container; place a symlink in the source final tree targeting a readable file outside it and prove both host-view and Caddy HTTP access fail while an adjacent regular APK succeeds.

- [ ] **Step 2: Run the focused tests and observe RED**

Run: `python -m pytest tests/test_integration_database_guard.py tests/deploy/test_caddy_integration.py -q`

Expected: the new database prefix is rejected and the Caddy harness fixtures are missing; no production or shared database is contacted.

- [ ] **Step 3: Implement the real-process harness**

`caddy_harness.py` must:

1. require the guarded `TEST_DATABASE_URL` before importing `api.main`;
2. apply `alembic upgrade head` to that disposable database;
3. create a real published `AppRelease` row with a unique UUID, `direct_apk`, content-addressed key, exact SHA-256 and size;
4. write byte fixture `b"EURITH-CADDY-RANGE-FIXTURE"` under a temporary `android/sha256` tree and set `RELEASE_STORAGE_ROOT` to its API-visible root;
5. start the real application with `uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=reserved_port, http="h11", lifespan="off", log_level="warning"))`;
6. run the exact production Caddyfile with test-only `EURITH_SITE_ADDRESS`, `EURITH_UPSTREAM`, and `RELEASE_FILE_ROOT` environment values pointing at loopback and the temporary mount; do not duplicate or generate routing directives in the harness;
7. create a temporary bind view of the final subtree, remount it `ro,nosymfollow`, verify both VFS flags and regular-read/external-symlink-denial on the host, launch `caddy:2.11.4` with only that view mounted read-only, verify the flags and both read outcomes again inside the container, then wait for readiness;
8. expose malformed response variants through a test-only ASGI router that never ships in `api/`;
9. on teardown, stop Caddy/Uvicorn, delete only the seeded row and temporary files, and leave database creation/drop to the caller that created the exact guarded database.

All subprocess output is bounded to 64 KiB per stream and sanitized of Authorization, signature, internal path values, environment content, and database credentials before assertion failures.

- [ ] **Step 4: Make every security and HTTP assertion explicit**

Assert full bytes and SHA-256; HEAD has no body; range returns `206`, exact `Content-Range`, four bytes, correct ETag/Digest, and Caddy-generated length. Assert direct internal/traversal access is `404`; malformed/non-200 handoffs are bodyless `502`; normal upstream errors preserve status/body but never the internal header; exact oversize upload is `413` before an upstream request counter increments; unrelated endpoint counters do increment; Caddy permission probes can read but all four mutation attempts fail.

- [ ] **Step 5: Run the integration suite and observe GREEN**

Create a unique local database, export its full asyncpg URL only for the test process, then run:

```bash
python -m pytest tests/test_integration_database_guard.py -q
python -m pytest tests/deploy/test_caddy_integration.py -q -m caddy_integration
```

Expected: all cases pass against real Uvicorn/h11, PostgreSQL, and Caddy v2.11.4; Docker inspection confirms the Caddy bind is read-only and excludes `.staging`. Drop only the exact database created for this run after the suite exits.

- [ ] **Step 6: Remove the obsolete nginx artifact, commit, and review**

```bash
git rm deploy/nginx/releases.conf
git add tests/integration/database_test_guard.py tests/test_integration_database_guard.py tests/deploy/caddy_harness.py tests/deploy/test_caddy_integration.py
git commit -m "test(release): verify Caddy APK delivery end to end"
```

Reviewer must inspect Docker mounts and raw responses, rerun the tests, and confirm no test can connect unless the guarded disposable URL is explicit.

### Task 4: Provision host permissions and protected configuration idempotently

**Files:**
- Create: `deploy/lib/release_common.sh`
- Create: `deploy/provision-release-host.sh`
- Create: `tests/deploy/test_provision_release_host.py`

**Interfaces:**
- Consumes: `--root PATH` (default `/`, test-only alternate root), `--secret-source-dir PATH`, `--api-release-env PATH`, `--cleanup-env PATH`, `--deploy-env PATH`, and already built Compose `api`/`caddy` services.
- Produces: the idempotent `/opt/eurith/release-caddy-view` bind mount remounted `ro,nosymfollow`; status lines containing only `group=created|existing`, `storage=created|verified`, `release_view=created|verified`, `api_env=created|existing`, `cleanup_env=created|existing`, `deploy_env=updated`, `permissions=verified`; exit `0` only after host and container runtime probes pass.

- [ ] **Step 1: Write fake-root tests before scripts**

Use temporary directories and fake `docker`, `findmnt`, `mount`, `umount`, `getent`, `groupadd`, `install`, and `chown` executables prepended to `PATH`. Cover first use, repeat execution, pre-existing correct files, incorrect ownership/mode, malformed secret source, duplicate secret values, missing cleanup database source, resolved UID/GID propagation, API staging atomic write, creation/remount of the dedicated view, exact host/container `ro,nosymfollow` checks, regular-file reads, external-symlink denial, Caddy final read, and all Caddy mutation failures. Assert captured stdout/stderr never contains any supplied secret or full database URL.

- [ ] **Step 2: Run tests and observe RED**

Run: `python -m pytest tests/deploy/test_provision_release_host.py -q`

Expected: failures report that `deploy/provision-release-host.sh` and its shared library do not exist.

- [ ] **Step 3: Implement shared fail-closed helpers**

`release_common.sh` must enable `set -Eeuo pipefail`, provide `die`, `require_command`, `require_root`, `require_full_sha`, `require_safe_absolute_path`, `assert_mode_owner_group`, `assert_mount_vfs_options`, `assert_regular_read_and_external_symlink_denied`, `sha256_file`, and `redacted_status`. It must reject newline-bearing inputs, symlinks for protected files/directories, `/` as a mutable target, and paths below the checkout for secrets/backups. Host provisioning must never start Caddy if the view is absent, is an ordinary directory, lacks either `ro` or `nosymfollow`, or either host/container symlink probe succeeds unexpectedly.

Provisioning atomically installs the fixed non-secret regular marker `.eurith-release-view-gate-regular` in the source final directory and an exact sibling symlink `.eurith-release-view-gate-external -> /etc/passwd`, then creates/remounts the dedicated view. The marker names are outside the valid `<64-lowercase-hex>.apk` storage-key grammar. Existing entries must have the exact type, target/content, owner, group, and mode or provisioning fails without replacement. The Compose one-shot gate runs after provisioning and before Caddy; it has no network, write access, capabilities, secrets, or success path unless the container sees `ro,nosymfollow`, reads the regular marker, and cannot follow the external symlink.

- [ ] **Step 4: Implement first-use versus existing installation**

The secret source directory contains root-readable files `publisher-token`, `operator-token`, `webhook-secret`, and `cleanup-database-url`; tokens must each be exactly 64 lowercase hex characters and pairwise distinct. On first use, atomically install API and cleanup env files without echoing values. If either destination exists, verify exact key set, ownership, group, mode, and fixed storage-root value, report `existing`, and do not overwrite or compare/display secret values. A partial installation or changed protected file fails closed and requires the documented one-credential rotation procedure rather than implicit regeneration.

Resolve API UID with `docker compose --env-file "$DEPLOY_ENV" -f "$BASE_COMPOSE" -f "$RELEASE_OVERLAY" run --rm --no-deps --entrypoint id api -u`, resolve `eurith-releases` GID with `getent`, create the group only when absent, and install the exact setgid tree. Write only `RELEASE_SHARED_GID=<digits>` and `EURITH_SITE_ADDRESS=<validated host>` to root-owned `release-deploy.env`; these are non-secret.

- [ ] **Step 5: Implement runtime permission probes**

With the versioned overlay, prove API can create a `0600` staging fixture, atomically move it into `android/sha256`, and chmod final to `0640`; prove Caddy can read the final fixture but cannot create, replace, chmod, or delete it. Remove the named probe fixture through the API identity only. Any failed negative probe is a fatal error.

- [ ] **Step 6: Run tests, shell syntax checks, and commit**

```bash
python -m pytest tests/deploy/test_provision_release_host.py -q
bash -n deploy/lib/release_common.sh deploy/provision-release-host.sh
git add deploy/lib/release_common.sh deploy/provision-release-host.sh tests/deploy/test_provision_release_host.py
git commit -m "feat(deploy): provision protected release storage"
```

Expected: tests pass twice against the same fake root, second run reports existing/verified state, and no secret appears in output. Reviewer must inject world-readable modes, duplicate tokens, a symlink destination, a direct-tree/writable Caddy mount, a missing `nosymfollow` flag, and a container that follows the external symlink; each must be rejected.

### Task 5: Create and verify paired database and release-volume backups

**Files:**
- Create: `deploy/backup-release-state.sh`
- Create: `deploy/verify-release-restore.sh`
- Create: `tests/deploy/test_release_backup_restore.py`

**Interfaces:**
- Consumes: `backup-release-state.sh <full-backend-sha> <absolute-output-dir>` with `RELEASE_MUTATIONS_PAUSED=1`; protected cleanup DB URL file; `/opt/eurith/releases`.
- Produces: one private generation directory containing `database.dump`, `releases.tar`, and `manifest.sha256`; `verify-release-restore.sh <generation-dir> <isolated-db-url-file> <isolated-volume-root>` returns a redacted row/file count summary.

- [ ] **Step 1: Write deterministic failure tests first**

Fake `pg_dump`, `pg_restore`, `psql`, `tar`, and `sha256sum` to cover missing pause flag, short SHA, output below checkout/release mount, symlink output, empty dump, changed archive, missing APK, hash mismatch, size mismatch, escaped storage key, published row without artifact, mixed-generation manifest, and any attempt to restore into `/opt/eurith/releases` or a database not named `eurith_restore_*` on localhost.

- [ ] **Step 2: Run tests and observe RED**

Run: `python -m pytest tests/deploy/test_release_backup_restore.py -q`

Expected: failures identify both missing scripts.

- [ ] **Step 3: Implement the paused paired backup**

Create a root-only `0700` generation directory named with UTC timestamp and old backend SHA. Produce a PostgreSQL custom dump and archive the complete release root, including `.staging`. Build `manifest.sha256` with format version, UTC start/end, source SHA, database dump SHA-256, archive SHA-256, and sorted `storage_key<TAB>size<TAB>sha256` rows for every finalized APK. Query registry rows through `psql` using the protected URL file without command tracing. Abort and remove only the incomplete named generation if any published direct row lacks a regular in-root artifact or has mismatched size/hash.

- [ ] **Step 4: Implement isolated restore verification**

Require an empty non-production volume root and local database named `eurith_restore_*`. Restore dump and archive, compare manifest generation/hash, then query all direct-release rows and enforce storage-key containment, exact size/hash, and presence for every published row. Never mount-swap, alter production, downgrade, or print the database URL. Return only counts and manifest path/hash.

- [ ] **Step 5: Run fake-tool and real disposable round-trip tests**

```bash
python -m pytest tests/deploy/test_release_backup_restore.py -q
bash -n deploy/backup-release-state.sh deploy/verify-release-restore.sh
```

Then create unique local source and `eurith_restore_*` databases plus a temporary volume, seed one published and one withdrawn direct release, run both scripts, and verify zero missing/mismatched artifacts. Drop only those exact disposable databases and temporary roots after successful evidence capture.

- [ ] **Step 6: Commit and request independent recovery review**

```bash
git add deploy/backup-release-state.sh deploy/verify-release-restore.sh tests/deploy/test_release_backup_restore.py
git commit -m "feat(deploy): verify paired release backups"
```

Reviewer must restore the generated pair independently, corrupt one byte in a copied archive, and prove verification fails without touching the source generation.

### Task 6: Integrate exact-SHA deployment, canaries, and runbooks

**Files:**
- Modify: `deploy/deploy.sh`
- Create: `deploy/canary-release-delivery.sh`
- Modify: `deploy/README.md`
- Modify: `docs/releases/update-center-backend-checklist.md`
- Create: `tests/deploy/test_release_deploy_contract.py`

**Interfaces:**
- Consumes: `deploy.sh <full-40-hex-backend-sha> <full-40-hex-mobile-candidate-sha>`; base Compose path `EURITH_BASE_COMPOSE`; protected `/etc/eurith/release-deploy.env`; explicit canary URL `EURITH_PUBLIC_API_URL`.
- Produces: a deployment evidence directory outside checkout/release storage containing exact old/new backend SHA, mobile candidate SHA, Compose hash, Caddy tag/digest, Alembic heads/path, paired-backup manifest hash, command exit statuses, applicable canary results, and rollback result; no credentials or internal paths.

- [ ] **Step 1: Add failing ordering and safety tests**

Use fake commands to assert: clean checkout and exact remote commit containment before mutation; publication/cleanup pause before backup; paired backup and isolated restore before migration; exactly one Alembic head; Caddy fmt/adapt/validate and loopback harness before switch; API and Caddy switched from one Compose revision; public canaries after switch; automatic source/container rollback but never automatic database restore/downgrade; and mobile-gate output written only after all applicable canaries/log checks pass.

Add tests that a short/tag/branch SHA, dirty checkout, force-push instruction, destructive migration, missing Caddy digest, missing protected env file, rendered-secret persistence, or failed negative-auth/header/range probe stops deployment.

- [ ] **Step 2: Run tests and observe RED**

Run: `python -m pytest tests/deploy/test_release_deploy_contract.py -q`

Expected: current `deploy.sh` lacks the mobile SHA, paired backup, Caddy validation/switch, and gate contracts; canary script is absent.

- [ ] **Step 3: Refactor `deploy.sh` into the specified gate sequence**

Require two full SHAs. Fetch and resolve backend SHA exactly, verify it is contained by the approved remote integration/main ref, record old SHA, and refuse dirty state. Hash the base Compose, overlay, and Caddyfile. Read non-secret interpolation with `--env-file /etc/eurith/release-deploy.env`; never write `docker compose config` output. Run provisioning verification, paused paired backup, isolated restore drill, Caddy syntax plus loopback integration, one-head/migration-path checks, build exact API image, migrate once, then start API and Caddy together with the same two Compose files.

Rollback checks out the exact old SHA and restarts the prior API/Caddy/Compose revision only after schema compatibility is affirmed. It retains the paired backup and never restores it automatically. Any incompatible/destructive migration exits before migration and points to a separate expand/contract process.

- [ ] **Step 4: Implement production canaries without fake release rows**

`canary-release-delivery.sh` must check health/database, latest `Cache-Control: no-store`, absent/wrong publisher and operator rejection, webhook signature rejection, missing/withdrawn/non-direct download statuses using existing known IDs supplied through a protected runtime input file, direct internal-prefix denial and header non-leakage, unrelated public API behavior, container restarts, framing errors, permission denials, `5xx`, and free space >=20%.

If the registry already contains a published direct APK, query only its public ID/hash/size/ETag/Digest through authorized server-side tools and verify public full and single-range bytes. If none exists, record `existing_direct_apk=not_applicable` and do not insert one. All HTTP output is bounded and redacted.

- [ ] **Step 5: Replace nginx documentation and encode the backend-before-mobile gate**

Document exact first-use and existing-host commands, consumer-by-consumer secret installation/rotation, permission probes, backup/restore, deploy, canary, log/metric alert criteria, rollback, withdrawal-before-infrastructure-disable, and first-real-release closure. State that successful push is not deployment and successful backend deployment is not APK publication. Remove all deployable nginx instructions.

- [ ] **Step 6: Run focused and full local verification**

```bash
python -m pytest tests/test_main_release_routes.py tests/test_release_handoff_uvicorn.py tests/deploy -q
python -m pytest -q
python -m compileall api
python -c "from api.main import app; print(app.title)"
bash -n deploy/lib/release_common.sh deploy/*.sh
git diff --check
```

Run PostgreSQL integration tests only with an explicitly created guarded disposable database. Expected: zero failures; bounded environment skips are permitted only for Docker-dependent tests and must become passes on the release host before deployment.

- [ ] **Step 7: Commit and conduct two independent final reviews**

```bash
git add deploy/deploy.sh deploy/canary-release-delivery.sh deploy/README.md docs/releases/update-center-backend-checklist.md tests/deploy/test_release_deploy_contract.py
git commit -m "feat(deploy): gate Caddy release rollout"
```

One reviewer focuses on request routing, header leakage, secrets, permissions, and body limits. A second reviewer focuses on backup consistency, migrations, exact-SHA provenance, rollback, operational ordering, and mobile gating. Fix and re-review every Critical/High/Medium finding, then rerun Step 6 from a clean worktree.

### Task 7: Push and land the exact reviewed backend SHA

**Files:**
- No source changes.
- Evidence only: private release ledger outside Git containing reviewed local SHA, pushed remote SHA, landed backend `main` SHA, review identities, and CI URLs.

**Interfaces:**
- Consumes: clean `codex/direct-apk-release-backend` at the exact SHA approved by both Task 6 reviewers.
- Produces: remote integration ref and protected backend `main` containing that exact reviewed SHA; no force-push.

- [ ] **Step 1: Freeze and verify the candidate**

Run `git status --porcelain`, `git rev-parse HEAD`, `git diff --check`, `git log --oneline origin/main..HEAD`, and the complete Task 6 verification. Expected: clean tree, a full SHA, zero failures, and no untracked secret/env/backup/APK content.

- [ ] **Step 2: Push only the reviewed branch**

Run `git push -u origin codex/direct-apk-release-backend`. Fetch immediately and assert `git rev-parse origin/codex/direct-apk-release-backend` equals the frozen local SHA.

- [ ] **Step 3: Land through the repository's protected mechanism**

Open/update the normal protected review for integration branch to backend `main`; require CI and required approvals; never bypass branch protection or force-push. Record whether the protected mechanism fast-forwarded or created a merge SHA.

- [ ] **Step 4: Prove exact containment**

Fetch, record full `origin/main`, and run `git merge-base --is-ancestor <reviewed-integration-sha> origin/main`. Expected: exit `0`. Run tests against a clean checkout of the landed `origin/main`; deployment target is the landed full SHA, not a branch name.

### Task 8: Provision and deploy production, then release the backend gate

**Files:**
- No repository edits on the host; use only versioned scripts from the landed backend SHA.
- Evidence: protected operational record outside Git and release-serving storage.

**Interfaces:**
- Consumes: landed backend full SHA, frozen mobile candidate full SHA, base Compose path, protected secret-source directory supplied by the authorized secret owners, and approved Caddy v2.11.4 digest.
- Produces: deployed exact backend SHA, verified paired backup generation, successful backend canary gate, or a restored prior infrastructure revision with publication still paused.

- [ ] **Step 1: Coordinate the three secret consumers without exposing values**

Confirm publisher value exists in the protected GitHub production environment and the server input file; operator value exists in the authorized password vault and server input file; webhook value exists in the GitHub webhook secret and server input file. Confirm the three server source files are root-owned `0400`, independently generated 64-hex values, and pairwise distinct through boolean validation only. Never display, paste into command arguments, attach, or store their values in Git/evidence.

- [ ] **Step 2: Freeze release mutations and capture identities**

Pause publisher workflow, withdrawal/mandatory operations, and cleanup timer. Record exact previous/target backend SHA, Compose hashes, Caddy tag/digest, one Alembic head and production-to-target path, mobile candidate SHA, runtime API UID, shared GID, and clean checkout state. Abort if any identity cannot be proven.

- [ ] **Step 3: Provision or verify host state idempotently**

Run the landed `provision-release-host.sh` with protected runtime paths. First-use output may report created state; an existing production host must report existing/verified without rotating credentials. Confirm API atomic staging/finalization and Caddy read-only negative probes.

- [ ] **Step 4: Create and independently restore the paired backup**

Run `backup-release-state.sh <previous-backend-sha> <private-backup-root>` while paused. Restore it into an isolated `eurith_restore_*` database and empty isolated volume with `verify-release-restore.sh`. A single missing/mismatched file, dump error, archive mismatch, or mixed generation aborts deployment.

- [ ] **Step 5: Render safely, validate, and deploy exact SHA**

Run Caddy fmt/adapt/validate with the approved tag/digest, Compose config without persisting rendered output, and the loopback mounted-file harness. Then execute `deploy.sh <landed-backend-sha> <frozen-mobile-candidate-sha>`. Observe build, migration, API/Caddy coordinated start, health, and all canaries to terminal success.

- [ ] **Step 6: Review production evidence and decide the gate**

Inspect bounded Caddy/API logs and metrics for framing errors, handoff `502`, artifact `503`, sustained download `5xx`, permission denial, unexpected `404/413`, header leakage, restarts, failed ranges, and free space below 20%. If any applicable check fails, keep mobile blocked and execute versioned infrastructure rollback; withdraw affected advertised direct releases before disabling handoff. If all pass, record `backend_gate=passed` with exact SHA and timestamp.

### Task 9: Advance mobile only after the backend gate and finish the first APK canary

**Files:**
- Follow: `C:/Users/Admin/fitpilot-mobile/docs/superpowers/plans/2026-09-08-direct-apk-release-integration.md` Tasks 9-12, with this plan's backend gate as an additional prerequisite.
- No AAB or Play Store files/actions.

**Interfaces:**
- Consumes: `backend_gate=passed`, deployed backend exact SHA, clean reviewed `codex/direct-apk-release-mobile` exact SHA, protected mobile CI environment, and approved Android signing-certificate SHA-256.
- Produces: protected mobile `main` containing the exact reviewed candidate, then either an EAS update or a signed direct APK release selected by the existing workflow, plus verified first-real-release evidence.

- [ ] **Step 1: Revalidate the frozen mobile candidate**

Fetch both repositories and assert production backend still equals the passed SHA, backend health/canaries remain green, remote mobile integration head equals the reviewed local SHA, and mobile worktree/tests are clean. A changed SHA invalidates the gate and requires review again.

- [ ] **Step 2: Advance mobile `main` through branch protection**

Push the reviewed integration ref if not already present, use the normal protected merge mechanism, and prove `git merge-base --is-ancestor <reviewed-mobile-sha> origin/main`. Record the exact resulting mobile `main` merge SHA. Do not force-push, tag, build, or publish from the integration ref.

- [ ] **Step 3: Invoke the existing release workflow at exact mobile `main`**

Re-run the existing plan's release preflight and publisher tests, verify remote-main immediately before each mutation, and let the workflow select `direct_apk` only when the native fingerprint requires it. Do not request or generate an AAB and do not access Play Store publication.

- [ ] **Step 4: Close the first real direct-release canary when an APK is produced**

Verify latest discovery from an older direct client; full and single-range public downloads; exact registry size/SHA-256/ETag/Digest; approved Android certificate; package `com.adept.tracker`; version name/code; and safe install/smoke. No internal header/path may appear. If the workflow selects EAS update, keep the first-real-APK canary open until the next legitimate native APK rather than forcing a build.

- [ ] **Step 5: Resume or rollback safely**

On success, review logs once more and resume publisher, withdrawal/mandatory operations, and cleanup. On APK failure, withdraw the affected release first, retain its row/artifact/version code, keep backend infrastructure if healthy, and publish any correction only with a larger version code. Report backend/mobile SHAs, build/workflow/release IDs, version/hash/certificate and canary outcomes without tokens, signed secrets, private paths, or user data.

## Plan self-review record

- Spec coverage: HTTP framing, Caddy routing/header policy, exact upload cap, asymmetric mounts, identities/permissions, independent secrets, paired backup/restore, migration and exact-SHA gates, canaries/observability, rollback/withdrawal, and backend-before-mobile ordering each map to Tasks 1-9.
- Placeholder scan: no deferred implementation markers or unspecified error-handling steps remain; host-specific values are explicit runtime inputs with validation rather than invented constants.
- Interface consistency: `RELEASE_SHARED_GID`, `EURITH_SITE_ADDRESS`, protected env paths, Compose service names, guarded database prefixes, two-SHA deploy interface, and backend gate name are defined once and reused consistently.
