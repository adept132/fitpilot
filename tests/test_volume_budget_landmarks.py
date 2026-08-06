"""Бюджет объёма клампится в физиологический диапазон."""
import pytest

from api.services.volume.landmarks import SYSTEMIC_CAP_EFF, Landmarks, landmarks_for
from api.services.volume_calculator import calculate_volume_budget, clamp_target


def test_clamp_lifts_target_to_mev():
    lm = Landmarks(mev=6, mav=12, mrv=18, mev_direct=4, mrv_direct=14)
    assert clamp_target(2.0, lm, cycle_multiplier=1.0) == 6


def test_clamp_caps_target_at_mrv():
    lm = Landmarks(mev=6, mav=12, mrv=18, mev_direct=4, mrv_direct=14)
    assert clamp_target(40.0, lm, cycle_multiplier=1.0) == 18


def test_clamp_leaves_target_inside_range_untouched():
    lm = Landmarks(mev=6, mav=12, mrv=18, mev_direct=4, mrv_direct=14)
    assert clamp_target(11.4, lm, cycle_multiplier=1.0) == 11


def test_clamp_scales_bounds_by_cycle_multiplier():
    # Десятидневный микроцикл: границы растягиваются вместе с целью,
    # иначе кламп срезал бы законный объём длинного окна.
    lm = Landmarks(mev=6, mav=12, mrv=18, mev_direct=4, mrv_direct=14)
    assert clamp_target(40.0, lm, cycle_multiplier=10 / 7) == 25


def test_focus_muscle_targets_mav_not_arbitrary_multiplier():
    budget = calculate_volume_budget("intermediate", ["chest"])
    assert budget.weekly_targets["chest"].target_sets == (
        landmarks_for("chest", "intermediate").mav
    )
    assert budget.weekly_targets["chest"].is_focus is True


def test_min_floor_is_mev():
    budget = calculate_volume_budget("intermediate", [])
    for muscle, target in budget.weekly_targets.items():
        lm = landmarks_for(muscle, "intermediate")
        assert target.min_floor == lm.mev, muscle


def test_no_target_exceeds_mrv():
    budget = calculate_volume_budget("advanced", ["chest", "lats", "quads"])
    for muscle, target in budget.weekly_targets.items():
        assert target.target_sets <= landmarks_for(muscle, "advanced").mrv, muscle


def test_systemic_cap_comes_from_effective_table():
    budget = calculate_volume_budget("intermediate", [])
    assert budget.constraints.systemic_cap_per_week == SYSTEMIC_CAP_EFF["intermediate"]


def test_total_stays_under_effective_cap():
    budget = calculate_volume_budget("advanced", ["chest", "lats"])
    assert budget.meta.total_weekly_sets <= SYSTEMIC_CAP_EFF["advanced"]
