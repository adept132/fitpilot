"""Правила снижения нагрузки (спека P0-06 §8.2)."""

import pytest

from api.services.progression import params
from api.services.progression.reduction import apply_reduction
from api.services.progression.types import (
    ExerciseHistory,
    Outcome,
    Prescription,
    ProgressionState,
    SchemeContext,
    SessionFact,
    SetFact,
    SetPrescription,
)


def presc(weight=44.0, sets_count=3) -> Prescription:
    return Prescription(
        scheme=params.SCHEME_DOUBLE,
        sets=tuple(
            SetPrescription(n, weight, 8, 12, 2, "normal")
            for n in range(1, sets_count + 1)
        ),
        reason_code="progressed",
        reason_text=params.REASON_TEXTS["progressed"],
    )


def ctx(**kw) -> SchemeContext:
    base = dict(
        history=ExerciseHistory(exercise_id=1),
        state=ProgressionState(last_top_weight=40.0),
        last_outcome=Outcome(status="hit", hit_sets=3, total_sets=3),
        target_sets=3,
        rep_min=8,
        rep_max=12,
        rep_range_source=params.REP_SOURCE_FALLBACK,
        target_rir=2,
        equipment=("barbell",),
        phase_effort_tier="medium",
        days_since_last_session=3,
    )
    base.update(kw)
    return SchemeContext(**base)


def top(p: Prescription) -> float:
    return p.top_weight


def test_clean_hit_passes_the_prescription_through_untouched():
    p = presc()
    assert apply_reduction(p, ctx()) == p


def test_deload_phase_holds_the_previous_weight():
    out = apply_reduction(presc(), ctx(phase_effort_tier="deload"))
    assert top(out) == pytest.approx(40.0)
    assert out.reason_code == "deload_phase"


def test_deload_without_anchor_keeps_scheme_weight():
    # Нет last_top_weight (например, первая обработка упражнения с
    # легаси-логами на неделе разгрузки) — вес схемы нельзя ни обнулить,
    # ни выдумать, только сообщить причину.
    p = presc(weight=44.0)
    out = apply_reduction(
        p,
        ctx(
            state=ProgressionState(last_top_weight=None),
            phase_effort_tier="deload",
        ),
    )
    assert out.reason_code == "deload_phase"
    assert top(out) == pytest.approx(44.0)
    assert all(s.weight_kg == pytest.approx(44.0) for s in out.sets)


def test_layoff_reduces_from_the_last_working_weight():
    out = apply_reduction(presc(), ctx(days_since_last_session=30))
    assert out.reason_code == "layoff"
    assert top(out) < 40.0


def test_short_break_is_not_a_layoff():
    out = apply_reduction(presc(), ctx(days_since_last_session=params.LAYOFF_DAYS))
    assert out.reason_code != "layoff"


def test_deviation_reanchors_without_growth():
    out = apply_reduction(
        presc(), ctx(last_outcome=Outcome(status="deviated", total_sets=3))
    )
    assert out.reason_code == "weight_deviation"
    assert top(out) == pytest.approx(40.0)


def test_deviation_reanchors_to_actual_weight_from_history():
    # Нагружаем реальный путь: _actual_last_weight должен найти вес по
    # обычным подходам и отфильтровать аномальный, а не свалиться на
    # запасной путь actual or anchor.
    history = ExerciseHistory(
        exercise_id=1,
        sessions=(
            SessionFact(
                session_id=1,
                finished_at=None,
                prescription=None,
                sets=(
                    SetFact(1, 42.0, 8, 2, "normal", False),
                    SetFact(2, 42.0, 8, 2, "normal", False),
                    SetFact(3, 999.0, 8, 2, "normal", True),  # аномалия — не в счёт
                ),
            ),
        ),
    )
    out = apply_reduction(
        presc(weight=44.0),
        ctx(
            history=history,
            state=ProgressionState(last_top_weight=40.0),
            last_outcome=Outcome(status="deviated", total_sets=3),
        ),
    )
    assert out.reason_code == "weight_deviation"
    assert top(out) == pytest.approx(42.0)


def test_deviation_without_anchor_or_actual_does_not_fire():
    # Ни фактического веса из истории, ни якоря нет — переякорить не на
    # что. Правило 3 не срабатывает, управление идёт дальше по списку и
    # (при отсутствии других триггеров) предписание схемы проходит как есть.
    p = presc()
    out = apply_reduction(
        p,
        ctx(
            state=ProgressionState(last_top_weight=None),
            last_outcome=Outcome(status="deviated", total_sets=3),
        ),
    )
    assert out.reason_code != "weight_deviation"
    assert out == p


def test_one_miss_holds_the_weight():
    out = apply_reduction(
        presc(),
        ctx(
            state=ProgressionState(last_top_weight=40.0, consecutive_misses=1),
            last_outcome=Outcome(status="miss", hit_sets=2, miss_sets=1, total_sets=3),
        ),
    )
    assert out.reason_code == "hold_after_miss"
    assert top(out) == pytest.approx(40.0)


def test_two_misses_reduce_the_weight():
    out = apply_reduction(
        presc(),
        ctx(
            state=ProgressionState(last_top_weight=40.0, consecutive_misses=2),
            last_outcome=Outcome(status="miss", hit_sets=1, miss_sets=2, total_sets=3),
        ),
    )
    assert out.reason_code == "repeated_miss"
    assert top(out) < 40.0


def test_severe_miss_reduces_immediately():
    # Провал половины подходов и более — подтверждать второй неделей незачем.
    out = apply_reduction(
        presc(),
        ctx(
            state=ProgressionState(last_top_weight=40.0, consecutive_misses=1),
            last_outcome=Outcome(status="miss", hit_sets=1, miss_sets=2, total_sets=3),
        ),
    )
    assert out.reason_code == "repeated_miss"


def test_plateau_reduces_the_weight():
    out = apply_reduction(
        presc(),
        ctx(state=ProgressionState(last_top_weight=40.0, stalled=True)),
    )
    assert out.reason_code == "plateau_reset"
    assert top(out) < 40.0


def test_only_one_rule_fires_even_when_several_match():
    # Плато И два недобора: снижение должно быть однократным, не -20 %.
    out = apply_reduction(
        presc(),
        ctx(
            state=ProgressionState(
                last_top_weight=40.0, consecutive_misses=2, stalled=True
            ),
            last_outcome=Outcome(status="miss", hit_sets=1, miss_sets=2, total_sets=3),
        ),
    )
    assert out.reason_code == "repeated_miss"
    assert top(out) == pytest.approx(
        apply_reduction(
            presc(),
            ctx(
                state=ProgressionState(last_top_weight=40.0, consecutive_misses=2),
                last_outcome=Outcome(
                    status="miss", hit_sets=1, miss_sets=2, total_sets=3
                ),
            ),
        ).top_weight
    )


def test_reduction_always_lowers_by_at_least_one_step():
    # На блочном шаге 4.5 кг округление к ближайшему вернуло бы исходные 30.
    out = apply_reduction(
        presc(weight=30.0),
        ctx(
            equipment=("block_machine",),
            state=ProgressionState(last_top_weight=30.0, stalled=True),
        ),
    )
    assert out.top_weight < 30.0


def test_reduction_keeps_reps_and_set_count():
    out = apply_reduction(
        presc(sets_count=4),
        ctx(state=ProgressionState(last_top_weight=40.0, stalled=True)),
    )
    assert len(out.sets) == 4
    assert all(s.rep_min == 8 and s.rep_max == 12 for s in out.sets)


def test_reason_text_matches_the_code():
    out = apply_reduction(
        presc(), ctx(state=ProgressionState(last_top_weight=40.0, stalled=True))
    )
    assert out.reason_text == params.REASON_TEXTS[out.reason_code]


def test_empty_prescription_is_returned_as_is():
    empty = Prescription(
        scheme=params.SCHEME_DOUBLE,
        sets=(),
        reason_code="no_basis",
        reason_text=params.REASON_TEXTS["no_basis"],
    )
    assert apply_reduction(empty, ctx(phase_effort_tier="deload")) == empty
