from __future__ import annotations

from pathlib import Path
import re

import yaml


ROOT = Path(__file__).resolve().parents[2]
CADDYFILE = ROOT / "deploy" / "caddy" / "Caddyfile"
COMPOSE_FILE = ROOT / "deploy" / "compose.release.yml"
SHARED_GID = "${RELEASE_SHARED_GID:?set in /etc/eurith/release-deploy.env}"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing versioned deployment contract: {path.relative_to(ROOT)}"
    return path.read_text(encoding="utf-8")


def _block(text: str, marker: str) -> str:
    start = text.index(marker)
    opening = start + marker.rindex("{")
    depth = 0
    for index in range(opening, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"unterminated block: {marker}")


def _named_block(text: str, name: str) -> str:
    return _block(text, f"@{name} {{")


def _service_text(compose_text: str, name: str) -> str:
    data = yaml.safe_load(compose_text)
    return yaml.safe_dump(data["services"][name], sort_keys=True)


def test_versioned_caddy_and_compose_contracts_exist() -> None:
    _read(CADDYFILE)
    _read(COMPOSE_FILE)


def test_compose_keeps_api_private_and_mounts_only_required_release_paths() -> None:
    compose_text = _read(COMPOSE_FILE)
    compose = yaml.safe_load(compose_text)
    assert set(compose) == {
        "services",
        "volumes",
        "x-eurith-release-view-contract",
    }
    assert set(compose["services"]) == {"api", "caddy"}
    assert compose["volumes"] == {"caddy_data": {}, "caddy_config": {}}

    api = compose["services"]["api"]
    assert "ports" not in api
    assert api["expose"] == ["8000"]
    assert api["env_file"] == ["/etc/eurith/api-release.env"]
    assert api["group_add"] == [SHARED_GID]
    assert api["volumes"] == [
        "/opt/eurith/releases:/var/lib/eurith/releases:rw",
    ]

    caddy = compose["services"]["caddy"]
    assert caddy == {
        "image": "${CADDY_IMAGE_REF:-caddy:2.11.4}",
        "restart": "unless-stopped",
        "depends_on": {"api": {"condition": "service_started"}},
        "entrypoint": ["/bin/sh", "/usr/local/bin/caddy-entrypoint.sh"],
        "command": [],
        "read_only": True,
        "group_add": [SHARED_GID],
        "volumes": [
            {
                "type": "bind",
                "source": "./backend/deploy/caddy/Caddyfile",
                "target": "/etc/caddy/Caddyfile",
                "read_only": True,
                "bind": {"create_host_path": False},
            },
            {
                "type": "bind",
                "source": "./backend/deploy/caddy/caddy-entrypoint.sh",
                "target": "/usr/local/bin/caddy-entrypoint.sh",
                "read_only": True,
                "bind": {"create_host_path": False},
            },
            {
                "type": "bind",
                "source": "/opt/eurith/release-caddy-view/android/sha256",
                "target": "/srv/eurith/releases/android/sha256",
                "read_only": True,
                "bind": {"create_host_path": False},
            },
            {
                "type": "bind",
                "source": "/opt/eurith/release-caddy-view/.probe",
                "target": "/run/eurith-release-view-probe",
                "read_only": True,
                "bind": {"create_host_path": False},
            },
            "caddy_data:/data",
            "caddy_config:/config",
        ],
        "ports": ["80:80", "443:443", "443:443/udp"],
    }


def test_compose_requires_a_nosymfollow_host_view_before_caddy_starts() -> None:
    compose = yaml.safe_load(_read(COMPOSE_FILE))
    assert compose["x-eurith-release-view-contract"] == {
        "source": "/opt/eurith/releases/android/sha256",
        "view": "/opt/eurith/release-caddy-view/android/sha256",
        "probe": "/opt/eurith/release-caddy-view/.probe",
        "required_vfs_options": ["ro", "nosymfollow"],
        "blocking_probes": [
            "host-regular-file-readable",
            "host-external-symlink-denied",
            "container-vfs-ro-nosymfollow",
            "container-external-symlink-denied",
        ],
    }
    caddy_volumes = compose["services"]["caddy"]["volumes"]
    bind_sources = {
        mount["source"] for mount in caddy_volumes if isinstance(mount, dict)
    }
    assert "/opt/eurith/release-caddy-view/android/sha256" in bind_sources
    assert "/opt/eurith/release-caddy-view/.probe" in bind_sources
    assert "/opt/eurith/releases" not in bind_sources
    assert "/opt/eurith/releases/android/sha256" not in bind_sources


def test_caddy_entrypoint_gate_is_mandatory_and_fail_closed() -> None:
    compose = yaml.safe_load(_read(COMPOSE_FILE))
    caddy = compose["services"]["caddy"]
    assert caddy["entrypoint"] == ["/bin/sh", "/usr/local/bin/caddy-entrypoint.sh"]
    assert caddy["command"] == []
    assert caddy["read_only"] is True
    gate_script = _read(ROOT / "deploy" / "caddy" / "caddy-entrypoint.sh")
    for required in (
        "/proc/self/mountinfo",
        "nosymfollow",
        "probe_mount=/run/eurith-release-view-probe",
        'regular="$probe_mount/regular"',
        'external="$probe_mount/external"',
        "readlink",
    ):
        assert required in gate_script
    assert '$5 == target { print $6; found = 1; exit }' in gate_script
    assert gate_script.count('require_protected_mount "$') == 2
    assert '*,ro,*) ;;' in gate_script
    assert '*,nosymfollow,*) ;;' in gate_script
    assert '"$head_command" -c 1 "$regular" >/dev/null 2>&1 || fail' in gate_script
    assert '"$("$readlink_command" "$external" 2>/dev/null)" = "/etc/passwd"' in gate_script
    assert 'if "$head_command" -c 1 "$external" >/dev/null 2>&1; then' in gate_script
    assert "|| true" not in gate_script
    assert "release_view_gate=passed" in gate_script
    assert 'exec "$caddy_command" run --config /etc/caddy/Caddyfile --adapter caddyfile' in gate_script
    assert "/opt/eurith/releases" not in compose["x-eurith-release-view-contract"]["probe"]


def test_caddy_receives_no_secret_or_staging_configuration() -> None:
    compose_text = _read(COMPOSE_FILE)
    caddy_text = _service_text(compose_text, "caddy").lower()
    for forbidden in (
        "publisher",
        "operator",
        "webhook",
        "database_url",
        ".staging",
        "nginx",
        "api-release.env",
    ):
        assert forbidden not in caddy_text


def test_caddyfile_uses_only_approved_runtime_inputs_and_contains_no_secrets() -> None:
    text = _read(CADDYFILE)
    assert set(re.findall(r"\{\$([A-Z_]+)(?::[^}]*)?\}", text)) == {
        "EURITH_SITE_ADDRESS",
        "EURITH_UPSTREAM",
        "RELEASE_FILE_ROOT",
    }
    lowered = text.lower()
    for forbidden in (
        "publisher",
        "operator",
        "webhook_secret",
        "database_url",
        ".staging",
        "nginx",
    ):
        assert forbidden not in lowered


def test_caddy_routes_are_ordered_and_publish_limit_is_exactly_scoped() -> None:
    text = _read(CADDYFILE)
    assert text.startswith("{$EURITH_SITE_ADDRESS:https://api.eurith.app} {")
    deny = "@internal_release_files path /_release_files/*"
    publish = _named_block(text, "direct_apk_publish")
    ordinary_proxy = "# Ordinary API proxy and the only response handoff boundary."
    assert deny in text
    assert "respond @internal_release_files 404" in text
    assert text.count("\n\troute {") == 3
    assert text.index(deny) < text.index("@direct_apk_publish {") < text.index(ordinary_proxy)
    assert "method POST" in publish
    assert "path /internal/app-releases/android/direct-apk" in publish
    assert "request_body @direct_apk_publish {\n\t\t\tmax_size 256MiB\n\t\t}" in text
    assert "reverse_proxy @direct_apk_publish {$EURITH_UPSTREAM:api:8000}" in text
    publish_proxy = _block(
        text, "reverse_proxy @direct_apk_publish {$EURITH_UPSTREAM:api:8000} {"
    )
    assert "header_down -X-Accel-Redirect" in publish_proxy
    for broad_matcher in ("/internal/*", "/internal/app-releases/*", "request_body {\n"):
        assert broad_matcher not in text


def test_handoff_requires_status_and_exact_prefix_and_fails_malformed_closed() -> None:
    text = _read(CADDYFILE)
    handoff = _named_block(text, "release_handoff")
    malformed = _named_block(text, "malformed_handoff")
    assert "status 200" in handoff
    assert "header X-Accel-Redirect /_release_files/*" in handoff
    assert "header X-Accel-Redirect *" in malformed
    assert text.index("handle_response @release_handoff") < text.index(
        "handle_response @malformed_handoff"
    )
    assert "respond \"\" 502" in text
    assert "header_down -X-Accel-Redirect" in text


def test_handoff_serves_from_read_only_root_and_copies_only_approved_metadata() -> None:
    text = _read(CADDYFILE)
    handler_start = text.index("handle_response @release_handoff")
    handler_end = text.index("handle_response @malformed_handoff")
    handler = text[handler_start:handler_end]
    assert "rewrite * {rp.header.X-Accel-Redirect}" in handler
    assert "uri strip_prefix /_release_files" in handler
    assert "root * {$RELEASE_FILE_ROOT:/srv/eurith/releases}" in handler
    assert "file_server" in handler
    copied_headers = _block(handler, "copy_response_headers {")
    assert copied_headers.splitlines()[1:-1] == [
        "\t\t\t\t\tinclude Content-Type",
        "\t\t\t\t\tinclude Content-Disposition",
        "\t\t\t\t\tinclude ETag",
        "\t\t\t\t\tinclude Digest",
    ]
    assert text.count("file_server") == 1
    assert "Content-Length" not in text


def test_access_log_is_json_and_explicitly_redacts_sensitive_headers() -> None:
    text = _read(CADDYFILE)
    log = _block(text, "log {")
    assert "output stdout" in log
    filtered = _block(log, "format filter {")
    assert "wrap json" in filtered
    fields = _block(filtered, "fields {")
    assert fields.splitlines()[1:-1] == [
        "\t\t\t\trequest>headers>Authorization delete",
        "\t\t\t\trequest>headers>X-Hub-Signature-256 delete",
        "\t\t\t\trequest>headers>X-Accel-Redirect delete",
    ]
    # The stock access logger does not record request bodies, upstream response
    # headers, or handler filesystem roots. Keep custom fields disabled so APK
    # notes/body/path data cannot be appended later without changing this test.
    assert "log_append" not in text
    assert "request>body" not in filtered
