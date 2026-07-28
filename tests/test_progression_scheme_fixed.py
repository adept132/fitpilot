"""Фиксированная прибавка (спека P0-06 §6.3)."""

import pytest

from api.services.progression import params
from api.services.progression.rounding import round_down_to_step
from api.services.progression.schemes.fixed_increment import increment_for, plan
from api.services.progression.types import (
    ExerciseHistory,
    Prescription,
    ProgressionState,
    SchemeContext,
    SessionFact,
    SetFact,
    SetPrescription,
)


def last_session(weight, reps_per_set, rep_min=5) -> ExerciseHistory:
    presc = Prescription(
        scheme=params.SCHEME_FIXED_INCREMENT,
        sets=tuple(
            SetPrescription(i + 1, weight, rep_min, rep_min, 2, "normal")
            for i in range(len(reps_per_set))
        ),
        reason_code="progressed",
        reason_text="x",
    )
    facts = tuple(SetFact(i + 1, weight, r, 2) for i, r in enumerate(reps_per_set))
    return ExerciseHistory(exercise_id=1, sessions=(SessionFact(1, None, presc, facts),))


def ctx(history, **kw) -> SchemeContext:
    base = dict(
        history=history,
        state=ProgressionState(last_top_weight=60.0),
        last_outcome=None,
        target_sets=3,
        rep_min=5,
        rep_max=5,
        rep_range_source=params.REP_SOURCE_PLAN,
        target_rir=2,
        equipment=("barbell",),
        main_muscle_group="грудь",
        experience_level="beginner",
    )
    base.update(kw)
    return SchemeContext(**base)


def test_upper_body_increment_is_one_step():
    c = ctx(last_session(60.0, [5, 5, 5]), main_muscle_group="грудь")
    assert increment_for(c, 60.0) == pytest.approx(2.5)


def test_lower_body_increment_is_two_steps():
    c = ctx(last_session(100.0, [5, 5, 5]), main_muscle_group="квадрицепсы")
    assert increment_for(c, 100.0) == pytest.approx(5.0)


def test_unknown_muscle_is_treated_as_upper_body():
    c = ctx(last_session(60.0, [5, 5, 5]), main_muscle_group="кибернетика")
    assert increment_for(c, 60.0) == pytest.approx(2.5)


def test_relative_cap_protects_light_weights():
    # 20 кг присед: два шага = +5 кг = +25 %. Потолок 5 % режет до одного шага.
    c = ctx(last_session(20.0, [5, 5, 5]), main_muscle_group="квадрицепсы")
    assert increment_for(c, 20.0) == pytest.approx(2.5)


def test_cap_never_goes_below_one_step():
    c = ctx(last_session(5.0, [5, 5, 5]), main_muscle_group="квадрицепсы")
    assert increment_for(c, 5.0) == pytest.approx(2.5)


def test_full_completion_advances_the_weight():
    p = plan(ctx(last_session(60.0, [5, 5, 5])))
    assert all(s.weight_kg == pytest.approx(62.5) for s in p.sets)
    assert p.reason_code == "progressed"


def test_missed_reps_hold_the_weight():
    p = plan(ctx(last_session(60.0, [5, 5, 4])))
    assert all(s.weight_kg == pytest.approx(60.0) for s in p.sets)


def test_reps_are_fixed_at_rep_min():
    p = plan(ctx(last_session(60.0, [5, 5, 5])))
    assert all(s.rep_min == 5 and s.rep_max == 5 for s in p.sets)


def test_produces_one_prescription_per_target_set():
    p = plan(ctx(last_session(60.0, [5, 5, 5]), target_sets=5))
    assert [s.set_number for s in p.sets] == [1, 2, 3, 4, 5]


def test_no_anchor_gives_no_basis():
    p = plan(ctx(ExerciseHistory(exercise_id=1), state=ProgressionState()))
    assert p.reason_code == "no_basis"
    assert p.sets == ()


def test_scheme_never_lowers_the_weight():
    p = plan(ctx(last_session(60.0, [1, 1, 1])))
    assert all(s.weight_kg >= 60.0 for s in p.sets)


def test_floor_rounding_never_stalls_progression():
    """Известная ловушка: округление вниз к шагу оборудования может съесть
    прибавку целиком, если анкор лежит точно на границе шага.

    Блочный тренажёр всегда шагает по 10 lb независимо от unit. При анкоре
    40.82 кг (кг-округление логированного веса) инкремент 4.5359 кг (кг-
    эквивалент 10 lb, округлённый до 4 знаков) в сумме даёт 45.3559 кг, что
    при переводе в lb и округлении вниз к шагу 10 lb (89.999... -> floor 80)
    возвращается в те же 40.82 кг — прогрессия молча встаёт навсегда без
    защиты от этого случая.
    """
    c = ctx(
        last_session(40.82, [5, 5, 5]),
        equipment=("block_machine",),
        main_muscle_group="грудь",
    )
    p = plan(c)
    assert all(s.weight_kg > 40.82 for s in p.sets)


@pytest.mark.parametrize("anchor", [12.5, 20.0, 40.0, 77.5, 140.0])
def test_emergency_branch_output_is_grid_fixed_point(anchor):
    """Находка №2 ревью Задачи 9: аварийная ветка _advance() раньше выдавала
    сырое anchor + step в обход канонического округления, и результат не был
    неподвижной точкой round_down_to_step (вне сетки оборудования).

    Эти пять анкоров (12.5, 20, 40, 77.5, 140 — набор Задачи 12) на блочном
    тренажёре раньше все попадали в аварийную ветку. Результат обязан быть
    строго больше анкора и лежать на сетке.
    """
    c = ctx(
        last_session(anchor, [5, 5, 5]),
        equipment=("block_machine",),
        main_muscle_group="грудь",
    )
    p = plan(c)
    for s in p.sets:
        assert s.weight_kg > anchor
        assert round_down_to_step(s.weight_kg, ["block_machine"], "kg", None) == pytest.approx(
            s.weight_kg
        )
