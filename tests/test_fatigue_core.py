"""Тесты чистого ядра усталостной модели.

Golden-master взят из сквозного примера §8 спеки усталости: присед 100 кг x 10,
RIR 2. Спека даёт L_set ~= 10.1 AU и механику ~= 650 кг*повт.
"""

import math
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


# --- Распад ---

from api.services.fatigue.core import (  # noqa: E402
    Progression,
    Readiness,
    decay,
    ewma_progression,
    readiness_z,
)


def test_decay_of_empty_history_is_zero():
    assert decay([], 48.0, T0) == 0.0


def test_fresh_impulse_is_not_decayed():
    assert decay([(T0, 100.0)], 48.0, T0) == pytest.approx(100.0)


def test_one_tau_leaves_one_over_e():
    later = T0 + timedelta(hours=48)
    assert decay([(T0, 100.0)], 48.0, later) == pytest.approx(100.0 / math.e, abs=0.01)


def test_decay_is_additive_over_impulses():
    a = decay([(T0, 100.0)], 48.0, T0)
    b = decay([(T0, 50.0)], 48.0, T0)
    both = decay([(T0, 100.0), (T0, 50.0)], 48.0, T0)
    assert both == pytest.approx(a + b)


def test_more_load_never_reduces_fatigue():
    """Safety-рельс §6.1: монотонность."""
    base = [(T0, 100.0)]
    more = base + [(T0 + timedelta(hours=1), 10.0)]
    now = T0 + timedelta(hours=24)
    assert decay(more, 48.0, now) >= decay(base, 48.0, now)


def test_future_impulses_are_ignored():
    # Часы клиента могут уехать вперёд — не даём этому раздуть усталость.
    future = T0 + timedelta(days=3)
    assert decay([(future, 100.0)], 48.0, T0) == 0.0


# --- Готовность ---

def test_cold_start_hides_z():
    short = [10.0] * (P.cold_start_days - 1)
    result = readiness_z(short, P)
    assert result.z is None
    assert result.band == "unknown"


def test_flat_history_gives_neutral_band():
    flat = [10.0] * 30
    result = readiness_z(flat, P)
    assert result.z is not None
    assert result.band == "neutral"


def test_sigma_floor_prevents_z_explosion():
    # Почти нулевая дисперсия: без пола z ушёл бы в сотни.
    series = [10.0] * 29 + [10.2]
    result = readiness_z(series, P)
    assert abs(result.z) < 5


def test_high_recent_load_reads_as_fatigued():
    series = [5.0] * 29 + [60.0]
    assert readiness_z(series, P).band == "fatigued"


def test_low_recent_load_reads_as_fresh():
    series = [50.0] * 29 + [1.0]
    assert readiness_z(series, P).band == "fresh"


# --- Прогрессия ---

def test_progression_of_steady_load_is_about_one():
    steady = [10.0] * 60
    result = ewma_progression(steady, P)
    assert result.ratio == pytest.approx(1.0, abs=0.1)
    assert result.flag == "ok"


def test_sharp_rise_is_flagged():
    ramp = [5.0] * 45 + [40.0] * 15
    result = ewma_progression(ramp, P)
    assert result.ratio > P.sharp_rise_ratio
    assert result.flag == "sharp_rise"


def test_progression_reports_week_over_week_change():
    series = [10.0] * 53 + [20.0] * 7
    result = ewma_progression(series, P)
    assert result.wow_change_pct == pytest.approx(100.0, abs=1.0)


def test_progression_on_empty_history_is_safe():
    result = ewma_progression([], P)
    assert result.ratio is None
    assert result.flag == "ok"


def test_chronic_window_excludes_the_acute_tail():
    # Несущее свойство расцепления (§5): хроническое окно НЕ видит спайк в
    # последних tau_acute днях, иначе вернулся бы ACWR-артефакт сопряжения
    # (острое окно втекает в хроническое). Спайк в хвосте обязан оставить
    # chronic_level неизменным, но поднять острый сигнал (ratio).
    flat = [10.0] * 60
    spiked_tail = [10.0] * 53 + [1000.0] * 7
    base = ewma_progression(flat, P)
    spike = ewma_progression(spiked_tail, P)
    # Хроника считается по daily[:-tau_acute], хвост в неё не входит -> не меняется.
    assert spike.chronic_level == pytest.approx(base.chronic_level)
    # А острый EWMA хвост видит -> ratio растёт. При сопряжённой (наивной)
    # реализации, где хроника берёт весь ряд, spike.chronic вырос бы и первая
    # ассерция упала бы.
    assert spike.ratio > base.ratio


def test_median_helper_odd_and_even():
    from api.services.fatigue.core import _median

    # Нечётная длина — центральный элемент; порядок не важен (сортируется внутри).
    assert _median([3.0, 1.0, 2.0]) == pytest.approx(2.0)
    # Чётная длина — среднее двух центральных.
    assert _median([1.0, 2.0, 3.0, 4.0]) == pytest.approx(2.5)
    # Один элемент.
    assert _median([7.0]) == pytest.approx(7.0)


def test_sigma_floor_uses_median_not_mean():
    # Пол должен считаться от медианы. Здесь медиана (10) и среднее заметно
    # расходятся из-за хвоста, но floor привязан к устойчивой медиане.
    from api.services.fatigue.core import _median, _mean

    series = [10.0] * 20 + [10.0] * 9 + [200.0]
    assert _median(series) == pytest.approx(10.0)
    assert _mean(series) > 15.0  # среднее утащено выбросом вверх
    # readiness_z не падает и выдаёт конечный z (пол не даёт взрыва).
    result = readiness_z(series, P)
    assert result.z is not None
    assert math.isfinite(result.z)


# --- Время восстановления ---

def test_fatigued_series_yields_recovery_hours_matching_the_formula():
    # Ряд с явным всплеском в последний день -> band "fatigued". Считаем
    # ожидаемое время восстановления вручную по формуле Δ = τ · ln(F_now/F_target)
    # с теми же mean/denominator, что использует readiness_z, и сверяем.
    series = [5.0] * 29 + [60.0]
    tau = 48.0
    result = readiness_z(series, P, tau_hours=tau)

    assert result.band == "fatigued"
    assert result.recovery_hours is not None

    mean = sum(series) / len(series)
    std = math.sqrt(sum((v - mean) ** 2 for v in series) / (len(series) - 1))
    from api.services.fatigue.core import _median

    floor = abs(_median(series)) * P.sigma_floor_ratio
    denominator = max(std, floor)
    f_target = mean + P.band_threshold_z * denominator
    expected = tau * math.log(series[-1] / f_target)

    assert result.recovery_hours == pytest.approx(expected, rel=1e-9)
    assert result.recovery_hours > 0.0


def test_neutral_series_has_zero_recovery_hours():
    flat = [10.0] * 30
    result = readiness_z(flat, P, tau_hours=48.0)
    assert result.band == "neutral"
    assert result.recovery_hours == 0.0


def test_fresh_series_has_zero_recovery_hours():
    series = [50.0] * 29 + [1.0]
    result = readiness_z(series, P, tau_hours=48.0)
    assert result.band == "fresh"
    assert result.recovery_hours == 0.0


def test_cold_start_hides_recovery_hours():
    short = [10.0] * (P.cold_start_days - 1)
    result = readiness_z(short, P, tau_hours=48.0)
    assert result.band == "unknown"
    assert result.recovery_hours is None


def test_missing_tau_hides_recovery_hours():
    # Механика/мышца/система знают свой tau только у вызывающего кода
    # (service.py); без него readiness_z обязана молчать, а не гадать.
    series = [5.0] * 29 + [60.0]
    result = readiness_z(series, P, tau_hours=None)
    assert result.band == "fatigued"
    assert result.recovery_hours is None


def test_recovery_hours_is_capped():
    # Экстремальный всплеск даёт большое ln(F_now/F_target); c достаточно
    # большим tau «сырая» оценка превышает потолок. Проверяем, что срабатывает
    # КОНФИГ-предохранитель recovery_hours_cap, а не голая формула.
    series = [1.0] * 29 + [1e9]
    tau = 200.0
    result = readiness_z(series, P, tau_hours=tau)

    assert result.band == "fatigued"

    mean = sum(series) / len(series)
    std = math.sqrt(sum((v - mean) ** 2 for v in series) / (len(series) - 1))
    from api.services.fatigue.core import _median

    floor = abs(_median(series)) * P.sigma_floor_ratio
    denominator = max(std, floor)
    f_target = mean + P.band_threshold_z * denominator
    raw = tau * math.log(series[-1] / f_target)

    assert raw > P.recovery_hours_cap  # убеждаемся, что без потолка тест был бы иным
    assert result.recovery_hours == pytest.approx(P.recovery_hours_cap)
