"""Решатель: что и когда предлагается. Чистые функции, БД не участвует."""
from api.services.periodization import params
from api.services.periodization.decide import decide
from api.services.periodization.types import (
    BlockPosition,
    DecisionInput,
    FatigueSignal,
    PlateauSignal,
    ReadinessSignal,
)


def _pos(**over) -> BlockPosition:
    base = dict(
        phase_number=2,
        effort_tier="medium",
        phase_ordinal=2,
        phases_total=4,
        day_in_block=10,
        days_to_deload=14,
        is_last_phase=False,
        is_complete=False,
    )
    base.update(over)
    return BlockPosition(**base)


def _inp(**over) -> DecisionInput:
    base = dict(position=_pos(), workouts_to_planned_deload=8)
    base.update(over)
    return DecisionInput(**base)


def _kinds(inp: DecisionInput) -> list[str]:
    return [p.kind for p in decide(inp)]


# --- Триггер по усталости ---

def test_sustained_fatigue_proposes_early_deload():
    inp = _inp(fatigue=FatigueSignal(fatigued_days=5, band_known=True))
    result = decide(inp)
    assert [p.kind for p in result] == [params.KIND_EARLY_DELOAD]
    assert result[0].reason_code == params.REASON_FATIGUE_HIGH


def test_fatigue_below_threshold_proposes_nothing():
    assert _kinds(_inp(fatigue=FatigueSignal(fatigued_days=4, band_known=True))) == []


def test_sharp_rise_alone_proposes_early_deload():
    result = decide(_inp(fatigue=FatigueSignal(sharp_rise=True, band_known=True)))
    assert result[0].reason_code == params.REASON_LOAD_SPIKE


def test_cold_start_is_not_treated_as_rested():
    """band_known=False — модель ещё не знает; реагировать не на что."""
    assert _kinds(_inp(fatigue=FatigueSignal(fatigued_days=9, band_known=False))) == []


# --- Триггер по плато ---

def test_block_plateau_proposes_early_deload():
    result = decide(_inp(plateau=PlateauSignal(exercises_with_history=6, stalled=3)))
    assert result[0].reason_code == params.REASON_BLOCK_PLATEAU


def test_plateau_needs_enough_exercises():
    """Одно вставшее из двух — тоже 50 %, но это шум, а не плато блока."""
    assert _kinds(_inp(plateau=PlateauSignal(exercises_with_history=2, stalled=1))) == []


# --- Триггер по готовности ---

def test_repeated_limit_verdicts_propose_early_deload():
    signal = ReadinessSignal(recent_levels=("limit", "ok", "limit", "limit", "ok"))
    result = decide(_inp(readiness=signal))
    assert result[0].reason_code == params.REASON_READINESS_LIMITED


def test_limits_outside_the_window_do_not_count():
    signal = ReadinessSignal(recent_levels=("ok", "ok", "ok", "ok", "ok", "limit", "limit", "limit"))
    assert _kinds(_inp(readiness=signal)) == []


# --- Предохранители ---

def test_no_proposal_in_the_first_phase():
    inp = _inp(position=_pos(phase_ordinal=1), fatigue=FatigueSignal(fatigued_days=9, band_known=True))
    assert _kinds(inp) == []


def test_no_proposal_during_deload():
    inp = _inp(
        position=_pos(effort_tier="deload", days_to_deload=0),
        fatigue=FatigueSignal(fatigued_days=9, band_known=True),
    )
    assert _kinds(inp) == []


def test_no_proposal_when_planned_deload_is_near():
    inp = _inp(
        workouts_to_planned_deload=3,
        fatigue=FatigueSignal(fatigued_days=9, band_known=True),
    )
    assert params.KIND_EARLY_DELOAD not in _kinds(inp)


def test_only_one_early_deload_per_block():
    inp = _inp(early_deload_used=True, fatigue=FatigueSignal(fatigued_days=9, band_known=True))
    assert _kinds(inp) == []


def test_no_proposal_when_next_phase_is_already_deload():
    """Иначе получаются две разгрузки подряд: предложенная досрочная и уже
    запланированная сразу за ней. Число тренировок до плановой разгрузки
    здесь высокое (первый день семидневной фазы), так что предохранитель
    по workouts_to_planned_deload не сработал бы — нужен именно этот."""
    inp = _inp(
        position=_pos(next_phase_is_deload=True),
        workouts_to_planned_deload=8,
        fatigue=FatigueSignal(fatigued_days=9, sharp_rise=True, band_known=True),
        plateau=PlateauSignal(exercises_with_history=6, stalled=5),
        readiness=ReadinessSignal(recent_levels=("limit", "limit", "limit")),
    )
    assert _kinds(inp) == []


def test_at_most_one_proposal_even_when_all_triggers_fire():
    inp = _inp(
        fatigue=FatigueSignal(fatigued_days=9, sharp_rise=True, band_known=True),
        plateau=PlateauSignal(exercises_with_history=6, stalled=5),
        readiness=ReadinessSignal(recent_levels=("limit", "limit", "limit")),
    )
    result = decide(inp)
    assert len(result) == 1
    assert result[0].reason_code == params.REASON_FATIGUE_HIGH, "усталость приоритетнее"


def test_payload_carries_the_evidence():
    result = decide(_inp(fatigue=FatigueSignal(fatigued_days=6, band_known=True)))
    assert result[0].payload["fatigued_days"] == 6


# --- Триггер по переносу плановой разгрузки ---

def test_dropped_load_proposes_postponing_the_planned_deload():
    """Плановая разгрузка уже на носу (2 тренировки), а нагрузка упала.
    Разгружаться не от чего — сдвигаем планы."""
    inp = _inp(
        workouts_to_planned_deload=2,
        fatigue=FatigueSignal(chronic_level=30.0, chronic_at_block_start=100.0),
    )
    result = decide(inp)
    assert [p.kind for p in result] == [params.KIND_POSTPONE_DELOAD]
    assert result[0].reason_code == params.REASON_LOAD_DROPPED
    assert "chronic_ratio" in result[0].payload
    assert result[0].payload["chronic_ratio"] == 0.3


def test_postpone_is_not_proposed_when_load_held_up():
    """Хроническая нагрузка не упала достаточно сильно — разгружаться не нужно,
    плановая разгрузка пойдёт по графику."""
    inp = _inp(
        workouts_to_planned_deload=2,
        fatigue=FatigueSignal(chronic_level=90.0, chronic_at_block_start=100.0),
    )
    assert _kinds(inp) == []


def test_postpone_needs_a_baseline():
    """Без исходного уровня нагрузки на старте блока нельзя считать падение.
    Оба случая — None и 0.0 — должны быть обработаны без деления на ноль."""
    # Case 1: chronic_at_block_start = None
    inp = _inp(
        workouts_to_planned_deload=2,
        fatigue=FatigueSignal(chronic_level=30.0, chronic_at_block_start=None),
    )
    assert _kinds(inp) == []

    # Case 2: chronic_at_block_start = 0.0
    inp = _inp(
        workouts_to_planned_deload=2,
        fatigue=FatigueSignal(chronic_level=30.0, chronic_at_block_start=0.0),
    )
    assert _kinds(inp) == []


def test_postpone_not_proposed_while_planned_deload_is_far():
    """Хроническая нагрузка упала, но до плановой разгрузки ещё далеко
    (8 тренировок > PLANNED_DELOAD_NEAR_WORKOUTS=3) — не предлагаем перенос."""
    inp = _inp(
        workouts_to_planned_deload=8,
        fatigue=FatigueSignal(chronic_level=30.0, chronic_at_block_start=100.0),
    )
    assert _kinds(inp) == []


def test_early_deload_and_postpone_are_mutually_exclusive():
    """Когда плановая разгрузка рядом, предохранитель отключает досрочную разгрузку.
    Если при этом сработали условия переноса плановой, только перенос выйдет
    в результате."""
    inp = _inp(
        workouts_to_planned_deload=2,
        fatigue=FatigueSignal(
            fatigued_days=9,
            band_known=True,
            chronic_level=30.0,
            chronic_at_block_start=100.0,
        ),
    )
    result = decide(inp)
    assert len(result) == 1
    assert result[0].kind == params.KIND_POSTPONE_DELOAD


# --- Границы порогов плато ---

def test_plateau_triggers_exactly_at_the_minimum_exercises():
    """Ровно на пороге PLATEAU_MIN_EXERCISES (3 упражнения с историей):
    - С 2 вставшими (2/3 ≈ 0.67 > 0.5) предложение есть
    - С 1 вставшим (1/3 ≈ 0.33 < 0.5) предложения нет"""
    # Ровно 3 упражнения, 2 вставших — триггер сработает
    inp = _inp(plateau=PlateauSignal(exercises_with_history=3, stalled=2))
    result = decide(inp)
    assert len(result) == 1
    assert result[0].kind == params.KIND_EARLY_DELOAD
    assert result[0].reason_code == params.REASON_BLOCK_PLATEAU

    # Ровно 3 упражнения, 1 вставшее — триггер не сработает
    inp = _inp(plateau=PlateauSignal(exercises_with_history=3, stalled=1))
    assert _kinds(inp) == []


def test_readiness_window_shorter_than_limit_is_safe():
    """Окно вердиктов может быть меньше чем READINESS_LIMIT_WINDOW (5).
    Слайс не должен вызвать ошибку, и если недостаточно limit'ов,
    предложения не должно быть."""
    # Всего 2 вердикта, оба limit — это меньше чем READINESS_LIMIT_WINDOW=5
    # и меньше чем READINESS_LIMIT_COUNT=3
    signal = ReadinessSignal(recent_levels=("limit", "limit"))
    assert _kinds(_inp(readiness=signal)) == []
