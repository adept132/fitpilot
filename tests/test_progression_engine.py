"""Оркестратор движка: порядок пяти шагов (спека P0-06 §5.4)."""

import pytest

from api.services.progression import params
from api.services.progression.engine import plan_exercise
from api.services.progression.types import (
    ExerciseHistory,
    Prescription,
    ProgressionState,
    SchemeContext,
    SessionFact,
    SetFact,
    SetPrescription,
)


def presc(weight, rep_min=8, rep_max=12, sets_count=3) -> Prescription:
    return Prescription(
        scheme=params.SCHEME_DOUBLE,
        sets=tuple(
            SetPrescription(n, weight, rep_min, rep_max, 2, "normal")
            for n in range(1, sets_count + 1)
        ),
        reason_code="progressed",
        reason_text="x",
    )


def session(idx, weight, reps_per_set, *, is_deload=False) -> SessionFact:
    return SessionFact(
        session_id=idx,
        finished_at=None,
        prescription=presc(weight, sets_count=len(reps_per_set)),
        sets=tuple(SetFact(i + 1, weight, r, 2) for i, r in enumerate(reps_per_set)),
        is_deload=is_deload,
    )


def ctx(*sessions, **kw) -> SchemeContext:
    base = dict(
        history=ExerciseHistory(exercise_id=1, sessions=tuple(reversed(sessions))),
        state=ProgressionState(),
        last_outcome=None,
        target_sets=3,
        rep_min=8,
        rep_max=12,
        rep_range_source=params.REP_SOURCE_FALLBACK,
        target_rir=2,
        equipment=("barbell",),
        experience_level="intermediate",
        phase_effort_tier="medium",
        days_since_last_session=3,
    )
    base.update(kw)
    return SchemeContext(**base)


def test_empty_history_returns_no_basis():
    p = plan_exercise(ctx())
    assert p.reason_code == "no_basis"
    assert p.sets == ()


def test_state_is_rebuilt_even_if_caller_passed_an_empty_one():
    # Оркестратор обязан сам восстановить состояние из истории.
    p = plan_exercise(ctx(session(1, 40.0, [10, 9, 7])))
    assert p.sets, "предписание не должно быть пустым при непустой истории"


def test_successful_ceiling_session_advances_the_weight():
    p = plan_exercise(ctx(session(1, 40.0, [12, 12, 12])))
    assert p.top_weight == pytest.approx(42.5)
    assert p.reason_code == "progressed"


def test_single_miss_holds_the_weight():
    p = plan_exercise(ctx(session(1, 40.0, [10, 9, 5])))
    assert p.top_weight == pytest.approx(40.0)
    assert p.reason_code == "hold_after_miss"


def test_two_misses_in_a_row_reduce_the_weight():
    p = plan_exercise(
        ctx(session(1, 40.0, [7, 10, 10]), session(2, 40.0, [6, 10, 10]))
    )
    assert p.top_weight < 40.0
    assert p.reason_code == "repeated_miss"


def test_override_changes_the_scheme():
    p = plan_exercise(
        ctx(session(1, 100.0, [5, 5, 5]), rep_min=5, rep_max=5),
        override=params.SCHEME_PERCENT_1RM,
    )
    assert p.scheme == params.SCHEME_PERCENT_1RM


def test_reason_code_and_text_are_always_populated():
    for c in [ctx(), ctx(session(1, 40.0, [12, 12, 12])), ctx(session(1, 40.0, [3]))]:
        p = plan_exercise(c)
        assert p.reason_code
        assert p.reason_text


def test_engine_version_is_stamped():
    from api.services.progression.types import ENGINE_VERSION

    p = plan_exercise(ctx(session(1, 40.0, [12, 12, 12])))
    assert p.engine_version == ENGINE_VERSION


def test_provisional_flag_is_propagated():
    p = plan_exercise(ctx(session(1, 40.0, [12, 12, 12])), provisional=True)
    assert p.provisional is True
