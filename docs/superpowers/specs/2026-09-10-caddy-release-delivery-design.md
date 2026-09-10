# Caddy Direct-APK Delivery Design

## Outcome

Production serves an authorized direct-release APK through Caddy without
streaming the artifact through Uvicorn. FastAPI remains the authority for
release state and filesystem validation; Caddy becomes the only component that
reads and transfers finalized APK bytes to a public client.

This design replaces the nginx-specific production assumption in the existing
update-center documentation. Production uses Caddy v2.11.4 under Docker
Compose, so nginx configuration is not a deployable production contract.

The public manifest contains a normal HTTPS URL of the form
`/app-releases/{release_id}/download`. It is not a cryptographically signed or
expiring URL and there is no object-storage dependency. Authenticity and
integrity come from the HTTPS connection, the manifest SHA-256/Digest, the
verified APK hash, and Android package signing. Authorization to receive bytes
comes from the current release state checked by FastAPI on every request.

## Scope

In scope:

- adapt the existing FastAPI `X-Accel-Redirect` handoff to Caddy;
- correct the empty-response HTTP framing contract;
- add production Compose mounts, request limits, protected environment files,
  backup steps, deployment gates, canaries, and rollback instructions;
- prove direct paths cannot bypass FastAPI and that Caddy supports byte ranges.

Out of scope:

- object storage, CDN migration, presigned URLs, or a second nginx proxy;
- proxying APK bodies through FastAPI;
- changing release-lane policy, version allocation, or mobile update UX;
- AAB generation, Google Play publication, or Android signing-key rotation;
- editing production files manually outside the versioned deployment process.

## Current contract and blocking defects

The download endpoint already performs the important authorization checks. It
loads a release by UUID, accepts only `direct_apk` plus `published`, returns
`410` for withdrawn releases, validates the content-addressed storage key,
rejects path escapes and symlinks, resolves the file below
`RELEASE_STORAGE_ROOT`, and verifies the stored size. On success it returns an
empty `200` response with `X-Accel-Redirect` pointing below
`/_release_files/`.

Two production blockers remain:

1. Caddy currently acts only as a reverse proxy. It does not interpret
   `X-Accel-Redirect`, has no read-only artifact mount, and can therefore return
   no APK bytes.
2. FastAPI currently attaches the full artifact `Content-Length` to its empty
   handoff response. Uvicorn/h11 correctly treats that header as a promise that
   FastAPI will emit that many body bytes; completing an empty response can
   fail HTTP framing before Caddy can perform the handoff.

The fix keeps the size check but removes `Content-Length` from the empty
FastAPI response. Caddy's `file_server`, not FastAPI, calculates the actual
response length and implements `HEAD`, `Range`, `Content-Range`, and
`Accept-Ranges` for the artifact it opens.

## Trust boundaries and threat model

### Assets

- publisher, operator, and GitHub webhook credentials;
- finalized APK bytes and their content-addressed filenames;
- release registry state, including withdrawal and mandatory-update state;
- Android signing key and the pinned signing-certificate digest;
- production database and release-volume backups.

### Trusted components

- Caddy terminates public TLS and is the only public entry point;
- FastAPI authorizes release state and resolves the storage key;
- PostgreSQL stores the immutable release record;
- the API runtime identity writes the release volume;
- the Caddy runtime identity reads only the finalized artifact subtree;
- the trusted release workflow may use the publisher token.

Uvicorn/container ports and the release host path are not publicly reachable.

### Threats and controls

| Threat | Required control |
| --- | --- |
| A client guesses a content-addressed path | Every public `/_release_files/*` request is rejected before proxying; only a qualifying upstream response can enter the file handler. |
| A compromised or buggy upstream selects another file | The handoff matcher requires status `200` and the exact `/_release_files/` prefix; FastAPI permits only `android/sha256/<64-lowercase-hex>.apk`; Caddy sees the final subtree only through a host bind view remounted `ro,nosymfollow`; host and container probes must prove that regular files work while an external symlink is denied. |
| An upstream leaks its internal handoff header | `X-Accel-Redirect` is consumed only inside the matching response route and removed from all public responses, including malformed and non-200 responses. |
| Header injection changes the filename or metadata | FastAPI validates `version_name`; Caddy copies only the four approved metadata headers and never copies the upstream length. Tests cover CR/LF rejection. |
| A large unauthenticated body exhausts API resources | A `256MiB` Caddy request-body ceiling applies only to `POST /internal/app-releases/android/direct-apk`; FastAPI retains its strict 250 MiB artifact limit. Other internal endpoints do not inherit the multipart allowance. |
| Caddy mutates or observes staging data | It receives a read-only mount of only the finalized `android/sha256` subtree; `.staging` is not mounted into Caddy. |
| A stolen secret crosses privilege boundaries | Three independently generated secrets have distinct consumers. No secret, env file, backup, or signing material enters Git, an image, application logs, or release artifacts. |
| A bad APK remains discoverable | The operator withdraws it first. Latest stops advertising it and download returns `410`; its version code remains consumed. |

## Request and response architecture

### Public flow

1. The mobile client gets an ordinary HTTPS download URL from the public latest
   endpoint.
2. Caddy proxies `GET` or `HEAD /app-releases/{release_id}/download` to FastAPI.
3. FastAPI checks the release row and validates the final file, including its
   recorded size. Failure remains a normal localized `404`, `410`, or `503`.
4. Success is an empty `200` handoff response containing exactly:
   `X-Accel-Redirect`, `Content-Type`, `Content-Disposition`, `ETag`, and
   `Digest`. It does **not** contain `Content-Length`.
5. Caddy accepts the handoff only when the upstream status is exactly `200` and
   the header begins with the exact internal prefix `/_release_files/`.
6. Caddy rewrites the internal value by stripping that prefix, resolves it
   against `/srv/eurith/releases`, and serves the existing regular file through
   `file_server`.
7. Caddy emits the actual `Content-Length` and range headers from the file it
   opened. The client verifies the expected SHA-256 before installation; Android
   then verifies the package signature.

FastAPI must retain the expected size as a typed local value for `stat()`
validation rather than reading it back from a response header. Removing the
header must not remove or weaken the size comparison.

### Caddy routing contract

The versioned Caddy configuration is implemented with ordered `route` blocks so
directive reordering cannot create a bypass. The executable configuration must
preserve all of these semantics:

1. A first public route returns `404` for every direct request whose path starts
   with `/_release_files/`; the request is never sent to FastAPI or
   `file_server`.
2. A matcher for the exact `POST /internal/app-releases/android/direct-apk`
   endpoint applies `request_body max_size 256MiB` and then uses the ordinary API
   reverse proxy. A rejected oversized request returns `413` without reaching
   FastAPI. No broad `/internal/*` or site-wide 256 MiB exception is allowed.
3. The ordinary reverse proxy contains a response matcher that is the logical
   conjunction of upstream status `200` and
   `X-Accel-Redirect: /_release_files/*`.
4. Its `handle_response` route rewrites from the header, strips the exact
   internal prefix, sets `root` to `/srv/eurith/releases`, and invokes
   `file_server`. It forwards only `Content-Type`, `Content-Disposition`,
   `ETag`, and `Digest` from FastAPI.
5. `X-Accel-Redirect` and the upstream `Content-Length` are never copied to the
   client. Any length, range, or last-modified metadata is generated from the
   opened file by Caddy.
6. A non-200 or malformed response carrying `X-Accel-Redirect` is treated as an
   upstream contract error, returns a bodyless `502`, and does not expose the
   internal header value. All other upstream responses preserve the normal API
   status and body while stripping any unexpected `X-Accel-Redirect` header.

The final Caddyfile syntax is verified against the pinned production Caddy
v2.11.4 image. A code comment beside the response matcher documents that its
status and header predicates are conjunctive and security-sensitive.

### Response-header policy

The handoff allowlist is deliberately small:

- `Content-Type: application/vnd.android.package-archive`;
- a validated attachment `Content-Disposition`;
- immutable SHA-based `ETag`;
- SHA-256 `Digest`.

The Caddy response owns `Content-Length`, `Accept-Ranges`, `Content-Range`,
`Last-Modified`, and the response body. Hop-by-hop headers, upstream cookies,
cache directives, `Server`, and `X-Accel-Redirect` are not copied from the
handoff. The normal latest endpoint remains `Cache-Control: no-store`.

## Storage and process permissions

The persistent host tree is `/opt/eurith/releases`:

```text
/opt/eurith/releases/                 owner=<API_UID>, group=eurith-releases, 2770
  .staging/                           owner=<API_UID>, group=eurith-releases, 2770
  android/                            owner=<API_UID>, group=eurith-releases, 2770
    sha256/                           owner=<API_UID>, group=eurith-releases, 2770
      <64-lowercase-hex>.apk          owner=<API_UID>, group=eurith-releases, 0640
```

`<API_UID>` is resolved from the built API image. The numeric GID is resolved
from the host `eurith-releases` group. No deployment instruction assumes image
UID/GID values. Setgid (`2`) on every directory preserves the shared group for
new descendants. Staging files remain `0600` until validation and atomic
finalization changes the final APK to `0640`.

Compose has asymmetric mounts:

- API: `/opt/eurith/releases:/var/lib/eurith/releases:rw`;
- Caddy:
  `/opt/eurith/release-caddy-view/android/sha256:/srv/eurith/releases/android/sha256:ro`;
- Caddy probe:
  `/opt/eurith/release-caddy-view/.probe:/run/eurith-release-view-probe:ro`.

`/opt/eurith/release-caddy-view` is a dedicated host tree outside API storage.
Host provisioning bind-mounts `/opt/eurith/releases/android/sha256` at its
`android/sha256` child and remounts that child with the per-mount VFS options
`ro,nosymfollow`. A separate `.probe` sibling contains a fixed non-secret regular
file and a symlink to `/etc/passwd`; it is also exposed `ro,nosymfollow`. The API
and cleanup jobs cannot see the probe sibling, and Caddy never mounts the
writable source tree directly. Deployment is blocked unless `findmnt` confirms
both options on the host views and inside the actual Caddy container, the regular
probe read succeeds, and the external symlink cannot be followed. This has been
proven feasible on the production kernel 6.8.0 and Docker Engine 29.7 line with
a disposable mount/container probe; the deployment repeats the proof instead of
relying on the earlier observation.

Compose uses long bind syntax with `create_host_path: false`, so a missing view
cannot silently become an ordinary directory. The pinned `caddy:2.11.4` service
has an explicit read-only `caddy-entrypoint.sh` wrapper and an empty Compose
command. On every container start or restart the wrapper reads
`/proc/self/mountinfo`, requires both exact Caddy mount targets to contain `ro`
and `nosymfollow`, reads the fixed regular probe, requires the external symlink
to resolve to `/etc/passwd`, and fails if that symlink can be opened. Only after
all checks pass does it `exec caddy run --config /etc/caddy/Caddyfile --adapter
caddyfile`; a stale successful sidecar cannot bypass a later failed check.

The API receives the resolved supplementary group. Caddy receives the same
group only to traverse/read final files; its bind mount is read-only even
though the shared host directory is group writable for the API. A deployment
check runs as each container identity: API can atomically create a staging file
and Caddy can read a final fixture, while Caddy cannot create, replace, chmod,
or delete it.

## Secrets and signing identity

The API service alone reads `/etc/eurith/api-release.env`, owned by root,
group-readable only by the deployment/API group, and mode `0640`. It contains:

```text
RELEASE_PUBLISHER_TOKEN=<independent 256-bit random value>
RELEASE_OPERATOR_TOKEN=<independent 256-bit random value>
GITHUB_WEBHOOK_SECRET=<independent 256-bit random value>
RELEASE_STORAGE_ROOT=/var/lib/eurith/releases
```

`/etc/eurith/release-cleanup.env` is also root-owned and mode `0640`; it contains
only the cleanup database connection and host-visible
`RELEASE_STORAGE_ROOT=/opt/eurith/releases`. Compose interpolation values that
are not application secrets, such as the resolved release GID, live in a
separate root-owned deployment env file. Caddy receives no publisher, operator,
webhook, database, or signing secret.

The three secrets are generated independently with a cryptographic RNG and are
never displayed after installation. Rotation is one credential at a time:

1. create the replacement in the authoritative vault;
2. update its one server env entry and the matching consumer (trusted workflow,
   operator vault, or GitHub webhook) without reusing another credential;
3. restart only the API and run positive and negative authentication canaries;
4. revoke the old value and record the rotation timestamp, not the value.

An emergency publisher-token rotation pauses publication until CI and the API
agree. A webhook-secret rotation pauses automatic lane advancement until a
signed test delivery succeeds. Secret values, env files, command traces, and
rendered Compose configuration must not be committed or attached to CI logs.

These credentials are unrelated to Android signing. The APK signing keystore
stays in the existing protected EAS/Android signing system, and the release
workflow pins only the approved signing-certificate digest. Rotating a backend
bearer or webhook secret does not change APK identity. Rotating the Android
signing key is a separate migration because an unrecognized certificate can
prevent installed clients from upgrading; it is explicitly not part of this
deployment.

## Backup and restore

Before the first Caddy-enabled deployment, publication, withdrawal, mandatory
changes, and cleanup are paused. Take a consistent pair:

1. a PostgreSQL custom-format dump with release registry rows;
2. a snapshot/archive of the complete `/opt/eurith/releases` volume, including
   finalized files and staging metadata;
3. a manifest containing backup timestamps, source commit, database dump
   SHA-256, and SHA-256 plus size for every finalized APK.

Publication remains paused until both artifacts and the manifest are durable.
The backup location is outside the checkout and release-serving mount, is
access-controlled, and is never copied into a container image or Git.

A restore drill uses an isolated database and volume. It restores the database,
verifies each registry storage key is contained below the isolated root, hashes
every restored APK against `artifact_sha256`, verifies its size, and confirms no
published row has a missing artifact before any production mount swap. Database
and release volume are restored as a pair; mixing generations is prohibited.

## Deployment sequence and gates

No mobile `main` merge/commit, release tag, EAS build, or APK publication is
created until the backend production canary gate below succeeds.

1. **Freeze and identify.** Pause release mutations and cleanup. Record the
   exact remote backend SHA, previous deployed SHA, Compose revision, Caddy
   v2.11.4 image digest, Alembic head, and mobile candidate SHA. Require clean
   worktrees and no force push.
2. **Pre-deploy tests.** Pass backend unit/integration tests, including the
   Uvicorn/h11 framing regression and a Caddy integration test using the pinned
   image. The test must observe real file bytes, not a mocked final response.
3. **Prepare host state.** Resolve actual API and Caddy identities, create the
   shared group and setgid directories, install root-owned env files, and add
   the API `rw` mount plus the dedicated Caddy `ro,nosymfollow` host view. Verify
   mount flags, regular reads, external-symlink denial, and write denial with the
   runtime identities before Caddy can start.
4. **Back up.** Create and verify the paired database and release-volume backup
   described above. Abort on any hash or missing-file discrepancy.
5. **Render and validate.** Run `docker compose config` with protected env input
   and inspect it without persisting rendered secrets. Run `caddy fmt --diff`,
   `caddy adapt --adapter caddyfile --pretty`, and
   `caddy validate --adapter caddyfile` inside the exact pinned image. An
   isolated loopback dry run mounts the real final-directory layout and uses a
   stub upstream to prove: valid handoff downloads and ranges work, direct
   internal paths fail, malformed/non-200 handoffs fail, and oversized publish
   requests receive `413`.
6. **Migration gate.** Confirm exactly one expected Alembic head and review the
   current production-to-target migration path. The current release-registry
   migration is expected to be additive, but that must be verified against the
   live starting head. Apply migrations once after backup and before switching
   the API. A failed code rollback does not automatically downgrade the schema;
   any destructive or incompatible migration stops this release and requires a
   separate expand/contract plan.
7. **Coordinated deploy.** Build from the exact backend SHA. Start the API and
   Caddy from the same versioned Compose revision so the no-`Content-Length`
   handoff and file handler become active together. Keep container ports private.
8. **Backend production canary.** Require public health and database checks;
   latest-endpoint `no-store`; missing/withdrawn/non-direct download statuses;
   wrong and absent publisher/operator authentication rejection; webhook
   signature rejection; direct `/_release_files/*` rejection; no internal
   header leakage; and normal unrelated API behavior. Repeat the isolated
   mounted-file handoff probe with the production image/config/directory
   identities. If an already-published direct APK exists, additionally require
   full and ranged public downloads whose bytes, length, ETag, Digest, and
   SHA-256 match the registry. Do not insert a fake production release row just
   to satisfy this probe.
9. **Release backend gate.** Review Caddy/API logs and metrics for framing
   errors, `5xx`, permission denials, unexpected `404/413`, header leakage, or
   restarts. Only after all applicable canaries pass may mobile `main` be
   created/advanced and the exact mobile SHA built.
10. **First real release canary.** Publish the verified signed APK through the
    protected workflow. Verify latest discovery from an older direct client,
    full download, a byte range, exact size and SHA-256, approved certificate,
    package/version metadata, and safe installation. This closes the only
    production check that cannot exist when the registry has no prior artifact.
11. **Resume.** Unfreeze release operations and cleanup only after the first
    real release canary and log review pass.

## Observability

Caddy access logs record request ID, route class, status, duration, bytes sent,
range status, and upstream duration. API audit logs record release UUID,
operation, result, source commit, CI run ID, and authenticated role. Neither log
may contain Authorization, webhook signature, env values, storage host paths,
`X-Accel-Redirect`, release notes, or uploaded body data.

Alert on repeated handoff `502`, artifact `503`, Caddy permission errors,
Uvicorn/h11 protocol errors, failed ranges, sustained download `5xx`, API/Caddy
restart loops, and release-volume free space below 20%. Correlate Caddy and API
events with a generated request ID without exposing the storage key.

## Rollback and withdrawal

Before an APK is published, a failed canary restores the previous backend SHA
and previous validated Caddy/Compose revision. The additive database schema is
left in place only after compatibility with the previous code is confirmed.
The paired backup is retained; an automatic schema downgrade or database reset
is forbidden.

After publication, application rollback and release withdrawal are separate:

1. withdraw the affected release through the protected endpoint so latest no
   longer advertises it and download returns `410`;
2. verify the withdrawn status publicly;
3. restore the previous API/Caddy revision if the infrastructure is faulty and
   schema compatibility has been confirmed;
4. keep the registry row, consumed version code, and artifact for audit and
   retention. Do not delete the APK as the first response and never reuse its
   version code;
5. publish a corrected direct APK with a strictly larger version code.

If Caddy cannot safely serve artifacts, withdraw all affected currently
advertised direct releases before disabling the handoff. Restore the paired
database/volume backup only for data corruption, after an isolated restore and
hash verification—not to undo ordinary release state.

## Test strategy

### Backend regression tests

- A valid published APK is stat-validated, yet the empty FastAPI response omits
  `Content-Length` and contains only the required handoff metadata.
- The route runs under a real Uvicorn/h11 server and completes without
  `LocalProtocolError`, premature disconnect, or promised-but-missing bytes.
- Missing, truncated, symlinked, escaped, malformed-key, unpublished, and
  withdrawn artifacts retain their fail-closed statuses and never hand off.
- CR/LF filename input cannot create another response header.

### Caddy integration tests

- Use the pinned Caddy v2.11.4 image, a real Uvicorn/h11 API, a temporary
  `ro,nosymfollow` bind view of the final subtree, and a valid database release
  row. The test must first assert that the flag is visible in the container.
- A full GET returns byte-identical APK data and Caddy-generated length.
- HEAD and single-range GET return correct headers/status/body; digest and ETag
  remain the FastAPI-approved values.
- Direct public access to the exact internal path and its encoded/path-traversal
  variants returns `404` and never opens a file.
- `200` plus the exact prefix succeeds; wrong status, absent header, alternate
  prefix, malformed path, missing file, and injected headers fail without
  leaking the internal value. A symlink in the final source tree targeting a
  readable file outside it must fail at the Caddy mount boundary even if the
  upstream deliberately selects that symlink.
- Upstream `Content-Length` is ignored; the actual file length wins.
- `256MiB` applies only to the exact direct-APK publish POST, the oversize case
  returns `413`, and normal endpoint limits/behavior are unchanged.
- Caddy can read but cannot write, rename, chmod, or delete the mounted APK;
  neither Caddy nor the host view follows a symlink outside the final subtree.

### Deployment and recovery tests

- `docker compose config`, Caddy format/adapt/validate, migration-head check,
  runtime permission probes, health checks, auth-negative checks, and public
  route canaries all pass from recorded exact image/commit identities.
- Backup restore into an isolated database/volume produces no missing or
  mismatched artifact.
- Withdrawal removes discovery, makes download return `410`, and does not free
  the version code or delete the artifact immediately.

## Acceptance criteria

- The production Compose revision runs the exact approved backend SHA and pinned
  Caddy v2.11.4 image with private application ports.
- API and Caddy have the required asymmetric mounts; staging is invisible to
  Caddy; final files are `0640` under setgid directories; Caddy receives only
  the dedicated host view whose `ro,nosymfollow` flags and external-symlink
  denial have been verified inside its container.
- Secrets are independently generated, root-owned at rest, absent from Git and
  logs, and scoped to their named consumers. Caddy receives none.
- FastAPI still validates artifact size but never sets full artifact
  `Content-Length` on its empty handoff response.
- Caddy accepts only `200` plus the exact internal prefix, exposes no direct
  internal route or handoff header, copies only the four approved metadata
  headers, and owns byte length/range delivery.
- The Uvicorn/h11 plus Caddy regression/integration suite passes before deploy.
- A verified database plus release-volume backup exists and its isolated restore
  has no hash or size mismatch.
- Backend deployment, migration checks, production canaries, and log review pass
  before any mobile `main` advancement or direct APK build/publication.
- The first real release is discoverable and its full/ranged downloads match the
  registry, APK signing certificate, package, version, size, and SHA-256.
- Failure invokes withdrawal and/or the versioned infrastructure rollback; it
  never uses a database reset, artifact-first deletion, version-code reuse, AAB,
  or Play Store action.
