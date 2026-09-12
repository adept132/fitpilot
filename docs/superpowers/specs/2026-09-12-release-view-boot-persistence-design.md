# Release View Boot Persistence Design

## Outcome

The two host views used by Caddy to serve direct APK releases are restored and
verified before Docker may restore production containers after a host reboot.
If either view cannot be mounted with the exact security properties required by
the release design, startup fails closed and Caddy cannot serve an unprotected
directory.

The protected views are:

- `/opt/eurith/release-caddy-view/android/sha256`, bound from
  `/opt/eurith/releases/android/sha256`;
- `/opt/eurith/release-caddy-view/.probe`, bound from
  `/opt/eurith/release-caddy-probe-source`.

Both exact mountpoints must be read-only and include `nosymfollow`.

## Scope

In scope:

- install a root-owned systemd oneshot service and helper from the versioned
  provisioning flow;
- order that service before Docker so restored Caddy containers never observe
  ordinary, unprotected destination directories;
- make installation and repeat provisioning idempotent and drift-detecting;
- verify exact mount source, mountpoint, `ro`, and `nosymfollow` at startup;
- fail closed on partial mounts and record actionable, non-secret errors;
- add executable tests for installation, ordering, successful restoration,
  drift, partial failure, and reboot-like repeated invocation;
- add deployment and runbook gates for enabled/active state and mount proof.

Out of scope:

- changing APK storage path names or release API behavior (ancestry ownership
  hardening is required for the source-path race fix);
- using AAB or a store publication path;
- changing Docker's system-wide restart policy;
- automatically rebooting production during provisioning;
- editing `/etc/fstab`.

## Chosen architecture

Provisioning installs an executable root-owned helper at
`/usr/local/libexec/eurith-release-views` and a root-owned unit at
`/etc/systemd/system/eurith-release-views.service`. It also installs the
root-owned Docker drop-in
`/etc/systemd/system/docker.service.d/eurith-release-views.conf`. The helper is
the single implementation of mount creation and verification used both during
provisioning and at boot.

The unit is `Type=oneshot` with `RemainAfterExit=yes`, is enabled for normal
multi-user boot, and declares `Before=docker.service`. The Docker drop-in adds
`Requires=eurith-release-views.service` and
`After=eurith-release-views.service`. Docker must not start when the helper
exits unsuccessfully. The unit also requires the local filesystems containing
`/opt/eurith` to be available first. The dependency graph must be checked with
`systemd-analyze verify` before enabling it.

The helper receives no secrets. All paths are fixed constants, not environment
or command-line inputs. It validates with `lstat`/canonical-path checks that no
source or destination component is a symlink and that every canonical path is
the expected absolute path. It creates no release data and never replaces an
unexpected entry.

The source and target parents have root-controlled ancestry, verified from `/`
down: real directories, owner UID 0, and no group/other write bits. This makes
each checked child non-replaceable by an API writer across validation, bind,
and identity verification. Storage root and `android` use root:shared-group
mode `2750`; `.staging` and `android/sha256` alone keep API-UID:shared-group
mode `2770`. The API can create/link/unlink artifact files and cleanup can
unlink within these leaves, but neither can replace a parent or leaf directory
entry. Existing writable ancestry fails closed and requires separately reviewed,
quiesced exact-directory ownership repair; no recursive automatic migration.

The effective systemd state is verified, not inferred from installed bytes:
loaded approved release fragment, no release drop-ins or transient/stale unit,
exact ExecStart with `/usr/bin/env -i` and fixed PATH/locale, approved execution
environment/mount namespace, required filesystems and Docker Before/Requires/
After relationship. Docker must use its vendor fragment and the single approved
drop-in. Provisioning checks before activation and again after verification;
deployment checks before build and immediately before backend-gate publication.

For each view the helper:

1. inspects the exact destination mountpoint with `findmnt --mountpoint`;
2. when absent, performs an exact bind mount from the fixed source;
3. remounts the bind with `bind,ro,nosymfollow`;
4. verifies the exact source, target, filesystem relationship, and both VFS
   options from `findmnt`;
5. fails if an existing mount has any source or option drift.

If this invocation created one or more mounts and a later step fails, it
unmounts only the mounts created by that invocation in reverse order. It never
unmounts a pre-existing verified mount. A rerun after a clean successful boot
is a no-op verification.

Provisioning writes the helper, unit, and Docker drop-in atomically with exact
owner/mode checks, reloads systemd, verifies the dependency graph, enables the
unit, and invokes it before any Caddy permission or container probe. Existing
files with different bytes, ownership, mode, type, or symlink status are
treated as drift and cause provisioning to stop; they are not silently
overwritten.

## Failure and recovery behavior

- Missing source directories, symlinks, wrong existing mount sources, absent
  `ro`/`nosymfollow`, or a failed bind/remount make the unit fail.
- Because Docker is ordered after and requires the unit, automatic container
  restoration does not proceed after that failure.
- Caddy's existing entrypoint remains a second independent gate inside the
  container and still refuses to start without the same mount properties.
- Operators repair the host condition and run `systemctl restart
  eurith-release-views.service`, then start/restart Docker. The helper emits
  symbolic error codes and paths only; it logs no credentials or database URLs.
- Deployment must not record `backend_gate=passed` unless the unit is enabled,
  is active, its exact installed bytes and metadata match the target release,
  and both live mountpoints pass the existing host and container probes.

## Alternatives rejected

### `/etc/fstab`

Although compact, fstab makes the two-phase bind/remount contract, exact-source
drift checks, rollback of partial setup, and actionable fail-closed behavior
harder to express and test. It also spreads the security contract between
generated fstab syntax and provisioning code.

### Re-run provisioning manually after reboot

This leaves a window where Docker can restore Caddy before protection exists
and depends on an operator noticing every reboot. It is not an acceptable
production invariant.

### Let the Caddy entrypoint be the only gate

The entrypoint does fail closed, but direct APK delivery would remain down
after every reboot. Host-level ordering restores the views deterministically;
the entrypoint remains defense in depth.

## Verification

Automated tests must execute the installed helper through fake mount/findmnt
and systemctl interfaces rather than merely inspect shell substrings. They
cover:

- first installation and idempotent repeat;
- unit bytes, owner/mode, enablement, and Docker ordering;
- successful first mount and reboot-like restoration after mounts disappear;
- already-correct mounts as a verification-only no-op;
- wrong source, missing `ro`, missing `nosymfollow`, symlinked paths, and
  unexpected pre-existing files;
- failure at each bind/remount step and rollback of only invocation-owned
  mounts;
- Docker remaining blocked when the unit fails;
- no premature `permissions=verified` or deployment success evidence.

Before production acceptance, a controlled reboot must prove:

1. the unit completed before Docker;
2. both exact host mountpoints are `ro,nosymfollow` and have the expected
   sources;
3. API and Caddy containers use the approved images, remain running with zero
   restarts, and pass bounded health checks;
4. Caddy can read a regular probe but cannot follow the external symlink or
   mutate the release view;
5. public health and release-delivery canaries still pass.

Mobile `main` advancement and APK construction remain blocked until this
post-reboot backend gate succeeds.
