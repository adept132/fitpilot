"""Compose API image selection from the complete Compose configuration."""

import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "resolve-compose-api-image.py"


def test_generated_api_image_ignores_other_services() -> None:
    config = {
        "name": "eurith",
        "services": {
            "api": {"build": {"context": "/opt/eurith/backend"}},
            "postgres": {"image": "postgres:17-bookworm"},
            "caddy": {"image": "caddy:2.11.4"},
        },
    }
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], input=json.dumps(config), text=True,
        capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "eurith-api\n"


def test_explicit_api_image_wins() -> None:
    config = {"name": "eurith", "services": {"api": {"image": "registry.example/eurith-api:tested"}}}
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], input=json.dumps(config), text=True,
        capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "registry.example/eurith-api:tested\n"


def test_expected_build_root_accepts_only_exact_source_and_dockerfile() -> None:
    config = {"name": "eurith", "services": {"api": {"build": {
        "context": "/opt/eurith/deploy-run/abc", "dockerfile": "Dockerfile",
    }}}}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--expected-build-root", "/opt/eurith/deploy-run/abc"],
        input=json.dumps(config), text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "eurith-api\n"


def test_expected_build_root_rejects_extra_build_options() -> None:
    config = {"name": "eurith", "services": {"api": {"build": {
        "context": "/opt/eurith/deploy-run/abc", "dockerfile": "Dockerfile",
        "args": {"APP_MODE": "other"},
    }}}}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--expected-build-root", "/opt/eurith/deploy-run/abc"],
        input=json.dumps(config), text=True, capture_output=True, check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "error=compose_api_build_source_invalid" in result.stderr


def test_expected_build_root_rejects_always_pull_policy() -> None:
    config = {"name": "eurith", "services": {"api": {
        "build": {"context": "/opt/eurith/deploy-run/abc", "dockerfile": "Dockerfile"},
        "image": "registry.example/eurith-api:latest",
        "pull_policy": "always",
    }}}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--expected-build-root", "/opt/eurith/deploy-run/abc"],
        input=json.dumps(config), text=True, capture_output=True, check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "error=compose_api_pull_policy_invalid" in result.stderr


def test_expected_build_root_rejects_automatic_rebuild_policy() -> None:
    config = {"name": "eurith", "services": {"api": {
        "build": {"context": "/opt/eurith/deploy-run/abc", "dockerfile": "Dockerfile"},
        "pull_policy": "build",
    }}}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--expected-build-root", "/opt/eurith/deploy-run/abc"],
        input=json.dumps(config), text=True, capture_output=True, check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "error=compose_api_pull_policy_invalid" in result.stderr
