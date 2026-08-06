"""Инварианты таблиц landmarks. Таблица объявлена калибруемой, поэтому
проверки должны падать при любой правке, которая делает её невменяемой.
"""
import pytest

from api.services.volume.landmarks import (
    LEVELS,
    MUSCLES,
    SESSION_MAX,
    SYSTEMIC_CAP_EFF,
    landmarks_for,
    reachable_mrv,
)


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("muscle", MUSCLES)
def test_mev_below_mav_below_mrv(muscle, level):
    lm = landmarks_for(muscle, level)
    assert lm is not None
    assert lm.mev < lm.mav < lm.mrv


@pytest.mark.parametrize("muscle", MUSCLES)
def test_monotonic_across_levels(muscle):
    b = landmarks_for(muscle, "beginner")
    i = landmarks_for(muscle, "intermediate")
    a = landmarks_for(muscle, "advanced")
    assert b.mev <= i.mev <= a.mev
    assert b.mav <= i.mav <= a.mav
    assert b.mrv <= i.mrv <= a.mrv


@pytest.mark.parametrize("level", LEVELS)
def test_sum_of_mev_fits_under_systemic_cap(level):
    # Иначе приложение требовало бы заведомо невыполнимой недели.
    total = sum(landmarks_for(m, level).mev for m in MUSCLES)
    assert total <= SYSTEMIC_CAP_EFF[level]


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("muscle", MUSCLES)
def test_mrv_direct_reachable_at_typical_frequency(muscle, level):
    # Недостижимый потолок — мёртвое предупреждение.
    lm = landmarks_for(muscle, level)
    assert lm.mrv_direct <= SESSION_MAX[level] * 2


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("muscle", MUSCLES)
def test_direct_bounds_lie_inside_effective_bounds(muscle, level):
    lm = landmarks_for(muscle, level)
    assert lm.mev_direct <= lm.mev
    assert lm.mrv_direct <= lm.mrv


def test_sum_of_mav_exceeds_cap():
    # Определяющее свойство MAV: вывести туда все мышцы сразу нельзя.
    # Если сумма влезает под потолок, значит таблица описывает не MAV, а
    # очередное распределение бюджета.
    total = sum(landmarks_for(m, "intermediate").mav for m in MUSCLES)
    assert total > SYSTEMIC_CAP_EFF["intermediate"]


def test_unknown_muscle_returns_none():
    assert landmarks_for("хвост", "intermediate") is None


def test_unknown_level_falls_back_to_beginner():
    assert landmarks_for("chest", "wizard") == landmarks_for("chest", "beginner")


def test_reachable_mrv_clamps_to_split_frequency():
    # Грудь раз в неделю: табличный потолок недосягаем, показываем достижимый.
    table = landmarks_for("chest", "intermediate").mrv_direct
    once = reachable_mrv("chest", "intermediate", frequency=1)
    assert once == SESSION_MAX["intermediate"]
    assert once < table


def test_reachable_mrv_returns_table_value_when_frequency_is_enough():
    assert reachable_mrv("chest", "intermediate", frequency=4) == (
        landmarks_for("chest", "intermediate").mrv_direct
    )
