"""Типы и константы пакета готовности (спека P0-07 §6)."""

import pytest

from api.services.readiness import params
from api.services.readiness.types import (
    CheckinSignals,
    ExerciseReadiness,
    ExerciseTarget,
    MuscleFlag,
    ReadinessVerdict,
)

# Ровно те 16 ключей, что отдаёт MUSCLE_TRANSLATION_MAP.
SYSTEM_KEYS = {
    "abductors", "abs", "adductors", "biceps", "calves", "chest",
    "front_delts", "glutes", "hamstrings", "lats", "mid_back", "quads",
    "rear_delts", "side_delts", "traps", "triceps",
}


def _sample() -> ReadinessVerdict:
    return ReadinessVerdict(
        level=params.LEVEL_CAUTION,
        reason_code="readiness_caution",
        reason_text="Сон и стресс так себе — сегодня поаккуратнее.",
        muscle_flags=(MuscleFlag("quads", params.LEVEL_LIMIT, "soreness_limit"),),
        pain_places=("knee",),
        completeness=params.COMPLETENESS_FULL,
        observed_at=None,
    )


def test_verdict_round_trip_preserves_everything():
    original = _sample()
    assert ReadinessVerdict.from_dict(original.to_dict()) == original


def test_verdict_to_dict_is_json_serializable():
    import json

    raw = json.dumps(_sample().to_dict())
    assert ReadinessVerdict.from_dict(json.loads(raw)) == _sample()


def test_verdict_is_immutable():
    with pytest.raises(Exception):
        _sample().level = params.LEVEL_OK


def test_levels_are_ordered():
    assert params.LEVEL_ORDER[params.LEVEL_OK] == 0
    assert params.LEVEL_ORDER[params.LEVEL_CAUTION] == 1
    assert params.LEVEL_ORDER[params.LEVEL_LIMIT] == 2


def test_downgrade_walks_one_step_down():
    assert params.downgrade(params.LEVEL_LIMIT) == params.LEVEL_CAUTION
    assert params.downgrade(params.LEVEL_CAUTION) == params.LEVEL_OK
    assert params.downgrade(params.LEVEL_OK) == params.LEVEL_OK


def test_scale_thresholds_are_named_not_magic():
    # Направления шкал разные: сон "выше = лучше", стресс "выше = хуже".
    assert params.SLEEP_BAD_AT_OR_BELOW == 2
    assert params.STRESS_BAD_AT_OR_ABOVE == 4
    assert params.SORENESS_CAUTION == 2
    assert params.SORENESS_LIMIT == 3


def test_joint_keys_are_disjoint_from_muscle_keys():
    # Мышц lower_back / forearms / neck в системе нет — они только суставы.
    assert params.JOINT_KEYS.isdisjoint(SYSTEM_KEYS)


def test_every_joint_has_impact_entry():
    assert set(params.JOINT_IMPACT) == set(params.JOINT_KEYS)


def test_joint_impact_muscles_are_real_system_keys():
    for joint, (muscles, _actions) in params.JOINT_IMPACT.items():
        assert muscles, joint
        assert muscles <= SYSTEM_KEYS, joint


def test_joint_impact_actions_are_real_exercise_actions():
    from api.services.exercise_pattern_tags import ExerciseAction

    known = {a.value for a in ExerciseAction}
    for joint, (_muscles, actions) in params.JOINT_IMPACT.items():
        assert actions, joint
        assert actions <= known, joint


def test_elbow_does_not_claim_lats_or_chest_as_prime_movers():
    # Иначе подтягивания получили бы limit вместо caution (спека §6.4).
    muscles, _ = params.JOINT_IMPACT["elbow"]
    assert muscles == {"biceps", "triceps"}


def test_checkin_signals_default_to_empty():
    s = CheckinSignals()
    assert s.sleep is None
    assert s.stress is None
    assert s.soreness == {}
    assert s.pain == {}


def test_exercise_target_holds_normalized_keys():
    t = ExerciseTarget(
        exercise_id=7, main_muscle="quads",
        secondary_muscles=("glutes",), action="squat",
    )
    assert t.main_muscle == "quads"
    assert t.secondary_muscles == ("glutes",)


def test_exercise_readiness_defaults_to_ok():
    r = ExerciseReadiness()
    assert r.level == params.LEVEL_OK
    assert r.source is None


def test_sources_are_the_three_expected():
    assert params.SOURCE_PAIN == "pain"
    assert params.SOURCE_SORENESS == "soreness"
    assert params.SOURCE_GLOBAL == "global"


def test_observation_kinds_cover_the_checkin():
    assert params.KIND_SLEEP == "sleep"
    assert params.KIND_STRESS == "stress"
    assert params.KIND_SORENESS == "soreness"
    assert params.KIND_PAIN == "pain"


def test_checkin_signals_soreness_is_truly_immutable():
    """Проверяет, что soreness защищён от мутации, а не только от переассоединения."""
    s = CheckinSignals(soreness={"quads": 2})
    with pytest.raises(TypeError):
        s.soreness["quads"] = 99


def test_checkin_signals_pain_is_truly_immutable():
    """Проверяет, что pain защищён от мутации, а не только от переассоединения."""
    s = CheckinSignals(pain={"knee": 1})
    with pytest.raises(TypeError):
        s.pain["knee"] = 3
