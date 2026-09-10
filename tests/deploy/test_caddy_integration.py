from __future__ import annotations

import hashlib
from pathlib import Path
import sys

import pytest

from tests.deploy.caddy_harness import (
    CADDY_IMAGE,
    FIXTURE_BYTES,
    MAX_CAPTURE_BYTES,
    CaddyHarness,
    _run,
    require_caddy_database,
    sanitize_output,
)


pytestmark = pytest.mark.caddy_integration


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://localhost/fitpilot",
        "postgresql+asyncpg://localhost/fitpilot_integration_a1b2c3",
        "postgresql+asyncpg://localhost/fitpilot_task7_a1b2c3",
        "postgresql+asyncpg://remote/fitpilot_task_caddy_a1b2c3",
    ],
)
def test_caddy_harness_accepts_only_its_unique_local_database(url: str) -> None:
    """Catches the release harness reusing a shared or another task's database."""
    with pytest.raises(RuntimeError, match="fitpilot_task_caddy"):
        require_caddy_database({"TEST_DATABASE_URL": url})


def test_caddy_harness_accepts_its_exact_disposable_prefix() -> None:
    require_caddy_database(
        {"TEST_DATABASE_URL": "postgresql+asyncpg://localhost/fitpilot_task_caddy_a1b2c3"}
    )


def test_harness_contract_is_pinned_and_uses_exact_production_caddyfile() -> None:
    """Catches a harness that silently tests a generated route or floating Caddy tag."""
    assert CADDY_IMAGE == "caddy:2.11.4"
    assert CaddyHarness.production_caddyfile().resolve() == (
        Path(__file__).resolve().parents[2] / "deploy/caddy/Caddyfile"
    ).resolve()
    assert FIXTURE_BYTES == b"EURITH-CADDY-RANGE-FIXTURE"


def test_subprocess_diagnostics_are_bounded_and_redacted() -> None:
    """Catches credentials, internal paths, or unlimited tool output entering failures."""
    secret = (
        "Authorization: Bearer abc\n"
        "X-Hub-Signature-256: sha256=deadbeef\n"
        "X-Accel-Redirect: /_release_files/android/sha256/abc.apk\n"
        "postgresql+asyncpg://admin:password@localhost/fitpilot_task_caddy_a\n"
        "ENV=value\n"
        '{"Authorization":["Bearer json-secret"],"X-Accel-Redirect":"/_release_files/private.apk"}\n'
        "unstructured /_release_files/private-two.apk\n"
    )
    result = sanitize_output(secret + ("x" * (MAX_CAPTURE_BYTES * 2)))
    assert len(result.encode("utf-8")) <= MAX_CAPTURE_BYTES
    for forbidden in (
        "Bearer abc",
        "deadbeef",
        "abc.apk",
        "password",
        "ENV=value",
        "json-secret",
        "private.apk",
        "private-two.apk",
    ):
        assert forbidden not in result


def test_subprocess_capture_itself_is_bounded(tmp_path: Path) -> None:
    """Catches a noisy child exhausting memory before diagnostics are sanitized."""
    completed = _run(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('o'*200000); sys.stderr.write('e'*200000)",
        ],
        cwd=tmp_path,
    )
    assert len(completed.stdout) <= MAX_CAPTURE_BYTES
    assert len(completed.stderr) <= MAX_CAPTURE_BYTES


@pytest.fixture(scope="module")
def caddy() -> CaddyHarness:
    with CaddyHarness.start_or_skip() as running:
        yield running


def test_full_head_and_range_are_served_by_caddy(caddy: CaddyHarness) -> None:
    full = caddy.request("GET", caddy.download_path)
    assert full.status == 200
    assert full.body == FIXTURE_BYTES
    assert hashlib.sha256(full.body).hexdigest() == caddy.sha256
    assert full.headers["content-length"] == str(len(FIXTURE_BYTES))
    assert full.headers["etag"] == caddy.etag
    assert full.headers["digest"] == caddy.digest
    assert "x-accel-redirect" not in full.headers

    head = caddy.request("HEAD", caddy.download_path)
    assert head.status == 200
    assert head.body == b""
    assert head.headers["content-length"] == str(len(FIXTURE_BYTES))

    partial = caddy.request("GET", caddy.download_path, {"Range": "bytes=2-5"})
    assert partial.status == 206
    assert partial.body == FIXTURE_BYTES[2:6]
    assert partial.headers["content-range"] == f"bytes 2-5/{len(FIXTURE_BYTES)}"
    assert partial.headers["content-length"] == "4"
    assert partial.headers["etag"] == caddy.etag
    assert partial.headers["digest"] == caddy.digest


@pytest.mark.parametrize(
    "path",
    ["/_release_files/android/sha256/file.apk", "/%5frelease_files/android/sha256/file.apk"],
)
def test_direct_internal_paths_never_reach_upstream(caddy: CaddyHarness, path: str) -> None:
    before = caddy.counter("all")
    response = caddy.request("GET", path)
    assert response.status == 404
    assert "x-accel-redirect" not in response.headers
    assert caddy.counter("all") == before


@pytest.mark.parametrize(
    "path", ["/_release_files/%2e%2e/secret", "/_release_files/../secret"]
)
def test_traversal_paths_cannot_select_or_leak_a_file(caddy: CaddyHarness, path: str) -> None:
    response = caddy.request("GET", path)
    assert response.status == 404
    assert response.body != caddy.outside_bytes
    assert "x-accel-redirect" not in response.headers


@pytest.mark.parametrize("variant", ["malformed", "non200", "injected"])
def test_invalid_handoffs_fail_closed_without_header_or_body(
    caddy: CaddyHarness, variant: str
) -> None:
    response = caddy.request("GET", f"/__caddy_test/handoff/{variant}")
    assert response.status == 502
    assert response.body == b""
    assert "x-accel-redirect" not in response.headers
    assert "x-injected" not in response.headers


def test_normal_upstream_error_is_preserved_but_internal_header_is_stripped(
    caddy: CaddyHarness,
) -> None:
    response = caddy.request("GET", "/__caddy_test/handoff/absent")
    assert response.status == 418
    assert response.body == b"ordinary-error"
    assert "x-accel-redirect" not in response.headers


def test_upstream_false_length_is_replaced_by_file_length(caddy: CaddyHarness) -> None:
    response = caddy.request("GET", "/__caddy_test/handoff/false-length")
    assert response.status == 200
    assert response.body == FIXTURE_BYTES
    assert response.headers["content-length"] == str(len(FIXTURE_BYTES))


@pytest.mark.parametrize("variant", ["missing", "wrong-size", "symlink"])
def test_unavailable_or_non_regular_artifacts_are_never_served(
    caddy: CaddyHarness, variant: str
) -> None:
    response = caddy.request("GET", caddy.download_variant_path(variant))
    assert response.status in {404, 503}
    assert response.body != caddy.outside_bytes
    assert "x-accel-redirect" not in response.headers


def test_caddy_cannot_follow_a_raw_handoff_to_external_symlink(caddy: CaddyHarness) -> None:
    response = caddy.request("GET", "/__caddy_test/handoff/symlink-raw")
    assert response.status in {404, 502}
    assert response.body != caddy.outside_bytes
    assert "x-accel-redirect" not in response.headers


def test_exact_upload_limit_does_not_expand_other_routes(caddy: CaddyHarness) -> None:
    before_publish = caddy.counter("publish")
    rejected = caddy.request_large_publish(256 * 1024 * 1024 + 1)
    assert rejected.status == 413
    assert caddy.counter("publish") == before_publish

    before_other = caddy.counter("other")
    accepted = caddy.request("POST", "/__caddy_test/unrelated", body=b"ok")
    assert accepted.status == 204
    assert caddy.counter("other") == before_other + 1


def test_final_view_is_read_only_nosymfollow_and_excludes_staging(caddy: CaddyHarness) -> None:
    assert caddy.host_mount_flags >= {"ro", "nosymfollow"}
    assert caddy.container_mount_flags >= {"ro", "nosymfollow"}
    assert caddy.host_probe_mount_flags >= {"ro", "nosymfollow"}
    assert caddy.container_probe_mount_flags >= {"ro", "nosymfollow"}
    assert caddy.host_regular_read_succeeded
    assert caddy.host_symlink_read_denied
    assert caddy.host_probe_regular_read_succeeded
    assert caddy.host_probe_symlink_read_denied
    assert caddy.container_regular_read_succeeded
    assert caddy.container_symlink_read_denied
    assert caddy.container_probe_regular_read_succeeded
    assert caddy.container_probe_symlink_read_denied
    assert ".staging" not in caddy.container_mount_sources
    assert caddy.caddy_mutations_denied == {"write", "rename", "chmod", "delete"}
