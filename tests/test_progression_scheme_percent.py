"""%1RM / training max и AMRAP (спека P0-06 §6.4)."""

import pytest

from api.services.progression import params
from api.services.progression.rounding import round_down_to_step
from api.services.progression.schemes.percent_1rm import plan, training_max_for
from api.services.progression.types import (
    ExerciseHistory,
    ProgressionState,
    SchemeContext,
)


def ctx(**kw) -> SchemeContext:
    base = dict(
        history=ExerciseHistory(exercise_id=1),
        state=ProgressionState(working_e1rm=150.0),
        last_outcome=None,
        target_sets=3,
        rep_min=5,
        rep_max=5,
        rep_range_source=params.REP_SOURCE_FALLBACK,
        target_rir=1,
        equipment=("barbell",),
        fatigue_tier=1,
        phase_effort_tier="prefailure",
    )
    base.update(kw)
    return SchemeContext(**base)


def test_training_max_is_ninety_percent_rounded_down():
    tm = training_max_for(ctx())
    expected = round_down_to_step(
        150.0 * params.TRAINING_MAX_RATIO, ["barbell"], "kg", None
    )
    assert tm == pytest.approx(expected)
    assert tm <= 150.0 * params.TRAINING_MAX_RATIO


def test_no_working_e1rm_gives_no_basis():
    p = plan(ctx(state=ProgressionState()))
    assert p.reason_code == "no_basis"
    assert p.sets == ()


def test_set_count_follows_the_percent_table_not_target_sets():
    p = plan(ctx(target_sets=99))
    assert len(p.sets) == len(params.PERCENT_TABLE["prefailure"])


def test_last_set_is_amrap_with_open_rep_max():
    p = plan(ctx())
    assert p.sets[-1].kind == "amrap"
    assert p.sets[-1].rep_max is None


def test_earlier_sets_are_normal_and_closed():
    p = plan(ctx())
    for s in p.sets[:-1]:
        assert s.kind == "normal"
        assert s.rep_max is not None


def test_weights_follow_the_percent_table():
    c = ctx()
    tm = training_max_for(c)
    p = plan(c)
    for prescribed, (ratio, _reps, _kind) in zip(
        p.sets, params.PERCENT_TABLE["prefailure"]
    ):
        expected = round_down_to_step(tm * ratio, ["barbell"], "kg", None)
        assert prescribed.weight_kg == pytest.approx(expected)


def test_weights_increase_across_the_session():
    weights = [s.weight_kg for s in plan(ctx()).sets]
    assert weights == sorted(weights)


def test_deload_phase_has_no_amrap():
    p = plan(ctx(phase_effort_tier="deload"))
    assert all(s.kind == "normal" for s in p.sets)


def test_unknown_phase_falls_back_to_medium():
    p = plan(ctx(phase_effort_tier="странная_фаза"))
    assert len(p.sets) == len(params.PERCENT_TABLE["medium"])


def test_basis_records_training_max():
    p = plan(ctx())
    assert p.basis["training_max"] == pytest.approx(training_max_for(ctx()))


def test_scheme_name_is_recorded():
    assert plan(ctx()).scheme == params.SCHEME_PERCENT_1RM
