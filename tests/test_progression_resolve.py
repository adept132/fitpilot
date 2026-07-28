"""Трёхслойная цепочка выбора схемы (спека P0-06 §7)."""

import pytest

from api.services.progression import params
from api.services.progression.resolve import override_for, resolve_scheme
from api.services.progression.types import (
    ExerciseHistory,
    Prescription,
    ProgressionState,
    SchemeContext,
    SessionFact,
    SetPrescription,
)


def ctx(**kw) -> SchemeContext:
    base = dict(
        history=ExerciseHistory(exercise_id=1),
        state=ProgressionState(),
        last_outcome=None,
        target_sets=3,
        rep_min=8,
        rep_max=12,
        rep_range_source=params.REP_SOURCE_FALLBACK,
        target_rir=2,
        equipment=("dumbbell",),
        fatigue_tier=2,
        experience_level="intermediate",
        phase_effort_tier="medium",
    )
    base.update(kw)
    return SchemeContext(**base)


def with_prescription() -> ExerciseHistory:
    p = Prescription(
        scheme="double",
        sets=(SetPrescription(1, 40.0, 8, 12, 2, "normal"),),
        reason_code="progressed",
        reason_text="x",
    )
    return ExerciseHistory(
        exercise_id=1,
        sessions=(SessionFact(1, None, p, ()),),
    )


def test_no_prescription_history_bootstraps_with_e1rm_factor():
    assert resolve_scheme(ctx()) == params.SCHEME_E1RM_FACTOR


def test_manual_override_wins_over_everything():
    got = resolve_scheme(
        ctx(history=with_prescription(), fatigue_tier=3),
        override=params.SCHEME_PERCENT_1RM,
    )
    assert got == params.SCHEME_PERCENT_1RM


def test_unknown_override_is_ignored():
    got = resolve_scheme(ctx(history=with_prescription()), override="astrology")
    assert got == params.SCHEME_DOUBLE


def test_strength_phase_on_tier_one_picks_percent_1rm():
    got = resolve_scheme(
        ctx(
            history=with_prescription(),
            fatigue_tier=1,
            equipment=("barbell",),
            phase_effort_tier="prefailure",
        )
    )
    assert got == params.SCHEME_PERCENT_1RM


def test_strength_phase_on_isolation_does_not_pick_percent_1rm():
    got = resolve_scheme(
        ctx(history=with_prescription(), fatigue_tier=3, phase_effort_tier="failure")
    )
    assert got != params.SCHEME_PERCENT_1RM


def test_heuristic_never_picks_percent_1rm():
    for tier in (1, 2, 3):
        for phase in ("easy", "medium", "deload"):
            got = resolve_scheme(
                ctx(
                    history=with_prescription(),
                    fatigue_tier=tier,
                    equipment=("barbell",),
                    phase_effort_tier=phase,
                )
            )
            assert got != params.SCHEME_PERCENT_1RM, (tier, phase)


def test_degenerate_range_picks_fixed_increment():
    # override_reps: "10" -> rmin == rmax, у double нулевой зазор.
    got = resolve_scheme(ctx(history=with_prescription(), rep_min=10, rep_max=10))
    assert got == params.SCHEME_FIXED_INCREMENT


def test_beginner_on_tier_one_barbell_picks_fixed_increment():
    got = resolve_scheme(
        ctx(
            history=with_prescription(),
            fatigue_tier=1,
            equipment=("barbell",),
            experience_level="beginner",
        )
    )
    assert got == params.SCHEME_FIXED_INCREMENT


def test_advanced_on_tier_one_barbell_picks_double():
    got = resolve_scheme(
        ctx(
            history=with_prescription(),
            fatigue_tier=1,
            equipment=("barbell",),
            experience_level="advanced",
        )
    )
    assert got == params.SCHEME_DOUBLE


@pytest.mark.parametrize("tier", [2, 3])
def test_machines_and_isolation_pick_double(tier):
    got = resolve_scheme(ctx(history=with_prescription(), fatigue_tier=tier))
    assert got == params.SCHEME_DOUBLE


def test_override_for_reads_nested_settings():
    settings = {"progression": {"overrides": {"42": "double"}}}
    assert override_for(settings, 42) == "double"


def test_override_for_returns_none_when_absent():
    assert override_for({}, 42) is None
    assert override_for({"progression": {}}, 42) is None
    assert override_for(None, 42) is None
