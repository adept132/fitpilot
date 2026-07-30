"""Вердикт готовности и его резолв на упражнение (спека P0-07 §6.5)."""

import pytest

from api.services.readiness import params
from api.services.readiness.types import CheckinSignals, ExerciseTarget
from api.services.readiness.verdict import build_verdict, level_for_exercise


def squat() -> ExerciseTarget:
    return ExerciseTarget(1, "quads", ("glutes", "hamstrings"), "squat")


def bench() -> ExerciseTarget:
    return ExerciseTarget(2, "chest", ("triceps", "front_delts"), "push")


def pullup() -> ExerciseTarget:
    return ExerciseTarget(3, "lats", ("biceps", "mid_back"), "pull")


def curl() -> ExerciseTarget:
    return ExerciseTarget(4, "biceps", (), "flexion")


# --- build_verdict ---


def test_empty_signals_give_no_verdict():
    assert build_verdict(CheckinSignals()) is None


def test_one_bad_signal_is_caution():
    v = build_verdict(CheckinSignals(sleep=2, stress=1))
    assert v.level == params.LEVEL_CAUTION
    assert v.reason_code == "readiness_caution"


def test_both_bad_signals_are_limit():
    v = build_verdict(CheckinSignals(sleep=1, stress=5))
    assert v.level == params.LEVEL_LIMIT
    assert v.reason_code == "readiness_limit"


def test_good_signals_are_ok():
    v = build_verdict(CheckinSignals(sleep=5, stress=1))
    assert v.level == params.LEVEL_OK
    assert v.reason_code == "readiness_ok"


def test_every_verdict_has_non_empty_reason_text():
    for signals in [
        CheckinSignals(sleep=5, stress=1),
        CheckinSignals(sleep=2, stress=1),
        CheckinSignals(sleep=1, stress=5),
    ]:
        assert build_verdict(signals).reason_text.strip()


def test_soreness_becomes_muscle_flags():
    v = build_verdict(CheckinSignals(sleep=5, stress=1, soreness={"quads": 3, "chest": 2}))
    by_muscle = {f.muscle: f for f in v.muscle_flags}
    assert by_muscle["quads"].level == params.LEVEL_LIMIT
    assert by_muscle["quads"].reason_code == "soreness_limit"
    assert by_muscle["chest"].level == params.LEVEL_CAUTION
    assert by_muscle["chest"].reason_code == "soreness_caution"


def test_soreness_below_threshold_produces_no_flag():
    v = build_verdict(CheckinSignals(sleep=5, stress=1, soreness={"quads": 1}))
    assert v.muscle_flags == ()


def test_zero_pain_is_not_an_active_place():
    v = build_verdict(CheckinSignals(sleep=5, stress=1, pain={"knee": 0, "elbow": 2}))
    assert v.pain_places == ("elbow",)


def test_completeness_is_full_only_when_both_scales_answered():
    assert build_verdict(CheckinSignals(sleep=4, stress=2)).completeness == params.COMPLETENESS_FULL
    assert build_verdict(CheckinSignals(sleep=4)).completeness == params.COMPLETENESS_PARTIAL
    partial = build_verdict(CheckinSignals(soreness={"quads": 3}))
    assert partial.completeness == params.COMPLETENESS_PARTIAL


def test_soreness_only_still_gives_a_verdict():
    v = build_verdict(CheckinSignals(soreness={"quads": 3}))
    assert v is not None
    assert v.level == params.LEVEL_OK  # глобально всё в порядке


# --- level_for_exercise ---


def test_no_verdict_means_ok():
    r = level_for_exercise(None, squat())
    assert r.level == params.LEVEL_OK
    assert r.source is None


def test_global_level_applies_to_every_exercise():
    v = build_verdict(CheckinSignals(sleep=1, stress=5))
    for target in (squat(), bench(), pullup()):
        r = level_for_exercise(v, target)
        assert r.level == params.LEVEL_LIMIT
        assert r.source == params.SOURCE_GLOBAL


def test_soreness_on_main_muscle_applies_in_full():
    v = build_verdict(CheckinSignals(sleep=5, stress=1, soreness={"quads": 3}))
    r = level_for_exercise(v, squat())
    assert r.level == params.LEVEL_LIMIT
    assert r.source == params.SOURCE_SORENESS


def test_soreness_on_secondary_muscle_is_downgraded():
    v = build_verdict(CheckinSignals(sleep=5, stress=1, soreness={"glutes": 3}))
    r = level_for_exercise(v, squat())
    assert r.level == params.LEVEL_CAUTION
    assert r.source == params.SOURCE_SORENESS


def test_soreness_on_unrelated_muscle_does_nothing():
    v = build_verdict(CheckinSignals(sleep=5, stress=1, soreness={"chest": 3}))
    assert level_for_exercise(v, squat()).level == params.LEVEL_OK


def test_knee_pain_limits_the_squat():
    v = build_verdict(CheckinSignals(sleep=5, stress=1, pain={"knee": 2}))
    r = level_for_exercise(v, squat())
    assert r.level == params.LEVEL_LIMIT
    assert r.source == params.SOURCE_PAIN


def test_elbow_pain_limits_the_curl():
    v = build_verdict(CheckinSignals(sleep=5, stress=1, pain={"elbow": 2}))
    r = level_for_exercise(v, curl())
    assert r.level == params.LEVEL_LIMIT
    assert r.source == params.SOURCE_PAIN


def test_elbow_pain_only_cautions_the_pullup():
    # biceps там синергист, а не целевая мышца (спека §6.4).
    v = build_verdict(CheckinSignals(sleep=5, stress=1, pain={"elbow": 2}))
    r = level_for_exercise(v, pullup())
    assert r.level == params.LEVEL_CAUTION
    assert r.source == params.SOURCE_PAIN


def test_elbow_pain_does_not_touch_the_squat():
    v = build_verdict(CheckinSignals(sleep=5, stress=1, pain={"elbow": 2}))
    assert level_for_exercise(v, squat()).level == params.LEVEL_OK


def test_joint_pain_requires_the_movement_pattern_too():
    # quads входят в мышцы колена, но разгибание сидя с паттерном "extension"
    # попадает, а вот "carry" — нет: конъюнкция мышцы и паттерна.
    v = build_verdict(CheckinSignals(sleep=5, stress=1, pain={"knee": 2}))
    carry = ExerciseTarget(9, "quads", (), "carry")
    assert level_for_exercise(v, carry).level == params.LEVEL_OK


def test_unknown_action_drops_the_pattern_requirement():
    # Ошибаемся в сторону осторожности: паттерн неизвестен -> решаем по мышце.
    v = build_verdict(CheckinSignals(sleep=5, stress=1, pain={"knee": 2}))
    unknown = ExerciseTarget(10, "quads", (), "unknown")
    r = level_for_exercise(v, unknown)
    assert r.level == params.LEVEL_LIMIT
    assert r.source == params.SOURCE_PAIN


def test_muscle_pain_needs_no_action_match():
    v = build_verdict(CheckinSignals(sleep=5, stress=1, pain={"chest": 2}))
    r = level_for_exercise(v, bench())
    assert r.level == params.LEVEL_LIMIT
    assert r.source == params.SOURCE_PAIN


def test_pain_wins_the_tie_against_soreness():
    # Оба дают limit; источником должна остаться боль — она информативнее.
    v = build_verdict(
        CheckinSignals(sleep=5, stress=1, soreness={"quads": 3}, pain={"knee": 2})
    )
    assert level_for_exercise(v, squat()).source == params.SOURCE_PAIN


def test_stronger_soreness_beats_weaker_pain():
    # Крепатура limit на целевой мышце против боли caution через синергиста.
    v = build_verdict(
        CheckinSignals(sleep=5, stress=1, soreness={"lats": 3}, pain={"elbow": 2})
    )
    r = level_for_exercise(v, pullup())
    assert r.level == params.LEVEL_LIMIT
    assert r.source == params.SOURCE_SORENESS


def test_exercise_without_muscle_data_falls_back_to_global():
    v = build_verdict(CheckinSignals(sleep=5, stress=1, soreness={"quads": 3}))
    blank = ExerciseTarget(11, None, (), "unknown")
    assert level_for_exercise(v, blank).level == params.LEVEL_OK
