"""Бутстрап-схема: поведение обязано совпадать с нынешним движком."""

import pytest

from api.services.progression import params
from api.services.progression.metrics import e1rm, weight_for_e1rm
from api.services.progression.rounding import round_to_step
from api.services.progression.schemes import SCHEMES, plan_with
from api.services.progression.types import (
    ExerciseHistory,
    ProgressionState,
    SchemeContext,
)


def ctx(**kw) -> SchemeContext:
    base = dict(
        history=ExerciseHistory(exercise_id=1),
        state=ProgressionState(working_e1rm=55.0, last_top_weight=40.0),
        last_outcome=None,
        target_sets=3,
        rep_min=8,
        rep_max=12,
        rep_range_source=params.REP_SOURCE_FALLBACK,
        target_rir=2,
        equipment=("barbell",),
        experience_level="intermediate",
    )
    base.update(kw)
    return SchemeContext(**base)


@pytest.mark.xfail(reason="схемы double/fixed/percent появятся в задачах 8-10")
def test_registry_contains_all_four_schemes():
    assert set(SCHEMES) == {
        params.SCHEME_E1RM_FACTOR,
        params.SCHEME_DOUBLE,
        params.SCHEME_FIXED_INCREMENT,
        params.SCHEME_PERCENT_1RM,
    }


def test_plan_with_unknown_scheme_raises():
    with pytest.raises(KeyError):
        plan_with("astrology", ctx())


def test_no_working_e1rm_gives_no_basis():
    p = plan_with(params.SCHEME_E1RM_FACTOR, ctx(state=ProgressionState()))
    assert p.reason_code == "no_basis"
    assert p.sets == ()


def test_produces_one_prescription_per_target_set():
    p = plan_with(params.SCHEME_E1RM_FACTOR, ctx(target_sets=4))
    assert [s.set_number for s in p.sets] == [1, 2, 3, 4]


def test_all_sets_share_weight_and_reps():
    p = plan_with(params.SCHEME_E1RM_FACTOR, ctx())
    assert len({s.weight_kg for s in p.sets}) == 1
    assert len({s.rep_min for s in p.sets}) == 1


def test_target_matches_legacy_selection_rule():
    # Ту же пару (вес, повторы) выбирает нынешний compute_autoprogression:
    # минимальное отклонение нового e1RM от base * factor.
    c = ctx()
    mod = 55.0 * 1.02  # intermediate
    best = None
    for r in range(c.rep_min, c.rep_max + 1):
        w = round_to_step(weight_for_e1rm(mod, r, c.target_rir), c.equipment, "kg", None)
        diff = abs(e1rm(w, r, c.target_rir) - mod)
        if best is None or diff < best[0]:
            best = (diff, w, r)
    p = plan_with(params.SCHEME_E1RM_FACTOR, c)
    assert p.sets[0].weight_kg == pytest.approx(best[1])
    assert p.sets[0].rep_min == best[2]


def test_reason_code_is_bootstrap():
    p = plan_with(params.SCHEME_E1RM_FACTOR, ctx())
    assert p.reason_code == "bootstrap_no_prescription"
    assert p.reason_text == params.REASON_TEXTS["bootstrap_no_prescription"]


def test_basis_records_the_source_e1rm():
    p = plan_with(params.SCHEME_E1RM_FACTOR, ctx())
    assert p.basis["e1rm"] == pytest.approx(55.0)


def test_settings_factor_overrides_experience_level():
    from api.services.progression.schemes.e1rm_factor import progression_factor_for

    assert progression_factor_for("advanced", {"progression_factor": 1.05}) == 1.05
    assert progression_factor_for("advanced", None) == 1.01
    assert progression_factor_for("beginner", None) == 1.03
    assert progression_factor_for(None, None) == 1.03
    assert progression_factor_for("beginner", {"progression_factor": "junk"}) == 1.03
