"""Источник целевого диапазона повторов (спека P0-06 §7.1)."""

import pytest

from api.services.calculate_exercise_recommendation import _apply_override_reps
from api.services.progression import params
from api.services.resolvers import (
    DayTacticalType,
    resolve_rep_range,
    resolve_rep_range_with_source,
)


def test_microcycle_day_is_marked_as_microcycle():
    lo, hi, source = resolve_rep_range_with_source(1, DayTacticalType.hard)
    assert (lo, hi) == (4, 6)
    assert source == params.REP_SOURCE_MICROCYCLE


@pytest.mark.parametrize(
    "tier,expected",
    [(1, (6, 8)), (2, (8, 12)), (3, (12, 15))],
)
def test_no_microcycle_uses_explicit_tier_fallback(tier, expected):
    lo, hi, source = resolve_rep_range_with_source(tier, None)
    assert (lo, hi) == expected
    assert source == params.REP_SOURCE_FALLBACK


def test_unknown_tier_falls_back_to_tier_two():
    lo, hi, source = resolve_rep_range_with_source(99, None)
    assert (lo, hi) == params.TIER_REP_FALLBACK[2]
    assert source == params.REP_SOURCE_FALLBACK


def test_rest_day_returns_zero_range():
    lo, hi, _ = resolve_rep_range_with_source(2, DayTacticalType.rest)
    assert (lo, hi) == (0, 0)


def test_legacy_wrapper_keeps_two_tuple_shape():
    assert resolve_rep_range(2, DayTacticalType.medium) == (8, 10)


def test_tier_two_fallback_is_wider_than_the_old_implicit_medium():
    # Чем грубее шаг оборудования, тем шире нужен диапазон: 8->12 копит
    # +10.5 % e1RM против +6.7 % у 8->10.
    fallback_lo, fallback_hi, _ = resolve_rep_range_with_source(2, None)
    implicit_lo, implicit_hi = resolve_rep_range(2, DayTacticalType.medium)
    assert fallback_hi > implicit_hi
    assert fallback_lo == implicit_lo


# --- _apply_override_reps: плановый override поверх базового диапазона ---


def test_override_reps_range_marks_source_as_plan_override():
    lo, hi, source = _apply_override_reps(8, 10, params.REP_SOURCE_MICROCYCLE, "6-8")
    assert (lo, hi) == (6, 8)
    assert source == params.REP_SOURCE_PLAN


def test_override_reps_single_number_marks_source_as_plan_override():
    lo, hi, source = _apply_override_reps(8, 10, params.REP_SOURCE_FALLBACK, "10")
    assert (lo, hi) == (10, 10)
    assert source == params.REP_SOURCE_PLAN


@pytest.mark.parametrize("override_reps", [None, ""])
def test_no_override_reps_keeps_base_source(override_reps):
    lo, hi, source = _apply_override_reps(8, 10, params.REP_SOURCE_MICROCYCLE, override_reps)
    assert (lo, hi) == (8, 10)
    assert source == params.REP_SOURCE_MICROCYCLE


def test_unparsable_override_reps_keeps_base_source():
    lo, hi, source = _apply_override_reps(8, 10, params.REP_SOURCE_FALLBACK, "many")
    assert (lo, hi) == (8, 10)
    assert source == params.REP_SOURCE_FALLBACK


def test_malformed_override_reps_range_keeps_base_source():
    lo, hi, source = _apply_override_reps(8, 10, params.REP_SOURCE_MICROCYCLE, "6-8-10")
    assert (lo, hi) == (8, 10)
    assert source == params.REP_SOURCE_MICROCYCLE
