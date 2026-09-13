from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path(os.environ["RELEASE_ROOT"]) if "RELEASE_ROOT" in os.environ else Path(__file__).resolve().parents[2]
DEPLOY = Path(os.environ["RELEASE_DEPLOY"]) if "RELEASE_DEPLOY" in os.environ else ROOT / "deploy" / "deploy.sh"
COMMON = ROOT / "deploy" / "lib" / "release_common.sh"
BASH = shutil.which("bash") or "D:/Git/usr/bin/bash.exe"


def _shell(path: Path) -> str:
    value = path.resolve().as_posix()
    return f"/{value[0].lower()}{value[2:]}" if len(value) > 2 and value[1] == ":" else value


def test_evidence_writer_accepts_sha256_field_used_by_backup(tmp_path: Path) -> None:
    """A completed backup must be able to record its manifest hash."""
    script = DEPLOY.read_text(encoding="utf-8")
    start = script.index("evidence() {")
    end = script.index("\ngate_checkout() {", start)
    evidence_function = script[start:end]
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    evidence_file = evidence_dir / "deploy.env"
    evidence_file.write_text("", encoding="ascii")
    expected_hash = "a" * 64
    harness = (
        "set -Eeuo pipefail\n"
        + f"source '{_shell(COMMON)}'\n"
        + f"EVIDENCE_DIR='{_shell(evidence_dir)}'\n"
        + evidence_function
        + f"\nevidence backup_manifest_sha256 {expected_hash}\n"
    )
    result = subprocess.run(
        [BASH, "-c", harness],
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert evidence_file.read_text(encoding="ascii") == f"backup_manifest_sha256={expected_hash}\n"
