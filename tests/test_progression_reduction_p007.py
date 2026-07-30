"""Объективные строки 7-9 таблицы снижения (спека P0-07 §7.1, §8)."""

import pytest

from api.services.progression import params
from api.services.progression.reduction import apply_reduction
from api.services.progression.types import (
    ExerciseHistory,
    Outcome,
    Prescription,
    ProgressionState,
    SchemeContext,
    SetPrescription,
)

ANCHOR = 40.0


def presc(weight=45.0) -> Prescription:
    return Prescription(
        scheme="double",
        sets=(
            SetPrescription(1, weight, 8, 12, 2, "normal"),
            SetPrescription(2, weight, 8, 12, 2, "normal"),
        ),
        reason_code="progressed",
        reason_text="x",
    )


def ctx(**kw) -> SchemeContext:
    base = dict(
        history=ExerciseHistory(exercise_id=1),
        state=ProgressionState(last_top_weight=ANCHOR),
        last_outcome=None,
        target_sets=2,
        rep_min=8,
        rep_max=12,
        rep_range_source=params.REP_SOURCE_FALLBACK,
        target_rir=2,
        equipment=("barbell",),
        unit="kg",
        phase_effort_tier="medium",
        days_since_last_session=3,
    )
    base.update(kw)
    return SchemeContext(**base)


def strained_outcome() -> Outcome:
    return Outcome(status="strained", hit_sets=2, total_sets=2, strained_sets=2)


def test_strained_holds_the_previous_weight():
    result = apply_reduction(presc(), ctx(last_outcome=strained_outcome()))
    assert result.reason_code == "strained_hold"
    assert result.top_weight == pytest.approx(ANCHOR)


def test_strained_never_reduces():
    result = apply_reduction(presc(), ctx(last_outcome=strained_outcome()))
    assert result.top_weight >= ANCHOR


def test_skipped_exercise_holds():
    result = apply_reduction(presc(), ctx(last_session_skipped=True))
    assert result.reason_code == "exercise_skipped"
    assert result.top_weight == pytest.approx(ANCHOR)


def test_soft_layoff_holds():
    result = apply_reduction(presc(), ctx(days_since_last_session=14))
    assert result.reason_code == "layoff_soft"
    assert result.top_weight == pytest.approx(ANCHOR)


def test_soft_layoff_does_not_fire_below_the_threshold():
    result = apply_reduction(presc(), ctx(days_since_last_session=9))
    assert result.reason_code == "progressed"


def test_hard_layoff_still_wins_over_soft():
    result = apply_reduction(presc(), ctx(days_since_last_session=45))
    assert result.reason_code == "layoff"
    assert result.top_weight < ANCHOR


def test_plateau_outranks_strain():
    # Снижение сильнее удержания — оно и должно сработать.
    result = apply_reduction(
        presc(),
        ctx(
            last_outcome=strained_outcome(),
            state=ProgressionState(last_top_weight=ANCHOR, stalled=True),
        ),
    )
    assert result.reason_code == "plateau_reset"
    assert result.top_weight < ANCHOR


def test_repeated_miss_outranks_soft_layoff():
    result = apply_reduction(
        presc(),
        ctx(
            last_outcome=Outcome(status="miss", miss_sets=2, total_sets=2),
            days_since_last_session=14,
            state=ProgressionState(last_top_weight=ANCHOR, consecutive_misses=2),
        ),
    )
    assert result.reason_code == "repeated_miss"


def test_deload_still_outranks_everything_new():
    result = apply_reduction(
        presc(), ctx(last_outcome=strained_outcome(), phase_effort_tier="deload")
    )
    assert result.reason_code == "deload_phase"


def test_new_rules_need_an_anchor():
    # Якоря нет — выдумывать вес нельзя, правило не срабатывается.
    result = apply_reduction(
        presc(), ctx(last_outcome=strained_outcome(), state=ProgressionState())
    )
    assert result.reason_code == "progressed"


def test_held_weight_lands_on_the_equipment_grid():
    # anchor мог прийти из легаси-лога мимо сетки; это НОВОЕ предписание.
    result = apply_reduction(
        presc(),
        ctx(last_session_skipped=True, state=ProgressionState(last_top_weight=41.3)),
    )
    assert result.top_weight == pytest.approx(42.5)


def test_every_new_reason_has_text():
    for code in ("strained_hold", "exercise_skipped", "layoff_soft"):
        assert params.REASON_TEXTS[code].strip()
