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
