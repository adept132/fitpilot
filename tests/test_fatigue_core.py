"""Тесты чистого ядра усталостной модели.

Golden-master взят из сквозного примера §8 спеки усталости: присед 100 кг x 10,
RIR 2. Спека даёт L_set ~= 10.1 AU и механику ~= 650 кг*повт.
"""

from datetime import datetime, timedelta, timezone

import pytest

from api.services.fatigue.core import (
    CompartmentLoads,
    SetImpulse,
    e1rm_epley,
    effort_factor,
    internal_load_set,
    mechanical_load_set,
    split_to_compartments,
)
from api.services.fatigue.params import DEFAULT_PARAMS as P

T0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def _squat_impulse(**overrides) -> SetImpulse:
    base = dict(
        at=T0, weight_kg=100.0, reps=10, rir=2,
        main_muscle="quads", secondary_muscles=("glutes", "hamstrings"),
        fatigue_tier=1,
    )
    base.update(overrides)
    return SetImpulse(**base)


# --- e1RM ---

def test_e1rm_epley_ignores_rir():
    # Ядро усталости считает по спеке: RIR учитывается отдельным множителем EF.
    assert e1rm_epley(100.0, 10) == pytest.approx(133.333, abs=0.001)


def test_e1rm_of_single_rep_is_the_weight_plus_epley_step():
    assert e1rm_epley(100.0, 1) == pytest.approx(103.333, abs=0.001)


def test_e1rm_is_intentionally_different_from_autoprogression():
    """Осознанное расхождение, а не рассинхрон.

    autoprogression.set_target_value складывает RIR внутрь Эпли и остаётся
    ЕДИНСТВЕННЫМ e1RM, который видит пользователь. Ядро усталости считает
    «сырой» e1RM, потому что вклад RIR идёт отдельным множителем EF, и
    применять его дважды нельзя. Наружу e1rm_epley не отдаётся никогда.
    """
    from api.services.autoprogression import set_target_value

    assert e1rm_epley(100.0, 10) != pytest.approx(set_target_value(100.0, 10, 2))


# --- EF ---

def test_effort_factor_at_reference_rir_is_one():
    assert effort_factor(P.rir_ref, P) == pytest.approx(1.0)


def test_effort_factor_grows_towards_failure():
    assert effort_factor(0, P) > effort_factor(2, P) > effort_factor(4, P)


def test_effort_factor_matches_spec_example():
    # exp(0.15 * (4 - 2)) = 1.3499
    assert effort_factor(2, P) == pytest.approx(1.3499, abs=0.001)


def test_effort_factor_is_capped():
    assert effort_factor(-100, P) == pytest.approx(P.ef_cap)


# --- Нагрузки ---

def test_internal_load_matches_spec_golden_master():
    assert internal_load_set(100.0, 10, 2, P) == pytest.approx(10.12, abs=0.02)


def test_mechanical_load_matches_spec_golden_master():
    assert mechanical_load_set(100.0, 10, P) == pytest.approx(649.5, abs=1.0)


def test_zero_weight_gives_zero_mechanical_load():
    assert mechanical_load_set(0.0, 12, P) == 0.0


def test_zero_reps_gives_zero_loads():
    assert internal_load_set(100.0, 0, 2, P) == 0.0
    assert mechanical_load_set(100.0, 0, P) == 0.0


def test_internal_load_is_monotonic_in_reps():
    a = internal_load_set(100.0, 5, 2, P)
    b = internal_load_set(100.0, 10, 2, P)
    assert b > a


def test_mechanical_load_is_monotonic_in_weight():
    a = mechanical_load_set(100.0, 5, P)
    b = mechanical_load_set(120.0, 5, P)
    assert b > a


# --- Отсеки ---

def test_split_preserves_internal_load_total():
    loads = split_to_compartments(_squat_impulse(), P)
    total_muscular = sum(loads.muscular.values())
    expected = internal_load_set(100.0, 10, 2, P)
    assert loads.systemic + total_muscular == pytest.approx(expected)


def test_split_routes_mechanical_separately():
    loads = split_to_compartments(_squat_impulse(), P)
    assert loads.mechanical == pytest.approx(mechanical_load_set(100.0, 10, P))


def test_main_muscle_gets_more_than_secondary():
    loads = split_to_compartments(_squat_impulse(), P)
    assert loads.muscular["quads"] > loads.muscular["glutes"]


def test_heavy_tier_sends_more_to_systemic():
    heavy = split_to_compartments(_squat_impulse(fatigue_tier=1), P)
    light = split_to_compartments(_squat_impulse(fatigue_tier=3), P)
    assert heavy.systemic > light.systemic


def test_exercise_without_secondary_muscles_loads_only_main():
    loads = split_to_compartments(_squat_impulse(secondary_muscles=()), P)
    assert set(loads.muscular) == {"quads"}


def test_unknown_tier_falls_back_without_crashing():
    loads = split_to_compartments(_squat_impulse(fatigue_tier=99), P)
    assert loads.systemic > 0
