"""Double progression (спека P0-06 §6.2)."""

import pytest

from api.services.progression import params
from api.services.progression.schemes.double import plan
from api.services.progression.types import (
    ExerciseHistory,
    Prescription,
    ProgressionState,
    SchemeContext,
    SessionFact,
    SetFact,
    SetPrescription,
)


def last_session(weight, reps_per_set, rep_min=8, rep_max=12) -> ExerciseHistory:
    presc = Prescription(
        scheme=params.SCHEME_DOUBLE,
        sets=tuple(
            SetPrescription(i + 1, weight, rep_min, rep_max, 2, "normal")
            for i in range(len(reps_per_set))
        ),
        reason_code="progressed",
        reason_text="x",
    )
    facts = tuple(
        SetFact(i + 1, weight, reps, 2) for i, reps in enumerate(reps_per_set)
    )
    return ExerciseHistory(
        exercise_id=1, sessions=(SessionFact(1, None, presc, facts),)
    )


def ctx(history, **kw) -> SchemeContext:
    base = dict(
        history=history,
        state=ProgressionState(last_top_weight=40.0),
        last_outcome=None,
        target_sets=3,
        rep_min=8,
        rep_max=12,
        rep_range_source=params.REP_SOURCE_FALLBACK,
        target_rir=2,
        equipment=("barbell",),
    )
    base.update(kw)
    return SchemeContext(**base)


def test_each_set_target_grows_from_its_own_previous_reps():
    p = plan(ctx(last_session(40.0, [10, 9, 7])))
    assert [s.rep_min for s in p.sets] == [11, 10, 8]


def test_targets_are_capped_at_rep_max():
    p = plan(ctx(last_session(40.0, [12, 11, 12])))
    # Не все подходы на потолке -> вес держим, цели упираются в rep_max.
    assert [s.rep_min for s in p.sets] == [12, 12, 12]
    assert all(s.weight_kg == pytest.approx(40.0) for s in p.sets)


def test_all_sets_at_rep_max_advances_weight_and_resets_reps():
    p = plan(ctx(last_session(40.0, [12, 12, 12])))
    assert all(s.weight_kg == pytest.approx(42.5) for s in p.sets)
    assert all(s.rep_min == 8 for s in p.sets)
    assert p.reason_code == "progressed"


def test_weight_holds_while_any_set_is_below_rep_max():
    p = plan(ctx(last_session(40.0, [12, 12, 11])))
    assert all(s.weight_kg == pytest.approx(40.0) for s in p.sets)


def test_extra_target_sets_reuse_last_known_reps():
    p = plan(ctx(last_session(40.0, [10, 9]), target_sets=4))
    assert [s.rep_min for s in p.sets] == [11, 10, 10, 10]


def test_rep_max_is_carried_into_every_set():
    p = plan(ctx(last_session(40.0, [10, 9, 7])))
    assert all(s.rep_max == 12 for s in p.sets)


def test_target_rir_is_carried_into_every_set():
    p = plan(ctx(last_session(40.0, [10, 9, 7]), target_rir=1))
    assert all(s.rir == 1 for s in p.sets)


def test_bodyweight_exercise_keeps_weight_none_and_grows_reps():
    history = last_session(None, [10, 10, 10])
    p = plan(ctx(history, equipment=("bodyweight",)))
    assert all(s.weight_kg is None for s in p.sets)
    assert [s.rep_min for s in p.sets] == [11, 11, 11]


def test_bodyweight_at_ceiling_asks_for_external_load():
    history = last_session(None, [12, 12, 12])
    p = plan(ctx(history, equipment=("bodyweight",)))
    assert p.reason_code == "needs_external_load"
    assert p.reason_text == params.REASON_TEXTS["needs_external_load"]


def test_no_history_falls_back_to_last_top_weight_at_rep_min():
    p = plan(ctx(ExerciseHistory(exercise_id=1)))
    assert all(s.rep_min == 8 for s in p.sets)
    assert all(s.weight_kg == pytest.approx(40.0) for s in p.sets)


def test_no_history_and_no_anchor_gives_no_basis():
    p = plan(ctx(ExerciseHistory(exercise_id=1), state=ProgressionState()))
    assert p.reason_code == "no_basis"
    assert p.sets == ()


def test_scheme_name_is_recorded():
    p = plan(ctx(last_session(40.0, [10, 9, 7])))
    assert p.scheme == params.SCHEME_DOUBLE


def test_scheme_never_lowers_the_weight():
    # Инвариант: схемы умеют только повышать. Даже при полном провале
    # вес не уменьшается — это работа reduction.py.
    p = plan(ctx(last_session(40.0, [3, 2, 1])))
    assert all(s.weight_kg >= 40.0 for s in p.sets)
