from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from tests.deploy.test_release_deploy_contract import BASH, DEPLOY, ROOT, _function, _shell, _text, _wrapper


ASSETS = ("deploy/compose.release.yml", "deploy/caddy/Caddyfile", "deploy/caddy/caddy-entrypoint.sh")


@pytest.mark.parametrize("case", ["identical", "changed", "missing", "read_failure", "malformed", "aggregate_failure", "aggregate_malformed"])
def test_checkout_compares_checked_relative_asset_manifests(tmp_path: Path, case: str) -> None:
    # Absolute filenames in hash input must not reject identical detached checkouts.
    source, assets, bin_dir = (tmp_path / name for name in ("source", "assets", "bin"))
    bin_dir.mkdir()
    for root in (source, assets):
        for relative in ASSETS:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(relative + "\n", encoding="ascii")
    if case == "changed":
        (source / ASSETS[1]).write_text("different\n", encoding="ascii")
    if case == "missing":
        (assets / ASSETS[1]).unlink()
    _wrapper(bin_dir, "git", 'case "$*" in *"rev-parse HEAD") printf "%s\\n" "$TARGET_SHA" ;; *) exit 0 ;; esac')
    _wrapper(bin_dir, "docker", 'printf "compose_validated\\n"')
    _wrapper(bin_dir, "sha256sum", '''
case "$MANIFEST_CASE:$#" in
  read_failure:2) exit 23 ;;
  malformed:2) printf 'INVALID  file\\n'; exit 0 ;;
  aggregate_failure:0) exit 24 ;;
  aggregate_malformed:0) printf 'INVALID  -\\n'; exit 0 ;;
esac
exec /usr/bin/sha256sum "$@"
''')
    runner = tmp_path / "runner.sh"
    runner.write_text(
        'set -Eeuo pipefail\n'
        + f'source "{_shell(ROOT / "deploy/lib/release_common.sh")}"\n'
        + _function(_text(DEPLOY), "checkout_target_source", "validate_caddy")
        + '\nevidence() { printf "%s=%s\\n" "$1" "$2"; }\n'
        + f'SOURCE_DIR="{_shell(source)}"\nEURITH_DEPLOY_ASSET_ROOT="{_shell(assets)}"\n'
        + 'TARGET_SHA=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\nexport TARGET_SHA\ncompose=(docker compose)\ncheckout_target_source\n',
        encoding="utf-8", newline="\n",
    )
    env = os.environ.copy()
    env.update(PATH=_shell(bin_dir) + ":/usr/bin:/bin", MANIFEST_CASE=case)
    result = subprocess.run([BASH, _shell(runner)], env=env, text=True, capture_output=True, timeout=30)
    if case == "identical":
        assert result.returncode == 0, result.stderr
        hashes = dict(line.split("=", 1) for line in result.stdout.splitlines())
        assert hashes["deploy_asset_sha256"] == hashes["runtime_asset_sha256"]
        assert len(hashes["runtime_asset_sha256"]) == 64
    else:
        assert result.returncode != 0
        assert "runtime_asset_sha256=" not in result.stdout
