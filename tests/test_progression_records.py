from datetime import datetime, timezone

from api.services.progression.records import (
    MAX_TRACKED_REPS,
    SetInput,
    band_for,
    brzycki_e1rm,
    fold_records,
)


def _at(day: int) -> datetime:
    return datetime(2026, 8, day, 12, 0, tzinfo=timezone.utc)


def test_band_boundaries():
    assert band_for(1) == "1-3"
    assert band_for(3) == "1-3"
    assert band_for(4) == "4-6"
    assert band_for(10) == "7-10"
    assert band_for(15) == "11-15"
    assert band_for(16) == "16+"
    assert band_for(0) is None


def test_brzycki_undefined_at_37_reps():
    assert brzycki_e1rm(100.0, 36) is not None
    assert brzycki_e1rm(100.0, 37) is None
    assert brzycki_e1rm(100.0, 40) is None


def test_weight_at_reps_keeps_best_per_exact_reps():
    records = fold_records([
        SetInput(weight=80.0, reps=5, at=_at(1), workout_id=1),
        SetInput(weight=82.5, reps=5, at=_at(2), workout_id=2),
        SetInput(weight=100.0, reps=8, at=_at(3), workout_id=3),
    ])
    assert records["weight_at_reps"]["5"]["weight"] == 82.5
    assert records["weight_at_reps"]["5"]["workout_id"] == 2
    # Ключи независимы: 100x8 не трогает запись для 5 повторов.
    assert records["weight_at_reps"]["8"]["weight"] == 100.0


def test_weight_at_reps_ignores_reps_above_limit():
    records = fold_records([
        SetInput(weight=40.0, reps=MAX_TRACKED_REPS + 1, at=_at(1), workout_id=1),
    ])
    assert records["weight_at_reps"] == {}
    # Полоса и объём высокие повторы всё равно ловят.
    assert records["band"]["11-15"]["reps"] == 13
    assert records["set_volume"]["value"] == 520.0


def test_band_keeps_best_e1rm_not_best_weight():
    records = fold_records([
        SetInput(weight=100.0, reps=4, at=_at(1), workout_id=1),   # e1rm 109.1
        SetInput(weight=95.0, reps=6, at=_at(2), workout_id=2),    # e1rm 110.3
    ])
    assert records["band"]["4-6"]["weight"] == 95.0
    assert records["band"]["4-6"]["reps"] == 6


def test_set_volume_is_single_best_set():
    records = fold_records([
        SetInput(weight=50.0, reps=10, at=_at(1), workout_id=1),
        SetInput(weight=60.0, reps=10, at=_at(2), workout_id=2),
    ])
    assert records["set_volume"]["value"] == 600.0
    assert records["set_volume"]["workout_id"] == 2


def test_ties_keep_the_earliest_set():
    records = fold_records([
        SetInput(weight=80.0, reps=5, at=_at(1), workout_id=1),
        SetInput(weight=80.0, reps=5, at=_at(2), workout_id=2),
    ])
    assert records["weight_at_reps"]["5"]["workout_id"] == 1


def test_empty_input_gives_empty_sections():
    assert fold_records([]) == {"weight_at_reps": {}, "band": {}, "set_volume": None}
