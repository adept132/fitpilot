"""Операции над снимком фаз: вставка разгрузки, перенос, разрез на границе."""
import pytest

from api.services.periodization.phases import (
    from_json,
    insert_deload,
    postpone_deload,
    split_after,
    tier_for,
    to_json,
)
from api.services.periodization.types import PhaseSnapshot
from api.services.validator import AntiSuicideValidator

BASE = (
    PhaseSnapshot(1, "Лёгкая", "easy", 7),
    PhaseSnapshot(2, "Средняя", "medium", 7),
    PhaseSnapshot(3, "Тяжёлая", "prefailure", 7),
    PhaseSnapshot(4, "Разгрузка", "deload", 7),
)


def test_insert_deload_puts_it_right_after_the_named_phase():
    result = insert_deload(BASE, after_phase_number=2, length_days=7)
    assert [p.effort_tier for p in result] == [
        "easy", "medium", "deload", "prefailure", "deload",
    ]


def test_insert_deload_never_renumbers_existing_phases():
    """Инвариант: WorkoutSession.mesocycle_phase завершённых сессий обязан
    указывать на ту же фазу и после вставки."""
    result = insert_deload(BASE, after_phase_number=2, length_days=7)
    for original in BASE:
        match = [p for p in result if p.phase_number == original.phase_number]
        assert len(match) == 1
        assert match[0].effort_tier == original.effort_tier


def test_inserted_phase_gets_a_fresh_number():
    result = insert_deload(BASE, after_phase_number=2, length_days=7)
    assert result[2].phase_number == 5


def test_insert_deload_keeps_total_length_consistent():
    result = insert_deload(BASE, after_phase_number=2, length_days=7)
    assert sum(p.length_days for p in result) == sum(p.length_days for p in BASE) + 7


def test_insert_deload_result_passes_anti_suicide_validator():
    result = insert_deload(BASE, after_phase_number=2, length_days=7)
    AntiSuicideValidator.validate_mesocycle_sequence([p.effort_tier for p in result])


def test_insert_deload_rejects_unknown_phase():
    with pytest.raises(ValueError):
        insert_deload(BASE, after_phase_number=99, length_days=7)


def test_postpone_deload_extends_the_phase_before_it():
    result = postpone_deload(BASE, extra_days=7)
    assert result[2].length_days == 14
    assert [p.phase_number for p in result] == [1, 2, 3, 4]


def test_postpone_deload_without_deload_is_a_noop():
    no_deload = BASE[:3]
    assert postpone_deload(no_deload, extra_days=7) == no_deload


def test_split_after_returns_head_and_tail():
    head, tail = split_after(BASE, phase_number=2)
    assert [p.phase_number for p in head] == [1, 2]
    assert [p.phase_number for p in tail] == [3, 4]


def test_tier_for_finds_by_stable_number():
    assert tier_for(BASE, 4) == "deload"
    assert tier_for(BASE, 99) is None


def test_json_roundtrip_preserves_order_and_numbers():
    result = insert_deload(BASE, after_phase_number=2, length_days=7)
    assert from_json(to_json(result)) == result
