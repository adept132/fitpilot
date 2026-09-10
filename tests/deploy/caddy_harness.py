"""Release-host integration harness for the exact production Caddyfile.

This module deliberately imports no application/database module at import time.
The guarded disposable URL is validated and installed before the application is
loaded by :meth:`CaddyHarness.start_or_skip`.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import http.client
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import TracebackType
from typing import Any, Iterator
from urllib.parse import urlparse
from uuid import UUID, uuid4

import pytest

from tests.integration.database_test_guard import require_disposable_integration_database


FIXTURE_BYTES = b"EURITH-CADDY-RANGE-FIXTURE"
MAX_CAPTURE_BYTES = 64 * 1024
CADDY_IMAGE = os.environ.get("CADDY_TEST_IMAGE", "caddy:2.11.4")
_INTERNAL_PREFIX = "/_release_files/"
_REDACTIONS = (
    re.compile(r"(?im)^(authorization\s*:\s*).*$"),
    re.compile(r"(?im)^(x-hub-signature-256\s*:\s*).*$"),
    re.compile(r"(?im)^(x-accel-redirect\s*:\s*).*$"),
    re.compile(r"postgresql\+asyncpg://[^\s]+", re.I),
    re.compile(r"(?im)^[A-Z][A-Z0-9_]*=.*$"),
    re.compile(
        r'(?i)("?(?:authorization|x-hub-signature-256|x-accel-redirect)"?\s*:\s*)'
        r'(?:\[[^\]\r\n]*\]|"[^"\r\n]*"|[^\s,}\r\n]*)'
    ),
    re.compile(r"/_release_files/[^\s\"',}]*", re.I),
)


def require_caddy_database(env: dict[str, str] | None = None) -> str:
    """Narrow the shared integration guard to this harness's unique namespace."""
    source = os.environ if env is None else env
    url = require_disposable_integration_database(source)
    database_name = urlparse(url).path.lstrip("/")
    if re.fullmatch(r"fitpilot_task_caddy_[a-z0-9][a-z0-9_]*", database_name) is None:
        raise RuntimeError(
            "Caddy integration requires a unique local fitpilot_task_caddy_* database"
        )
    return url


def sanitize_output(value: str | bytes) -> str:
    """Bound subprocess diagnostics and remove every protected value class."""
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
    for pattern in _REDACTIONS:
        text = pattern.sub(lambda match: f"{match.group(1) if match.lastindex else ''}[REDACTED]", text)
    encoded = text.encode("utf-8", "replace")
    if len(encoded) > MAX_CAPTURE_BYTES:
        encoded = encoded[: MAX_CAPTURE_BYTES - len(b"\n[TRUNCATED]")]
        text = encoded.decode("utf-8", "ignore") + "\n[TRUNCATED]"
    return text


def _run(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
    timeout: int = 60,
) -> subprocess.CompletedProcess[bytes]:
    process = subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout = bytearray()
    stderr = bytearray()

    def drain(stream: Any, destination: bytearray) -> None:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            remaining = MAX_CAPTURE_BYTES - len(destination)
            if remaining > 0:
                destination.extend(chunk[:remaining])

    stdout_thread = threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True)
    stderr_thread = threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        stdout_thread.join()
        stderr_thread.join()
        raise RuntimeError(
            f"command timed out: {args[0]}\n"
            f"stdout={sanitize_output(stdout)}\n"
            f"stderr={sanitize_output(stderr)}"
        ) from exc
    stdout_thread.join()
    stderr_thread.join()
    completed = subprocess.CompletedProcess(args, returncode, bytes(stdout), bytes(stderr))
    if check and completed.returncode:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {args[0]}\n"
            f"stdout={sanitize_output(completed.stdout)}\n"
            f"stderr={sanitize_output(completed.stderr)}"
        )
    return completed


def _reserve_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Response:
    def __init__(self, status: int, headers: list[tuple[str, str]], body: bytes) -> None:
        self.status = status
        self.headers = {key.lower(): value for key, value in headers}
        self.body = body


class _TestControlRouter:
    """Test-only ASGI boundary around the real application."""

    def __init__(self, app: Any, storage_key: str) -> None:
        self.app = app
        self.storage_key = storage_key
        self.counts = {"all": 0, "publish": 0, "other": 0}

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path.startswith("/__caddy_test/counter/"):
            name = path.rsplit("/", 1)[-1]
            value = str(self.counts.get(name, -1)).encode()
            await self._response(send, 200, [(b"content-length", str(len(value)).encode())], value)
            return

        self.counts["all"] += 1
        if path == "/internal/app-releases/android/direct-apk":
            self.counts["publish"] += 1
            await self._response(send, 204, [], b"")
            return
        if path == "/__caddy_test/unrelated":
            self.counts["other"] += 1
            await self._response(send, 204, [], b"")
            return
        if path.startswith("/__caddy_test/handoff/"):
            variant = path.rsplit("/", 1)[-1]
            internal = (_INTERNAL_PREFIX + self.storage_key).encode()
            if variant == "absent":
                await self._response(send, 418, [], b"ordinary-error")
            elif variant == "malformed":
                await self._response(send, 200, [(b"x-accel-redirect", b"/not-internal/file")], b"")
            elif variant == "non200":
                await self._response(send, 404, [(b"x-accel-redirect", internal)], b"hidden")
            elif variant == "false-length":
                headers = self._valid_handoff_headers(internal)
                headers.append((b"content-length", b"999"))
                await self._response(send, 200, headers, b"x" * 999)
            elif variant == "symlink-raw":
                symlink = (_INTERNAL_PREFIX + f"android/sha256/{'2' * 64}.apk").encode()
                await self._response(send, 200, self._valid_handoff_headers(symlink), b"")
            elif variant == "injected":
                # h11 must reject this before it can cross the proxy boundary.
                await self._response(
                    send,
                    200,
                    [(b"x-accel-redirect", internal + b"\r\nX-Injected: yes")],
                    b"",
                )
            else:
                await self._response(send, 404, [], b"")
            return
        await self.app(scope, receive, send)

    @staticmethod
    def _valid_handoff_headers(internal: bytes) -> list[tuple[bytes, bytes]]:
        digest = base64.b64encode(hashlib.sha256(FIXTURE_BYTES).digest())
        return [
            (b"x-accel-redirect", internal),
            (b"content-type", b"application/vnd.android.package-archive"),
            (b"content-disposition", b'attachment; filename="eurith-1.2.3.apk"'),
            (b"etag", b'"sha256:' + hashlib.sha256(FIXTURE_BYTES).hexdigest().encode() + b'"'),
            (b"digest", b"sha-256=" + digest),
        ]

    @staticmethod
    async def _response(
        send: Any, status: int, headers: list[tuple[bytes, bytes]], body: bytes
    ) -> None:
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


class CaddyHarness:
    """Own all ephemeral processes, rows, mounts, and release fixtures."""

    def __init__(self, database_url: str) -> None:
        self.repo = Path(__file__).resolve().parents[2]
        self.database_url = database_url
        self.temp = tempfile.TemporaryDirectory(prefix="eurith-caddy-harness-")
        self.root = Path(self.temp.name)
        self.source_root = self.root / "source"
        self.final_source = self.source_root / "android/sha256"
        self.view_final = self.root / "view/android/sha256"
        self.probe_source = self.root / "probe-source"
        self.view_probe = self.root / "view/.probe"
        self.outside = self.root / "outside-readable"
        self.outside_bytes = b"OUTSIDE-MUST-NEVER-BE-SERVED"
        self.sha256 = hashlib.sha256(FIXTURE_BYTES).hexdigest()
        self.storage_key = f"android/sha256/{self.sha256}.apk"
        self.artifact = self.final_source / f"{self.sha256}.apk"
        self.release_ids: dict[str, UUID] = {}
        self.container = f"eurith-caddy-test-{uuid4().hex[:12]}"
        self.api_port = _reserve_port()
        self.caddy_port = _reserve_port()
        self.server: Any = None
        self.thread: threading.Thread | None = None
        self._mounted = False
        self._probe_mounted = False
        self.host_mount_flags: set[str] = set()
        self.container_mount_flags: set[str] = set()
        self.host_probe_mount_flags: set[str] = set()
        self.container_probe_mount_flags: set[str] = set()
        self.host_regular_read_succeeded = False
        self.host_symlink_read_denied = False
        self.host_probe_regular_read_succeeded = False
        self.host_probe_symlink_read_denied = False
        self.container_regular_read_succeeded = False
        self.container_symlink_read_denied = False
        self.container_probe_regular_read_succeeded = False
        self.container_probe_symlink_read_denied = False
        self.container_mount_sources = ""
        self.caddy_mutations_denied: set[str] = set()

    @classmethod
    def production_caddyfile(cls) -> Path:
        return Path(__file__).resolve().parents[2] / "deploy/caddy/Caddyfile"

    @classmethod
    @contextlib.contextmanager
    def start_or_skip(cls) -> Iterator["CaddyHarness"]:
        reason = cls._unavailable_reason()
        if reason:
            pytest.skip(f"caddy_integration runtime gate unavailable: {reason}")
        database_url = require_caddy_database()
        harness = cls(database_url)
        try:
            harness.start()
            yield harness
        finally:
            harness.close()

    @staticmethod
    def _unavailable_reason() -> str | None:
        if sys.platform != "linux":
            return "requires a Linux release host for bind remount nosymfollow"
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            return "requires root for an isolated bind remount"
        for command in ("docker", "mount", "umount", "findmnt"):
            if shutil.which(command) is None:
                return f"missing required executable: {command}"
        url = os.environ.get("TEST_DATABASE_URL", "").strip()
        try:
            require_caddy_database(
                {"TEST_DATABASE_URL": url} if url else {}
            )
        except RuntimeError:
            return "TEST_DATABASE_URL must name unique local fitpilot_task_caddy_* database"
        image = _run(
            ["docker", "image", "inspect", CADDY_IMAGE],
            cwd=Path.cwd(),
            check=False,
            timeout=20,
        )
        if image.returncode:
            return f"pinned image is not installed: {CADDY_IMAGE}"
        return None

    @property
    def download_path(self) -> str:
        return f"/app-releases/{self.release_ids['valid']}/download"

    def download_variant_path(self, variant: str) -> str:
        return f"/app-releases/{self.release_ids[variant]}/download"

    @property
    def etag(self) -> str:
        return f'"sha256:{self.sha256}"'

    @property
    def digest(self) -> str:
        return f"sha-256={base64.b64encode(bytes.fromhex(self.sha256)).decode()}"

    def start(self) -> None:
        self._prepare_files_and_mount()
        env = os.environ.copy()
        env["DATABASE_URL"] = self.database_url
        env["RELEASE_STORAGE_ROOT"] = str(self.source_root)
        _run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=self.repo,
            env=env,
            timeout=120,
        )
        os.environ["DATABASE_URL"] = self.database_url
        os.environ["RELEASE_STORAGE_ROOT"] = str(self.source_root)
        main = importlib.import_module("api.main")
        database = importlib.import_module("app.database")
        models = importlib.import_module("api.services.models")
        asyncio.run(self._seed(database.SessionLocal, models.AppRelease))
        asyncio.run(database.engine.dispose())
        self._engine = database.engine
        router = _TestControlRouter(main.app, self.storage_key)
        uvicorn = importlib.import_module("uvicorn")
        self.server = uvicorn.Server(
            uvicorn.Config(
                router,
                host="127.0.0.1",
                port=self.api_port,
                http="h11",
                lifespan="off",
                log_level="warning",
            )
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        self._wait_http(self.api_port, "/__caddy_test/counter/all")
        self._start_caddy()

    async def _seed(self, session_factory: Any, app_release: Any) -> None:
        variants = {
            "valid": (self.storage_key, self.sha256, len(FIXTURE_BYTES)),
            "wrong-size": (self.storage_key, self.sha256, len(FIXTURE_BYTES) + 1),
            "missing": (f"android/sha256/{'1' * 64}.apk", "1" * 64, 10),
            "symlink": (f"android/sha256/{'2' * 64}.apk", "2" * 64, len(self.outside_bytes)),
        }
        async with session_factory() as session:
            for index, (name, (key, digest, size)) in enumerate(variants.items(), 1):
                release_id = uuid4()
                self.release_ids[name] = release_id
                session.add(
                    app_release(
                        id=release_id,
                        platform="android",
                        channel="production-direct",
                        delivery_method="direct_apk",
                        version_code=900000 + index,
                        version_name=f"900.{index}.0",
                        release_notes={"ru": "test", "en": "test"},
                        status="published",
                        is_mandatory=False,
                        artifact_storage_key=key,
                        artifact_sha256=digest,
                        artifact_size_bytes=size,
                        source_commit="a" * 40,
                        ci_run_id=f"caddy-harness-{index}-{release_id}",
                        idempotency_key=f"caddy-harness-{release_id}",
                    )
                )
            await session.commit()
        self._session_factory = session_factory
        self._app_release = app_release

    def _prepare_files_and_mount(self) -> None:
        self.final_source.mkdir(parents=True)
        self.view_final.mkdir(parents=True)
        self.probe_source.mkdir(parents=True)
        self.view_probe.mkdir(parents=True)
        self.artifact.write_bytes(FIXTURE_BYTES)
        self.outside.write_bytes(self.outside_bytes)
        (self.final_source / f"{'2' * 64}.apk").symlink_to(self.outside)
        (self.probe_source / "regular").write_bytes(b"P")
        (self.probe_source / "external").symlink_to("/etc/passwd")
        self.host_mount_flags = self._mount_protected(self.final_source, self.view_final)
        self._mounted = True
        self.host_probe_mount_flags = self._mount_protected(self.probe_source, self.view_probe)
        self._probe_mounted = True
        options = ",".join(sorted(self.host_mount_flags))
        if not {"ro", "nosymfollow"} <= self.host_mount_flags:
            raise RuntimeError(f"host release view lacks required flags: {sanitize_output(options)}")
        if not {"ro", "nosymfollow"} <= self.host_probe_mount_flags:
            raise RuntimeError("host probe view lacks required flags")
        self.host_regular_read_succeeded = (
            (self.view_final / self.artifact.name).read_bytes() == FIXTURE_BYTES
        )
        try:
            (self.view_final / f"{'2' * 64}.apk").read_bytes()
        except OSError:
            self.host_symlink_read_denied = True
        if not self.host_regular_read_succeeded or not self.host_symlink_read_denied:
            raise RuntimeError("host release view regular/symlink behavior gate failed")
        self.host_probe_regular_read_succeeded = (
            (self.view_probe / "regular").read_bytes() == b"P"
        )
        try:
            (self.view_probe / "external").read_bytes()
        except OSError:
            self.host_probe_symlink_read_denied = True
        if not self.host_probe_regular_read_succeeded or not self.host_probe_symlink_read_denied:
            raise RuntimeError("host probe-view regular/symlink behavior gate failed")

    def _mount_protected(self, source: Path, target: Path) -> set[str]:
        _run(["mount", "--bind", str(source), str(target)], cwd=self.repo)
        try:
            _run(
                ["mount", "-o", "remount,bind,ro,nosymfollow", str(target)],
                cwd=self.repo,
            )
        except Exception:
            _run(["umount", str(target)], cwd=self.repo, check=False)
            raise
        options = _run(
            ["findmnt", "-n", "-o", "OPTIONS", "--target", str(target)],
            cwd=self.repo,
        ).stdout.decode()
        return set(options.strip().split(","))

    def _start_caddy(self) -> None:
        command = [
            "docker", "run", "--detach", "--name", self.container,
            "--network", "host", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--tmpfs", "/data:rw,noexec,nosuid,nodev",
            "--tmpfs", "/config:rw,noexec,nosuid,nodev",
            "--mount", f"type=bind,src={self.production_caddyfile()},dst=/etc/caddy/Caddyfile,readonly",
            "--mount", f"type=bind,src={self.view_final},dst=/srv/eurith/releases/android/sha256,readonly",
            "--mount", f"type=bind,src={self.view_probe},dst=/run/eurith-release-view-probe,readonly",
            "--env", f"EURITH_SITE_ADDRESS=http://127.0.0.1:{self.caddy_port}",
            "--env", f"EURITH_UPSTREAM=127.0.0.1:{self.api_port}",
            "--env", "RELEASE_FILE_ROOT=/srv/eurith/releases",
            CADDY_IMAGE, "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile",
        ]
        _run(command, cwd=self.repo, timeout=60)
        try:
            self._probe_container()
            self._wait_http(self.caddy_port, "/__caddy_test/counter/all")
        except Exception as exc:
            logs = _run(
                ["docker", "logs", self.container], cwd=self.repo, check=False, timeout=20
            )
            raise RuntimeError(
                f"Caddy startup/probe failed: {exc}\nlogs={sanitize_output(logs.stdout + logs.stderr)}"
            ) from exc

    def _probe_container(self) -> None:
        target = "/srv/eurith/releases/android/sha256"
        probe_target = "/run/eurith-release-view-probe"
        self.container_mount_flags = self._container_mount_options(target)
        self.container_probe_mount_flags = self._container_mount_options(probe_target)
        if not {"ro", "nosymfollow"} <= self.container_mount_flags:
            raise RuntimeError("container release view lacks required flags")
        if not {"ro", "nosymfollow"} <= self.container_probe_mount_flags:
            raise RuntimeError("container probe view lacks required flags")
        regular = _run(
            ["docker", "exec", self.container, "cat", f"{target}/{self.artifact.name}"],
            cwd=self.repo,
            check=False,
        )
        self.container_regular_read_succeeded = regular.returncode == 0 and regular.stdout == FIXTURE_BYTES
        symlink = _run(
            ["docker", "exec", self.container, "cat", f"{target}/{'2' * 64}.apk"],
            cwd=self.repo,
            check=False,
        )
        self.container_symlink_read_denied = symlink.returncode != 0
        probe_regular = _run(
            ["docker", "exec", self.container, "cat", f"{probe_target}/regular"],
            cwd=self.repo,
            check=False,
        )
        self.container_probe_regular_read_succeeded = (
            probe_regular.returncode == 0 and probe_regular.stdout == b"P"
        )
        probe_symlink = _run(
            ["docker", "exec", self.container, "cat", f"{probe_target}/external"],
            cwd=self.repo,
            check=False,
        )
        self.container_probe_symlink_read_denied = probe_symlink.returncode != 0
        if not self.container_regular_read_succeeded or not self.container_symlink_read_denied:
            raise RuntimeError("container regular/symlink behavior gate failed")
        if not self.container_probe_regular_read_succeeded or not self.container_probe_symlink_read_denied:
            raise RuntimeError("container probe-view regular/symlink behavior gate failed")

        inspected = _run(
            ["docker", "inspect", self.container, "--format", "{{json .Mounts}}"], cwd=self.repo
        )
        mounts = json.loads(inspected.stdout)
        self.container_mount_sources = "\n".join(str(item.get("Source", "")) for item in mounts)
        release_mount = next(item for item in mounts if item.get("Destination") == target)
        if release_mount.get("RW") is not False or Path(release_mount["Source"]).resolve() != self.view_final.resolve():
            raise RuntimeError("container release bind is not the exact read-only final view")
        probes = {
            "write": ["touch", f"{target}/forbidden"],
            "rename": ["mv", f"{target}/{self.artifact.name}", f"{target}/moved.apk"],
            "chmod": ["chmod", "777", f"{target}/{self.artifact.name}"],
            "delete": ["rm", f"{target}/{self.artifact.name}"],
        }
        for name, operation in probes.items():
            result = _run(
                ["docker", "exec", self.container, *operation],
                cwd=self.repo,
                check=False,
            )
            if result.returncode:
                self.caddy_mutations_denied.add(name)
        if self.caddy_mutations_denied != set(probes):
            raise RuntimeError("Caddy final-view mutation denial gate failed")

    def _container_mount_options(self, target: str) -> set[str]:
        program = f'$5 == "{target}" {{ print $6; found = 1; exit }} END {{ if (!found) exit 1 }}'
        result = _run(
            ["docker", "exec", self.container, "/usr/bin/awk", program, "/proc/self/mountinfo"],
            cwd=self.repo,
        )
        return set(result.stdout.decode().strip().split(","))

    @staticmethod
    def _wait_http(port: int, path: str) -> None:
        deadline = time.monotonic() + 20
        last = "not attempted"
        while time.monotonic() < deadline:
            try:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                connection.request("GET", path)
                response = connection.getresponse()
                response.read()
                connection.close()
                return
            except OSError as exc:
                last = str(exc)
                time.sleep(0.1)
        raise RuntimeError(f"HTTP process did not become ready: {sanitize_output(last)}")

    def request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> Response:
        connection = http.client.HTTPConnection("127.0.0.1", self.caddy_port, timeout=20)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        result = Response(response.status, response.getheaders(), response.read())
        connection.close()
        return result

    def request_large_publish(self, size: int) -> Response:
        connection = http.client.HTTPConnection("127.0.0.1", self.caddy_port, timeout=20)
        chunk = b"x" * (1024 * 1024)

        def chunks() -> Iterator[bytes]:
            remaining = size
            while remaining:
                part = chunk if remaining >= len(chunk) else chunk[:remaining]
                remaining -= len(part)
                yield part

        connection.request(
            "POST",
            "/internal/app-releases/android/direct-apk",
            body=chunks(),
            headers={"Content-Length": str(size), "Content-Type": "application/octet-stream"},
            encode_chunked=False,
        )
        response = connection.getresponse()
        result = Response(response.status, response.getheaders(), response.read())
        connection.close()
        return result

    def counter(self, name: str) -> int:
        response = self.request("GET", f"/__caddy_test/counter/{name}")
        assert response.status == 200
        return int(response.body)

    def close(self) -> None:
        _run(["docker", "rm", "-f", self.container], cwd=self.repo, check=False, timeout=30)
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=10)
        if hasattr(self, "_session_factory") and self.release_ids:
            with contextlib.suppress(Exception):
                asyncio.run(self._engine.dispose())
                asyncio.run(self._delete_rows())
                asyncio.run(self._engine.dispose())
        if self._probe_mounted:
            _run(["umount", str(self.view_probe)], cwd=self.repo, check=False, timeout=30)
            self._probe_mounted = False
        if self._mounted:
            _run(["umount", str(self.view_final)], cwd=self.repo, check=False, timeout=30)
            self._mounted = False
        self.temp.cleanup()

    async def _delete_rows(self) -> None:
        sqlalchemy = importlib.import_module("sqlalchemy")
        async with self._session_factory() as session:
            await session.execute(
                sqlalchemy.delete(self._app_release).where(
                    self._app_release.id.in_(list(self.release_ids.values()))
                )
            )
            await session.commit()

    def __enter__(self) -> "CaddyHarness":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
