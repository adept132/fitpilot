from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "deploy.sh"
BASH = shutil.which("bash") or "D:/Git/usr/bin/bash.exe"


def _shell(path: Path) -> str:
    value = path.resolve().as_posix()
    return f"/{value[0].lower()}{value[2:]}" if len(value) > 2 and value[1] == ":" else value


def test_release_caddy_gate_uses_validated_digest_in_integration(tmp_path: Path) -> None:
    """The integration gate must test the image validated by deploy.sh."""
    script = DEPLOY.read_text(encoding="utf-8")
    start = script.index("run_caddy_integration() {")
    end = script.index("\ngate_migrations() {", start)
    gate = script[start:end]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python = bin_dir / "python3"
    python.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" != -m ]]; then exit 0; fi\n"
        "[[ \"${CADDY_INTEGRATION_REQUIRED:-}\" == 1 ]] || exit 41\n"
        "[[ \"${CADDY_TEST_IMAGE:-}\" == \"$CADDY_IMAGE_REF\" ]] || exit 42\n",
        encoding="ascii",
        newline="\n",
    )
    python.chmod(0o755)
    image = "caddy:2.11.4@sha256:" + "a" * 64
    harness = (
        "set -Eeuo pipefail\n"
        "die() { printf 'error=%s\\n' \"$1\" >&2; exit 1; }\n"
        "evidence() { printf '%s=%s\\n' \"$1\" \"$2\"; }\n"
        + gate
        + f"\nCADDY_DATABASE_GUARD=guard\nexport CADDY_IMAGE_REF={image}\n"
        + f"EURITH_DEPLOY_ASSET_ROOT='{_shell(tmp_path)}'\n"
        + "run_caddy_integration\n"
    )
    env = os.environ.copy()
    env.pop("CADDY_TEST_IMAGE", None)
    env["PATH"] = _shell(bin_dir) + ":/usr/bin:/bin"
    result = subprocess.run([BASH, "-c", harness], env=env, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert "caddy_integration=passed" in result.stdout
