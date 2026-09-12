# Release View Boot Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore and verify the protected Caddy APK views before Docker starts after every production host boot.

**Architecture:** Keep mount creation and verification in one fixed-path, root-owned shell helper. Provisioning installs that helper, a systemd oneshot unit, and a Docker dependency drop-in from versioned assets; deployment verifies their bytes/state and refuses to emit `backend_gate=passed` if the boot invariant is absent.

**Tech Stack:** Bash, systemd, Docker Compose, util-linux `mount`/`findmnt`, pytest shell harnesses.

**Spec:** `docs/superpowers/specs/2026-09-12-release-view-boot-persistence-design.md`

## Global Constraints

- Production APK delivery uses `/opt/eurith/release-caddy-view/android/sha256` bound from `/opt/eurith/releases/android/sha256` and `/opt/eurith/release-caddy-view/.probe` bound from `/opt/eurith/release-caddy-probe-source`.
- Both exact mountpoints must have `ro,nosymfollow`; an ancestor mount is not sufficient.
- The helper accepts no environment-controlled or command-line path overrides and logs no secrets.
- Docker must require and start after `eurith-release-views.service`; the service must run before Docker.
- Existing unexpected files, symlinks, metadata, bytes, mounts, or sources fail closed and are never silently replaced.
- A failed invocation unmounts only mounts created by that invocation, in reverse order.
- Caddy's existing container entrypoint remains an independent mount/security gate.
- No AAB or app-store publication work is part of this plan.
- Production reboot is a later manual deployment gate, never an automated test or provisioning side effect.

---

## File structure

- `deploy/systemd/eurith-release-views`: fixed-path mount/verification helper; no secret or application logic.
- `deploy/systemd/eurith-release-views.service`: oneshot boot unit.
- `deploy/systemd/docker-eurith-release-views.conf`: Docker dependency drop-in.
- `deploy/provision-release-host.sh`: atomically installs/verifies assets, enables the unit, invokes the helper, and then runs existing permission probes.
- `tests/deploy/test_release_view_boot_persistence.py`: executable helper failure/recovery tests using rendered temporary fixed paths and fake mount tools.
- `tests/deploy/test_provision_release_host.py`: installation, metadata, systemctl, idempotency, and provisioning-order tests.
- `deploy/deploy.sh`: preflight and final boot-invariant gate/evidence.
- `tests/deploy/test_release_deploy_contract.py`: executable deployment-gate scenarios.
- `deploy/README.md` and `docs/releases/update-center-backend-checklist.md`: operator installation, reboot proof, and recovery instructions.

---

### Task 1: Persistent mount helper and host provisioning

**Files:**
- Create: `deploy/systemd/eurith-release-views`
- Create: `deploy/systemd/eurith-release-views.service`
- Create: `deploy/systemd/docker-eurith-release-views.conf`
- Create: `tests/deploy/test_release_view_boot_persistence.py`
- Modify: `deploy/provision-release-host.sh`
- Modify: `tests/deploy/test_provision_release_host.py`

**Interfaces:**
- Consumes: the existing release storage/view/probe directory layout and the provisioning script's root-owned installation helpers.
- Produces: `/usr/local/libexec/eurith-release-views`, `/etc/systemd/system/eurith-release-views.service`, and `/etc/systemd/system/docker.service.d/eurith-release-views.conf`; stdout token `boot_mounts=verified` only after exact live verification.

- [ ] **Step 1: Add failing executable helper tests**

  Create a harness that copies the production helper into a temporary test tree and replaces only its four literal fixed path constants before execution. Put fake `mount`, `umount`, `findmnt`, and `readlink` executables first in `PATH`; persist fake mounts and calls as JSON/text. Do not replace the helper function bodies.

  Add separately named tests for first-start creation, verification-only repeat,
  reboot-like restoration, wrong existing source, missing `ro`, missing
  `nosymfollow`, every symlinked source/destination component, and a
  parametrized partial-failure test over `bind-final`, `remount-final`,
  `bind-probe`, and `remount-probe`.

  Each failure assertion must require nonzero exit, the symbolic error code, no `boot_mounts=verified`, and an exact remaining-mount set.

- [ ] **Step 2: Run the helper tests and verify RED**

  Run `python -m pytest tests/deploy/test_release_view_boot_persistence.py -q`.
  Expected: failure because `deploy/systemd/eurith-release-views` does not exist.

- [ ] **Step 3: Implement the minimal helper**

  Write a Bash helper with `set -Eeuo pipefail`, a cleanup trap, and literal constants for the four production paths. It must call `verify_path_chain_no_symlinks`, then `ensure_exact_bind` for final and probe views, and print `boot_mounts=verified` only at the end.

  `ensure_exact_bind` must distinguish absent exact mountpoints from existing ones using `findmnt --mountpoint`; existing mounts are verification-only. Newly absent mounts use `mount --bind SOURCE TARGET`, followed by `mount -o remount,bind,ro,nosymfollow TARGET`. Verification compares canonical exact source and target and parses comma-delimited VFS options. The cleanup trap records each newly bound target immediately after its successful bind and unmounts that list in reverse order on any later error.

- [ ] **Step 4: Run the helper tests and verify GREEN**

  Run the command from Step 2. Expected: all helper scenarios pass with no leaked temporary mount record.

- [ ] **Step 5: Add failing provisioning installation tests**

  Extend the existing fake command layer with `systemctl`. Assert first provisioning installs exact repository bytes with these contracts:

  ```text
  /usr/local/libexec/eurith-release-views                 root:root 0755
  /etc/systemd/system/eurith-release-views.service        root:root 0644
  /etc/systemd/system/docker.service.d/eurith-release-views.conf root:root 0644
  ```

  Assert the service contains `Type=oneshot`, `RemainAfterExit=yes`, `Before=docker.service`, `RequiresMountsFor=/opt/eurith/releases /opt/eurith/release-caddy-probe-source`, and `WantedBy=multi-user.target`. Assert the Docker drop-in contains `Requires=eurith-release-views.service` and `After=eurith-release-views.service`.

  Add tests for exact `daemon-reload`, `enable eurith-release-views.service`, helper invocation before existing Caddy probes, idempotent repeat, and fail-closed behavior for pre-existing wrong bytes/mode/owner/type/symlink. Require no `permissions=verified` on failures.

- [ ] **Step 6: Run provisioning tests and verify RED**

  Run `python -m pytest tests/deploy/test_provision_release_host.py -q`.
  Expected: new assertions fail because systemd assets are not installed or invoked.

- [ ] **Step 7: Add versioned units and integrate provisioning**

  Add the exact service and Docker drop-in described above. In `provision-release-host.sh`, require `systemctl`; validate that each source asset is a regular non-symlink file below the exact deploy asset root; install through a same-directory root-owned temporary file plus atomic rename; refuse drift rather than overwrite it. Run `systemctl daemon-reload`, `systemctl enable eurith-release-views.service`, invoke the installed helper directly, verify `systemctl is-enabled --quiet`, and only then continue to host/container permission probes.

  Keep the existing `--root` test mode contained: derive installation destinations below that root, and have the fake systemctl/helper harness execute the rendered temporary-path helper. Production with root `/` installs the unchanged fixed-path asset.

- [ ] **Step 8: Run Task 1 verification**

  Run `python -m pytest tests/deploy/test_release_view_boot_persistence.py tests/deploy/test_provision_release_host.py -q`, `bash -n deploy/systemd/eurith-release-views deploy/provision-release-host.sh`, and `git diff --check`. Expected: zero failures.

- [ ] **Step 9: Commit Task 1**

  Add the six Task 1 files and commit `feat(deploy): persist protected release views`.

---

### Task 2: Deployment gate, evidence, and operator recovery

**Files:**
- Modify: `deploy/deploy.sh`
- Modify: `tests/deploy/test_release_deploy_contract.py`
- Modify: `deploy/README.md`
- Modify: `docs/releases/update-center-backend-checklist.md`

**Interfaces:**
- Consumes: Task 1's installed helper/unit/drop-in and `boot_mounts=verified` live result.
- Produces: deployment evidence keys `boot_mount_assets=verified`, `boot_mount_unit=enabled`, `boot_mount_runtime=verified`; blocks `backend_gate=passed` until all three exist.

- [ ] **Step 1: Add failing deployment-gate tests**

  Extend the executable deploy harness, not a substring-only test. Add table cases where provisioning succeeds but each of the following is independently false: installed helper hash differs from target asset, unit hash differs, Docker drop-in hash differs, unit disabled, helper exits nonzero, final source/target/options drift, probe source/target/options drift. For every case assert nonzero deployment result, a specific symbolic error/evidence failure, and absence of `backend_gate=passed`.

  Add one success case that proves all three boot evidence keys occur before exactly one `backend_gate=passed`.

- [ ] **Step 2: Run deployment tests and verify RED**

  Run `python -m pytest tests/deploy/test_release_deploy_contract.py -q`.
  Expected: new boot-invariant scenarios fail because `deploy.sh` does not inspect the installed assets/unit/runtime.

- [ ] **Step 3: Implement the deployment gate**

  After exact-target provisioning and again immediately before final backend success evidence, call `verify_installed_boot_assets`, require `systemctl is-enabled --quiet eurith-release-views.service`, invoke `/usr/local/libexec/eurith-release-views`, and emit the three specified evidence keys.

  `verify_installed_boot_assets` requires regular non-symlink root-owned files, exact modes, and SHA-256 equality with the three files under the exact target deploy asset root. Retain the existing host and running-Caddy probes; the new check supplements rather than replaces them. Require `systemctl` in deploy preflight.

- [ ] **Step 4: Run deployment tests and verify GREEN**

  Run the command from Step 2. Expected: all existing and new deploy scenarios pass.

- [ ] **Step 5: Update operator documentation**

  Document inspection with `systemctl is-enabled`, `systemctl status --no-pager`, `systemctl cat` for the release unit and Docker, and exact `findmnt -n -o SOURCE,TARGET,VFS-OPTIONS --mountpoint` for both views.

  The recovery order is: repair the unexpected host entry; restart `eurith-release-views.service`; verify both mounts; then start/restart Docker and run bounded API/Caddy health/canary checks. State explicitly that mobile `main` and APK construction remain blocked until a controlled production reboot proves the boot order, exact mounts, zero container restarts, and public canaries.

- [ ] **Step 6: Run final plan verification**

  Run:

  ```powershell
  python -m pytest tests/deploy -q
  python -m pytest tests/test_release_storage.py tests/test_release_registry.py tests/test_release_migration.py tests/test_release_handoff_uvicorn.py tests/test_release_cleanup_guard.py tests/test_release_cleanup.py tests/test_release_auth.py tests/test_main_release_routes.py -q
  bash -n deploy/systemd/eurith-release-views deploy/provision-release-host.sh deploy/deploy.sh
  git diff --check
  ```

  Expected: zero failures, with environment-dependent skips only for their
  pre-existing explicit reasons.

- [ ] **Step 7: Commit Task 2**

  Add the four Task 2 files and commit `fix(deploy): gate release on boot-safe views`.

---

## Final review and production handoff

- [ ] Generate a whole-branch review package from `1a12fd0da15ca6e60b96e5f683cc171db030dd6d` through final HEAD and request independent security/operations review.
- [ ] Fix and re-review any Critical, High, or Medium finding before push.
- [ ] Verify clean worktree, exact commit ancestry, rollback candidate ancestry, and remote-main fast-forward.
- [ ] Push the rollback candidate branch, backend feature branch, and exact reviewed backend HEAD to `main` only after all checks pass.
- [ ] Prepare the separately reviewed root-only production bootstrap for PostgreSQL 17 client, deploy venv, protected secrets/files, disposable databases, exact Caddy digest, exact detached runner, and boot-persistence assets.
- [ ] Run deployment backup/restore/rehearsal/canary gates and a controlled reboot proof before advancing mobile `main` or building the production APK.
