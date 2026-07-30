"""Golden-фикстуры вердикта.

Эти же файлы дословно копируются в мобильный репозиторий
(src/readiness/__tests__/fixtures/) и прогоняются там через jest.
Манифест tests/readiness_fixtures.sha256 — единственный механический
сигнал о расхождении двух реализаций (спека P0-07 §9.3).
"""

import hashlib
import json
from pathlib import Path

import pytest

from api.services.readiness.types import CheckinSignals, ExerciseTarget
from api.services.readiness.verdict import build_verdict, level_for_exercise

FIXTURE_DIR = Path(__file__).parent / "readiness_fixtures"
MANIFEST = Path(__file__).parent / "readiness_fixtures.sha256"


def _fixture_files() -> list[Path]:
    return sorted(FIXTURE_DIR.glob("*.json"))


def _digest(path: Path) -> str:
    # Нормализуем перевод строки: репозитории на Windows, core.autocrlf
    # способен подменить LF на CRLF и развалить сверку на ровном месте.
    raw = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("path", _fixture_files(), ids=lambda p: p.stem)
def test_fixture_matches_engine(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))

    signals = CheckinSignals(
        sleep=data["signals"].get("sleep"),
        stress=data["signals"].get("stress"),
        soreness=data["signals"].get("soreness", {}),
        pain=data["signals"].get("pain", {}),
    )
    verdict = build_verdict(signals)
    assert verdict is not None, data["name"]

    expected = data["expect_verdict"]
    assert verdict.level == expected["level"], data["name"]
    assert verdict.reason_code == expected["reason_code"], data["name"]
    assert verdict.completeness == expected["completeness"], data["name"]

    for case in data["targets"]:
        raw = case["target"]
        target = ExerciseTarget(
            exercise_id=raw["exercise_id"],
            main_muscle=raw.get("main_muscle"),
            secondary_muscles=tuple(raw.get("secondary_muscles", [])),
            action=raw.get("action", "unknown"),
        )
        result = level_for_exercise(verdict, target)
        assert result.level == case["expect"]["level"], (data["name"], raw)
        assert result.source == case["expect"]["source"], (data["name"], raw)


def test_manifest_covers_every_fixture():
    listed = {
        line.split("  ", 1)[1]
        for line in MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    assert listed == {p.name for p in _fixture_files()}


def test_manifest_hashes_are_current():
    expected = {
        line.split("  ", 1)[1]: line.split("  ", 1)[0]
        for line in MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    for path in _fixture_files():
        assert _digest(path) == expected[path.name], (
            f"Фикстура {path.name} изменена без обновления манифеста. "
            "Пересчитайте манифест И перенесите копию в мобильный репозиторий."
        )
