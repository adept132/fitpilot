"""Поля объёма и готовности в типах движка (спека P0-07 §7.2, §7.3)."""

from api.services.progression import params
from api.services.progression.types import (
    ENGINE_VERSION,
    Prescription,
    SchemeContext,
    ExerciseHistory,
    ProgressionState,
    SetPrescription,
)


def _presc(**kw) -> Prescription:
    base = dict(
        scheme="double",
        sets=(SetPrescription(1, 40.0, 8, 12, 2, "normal"),),
        reason_code="progressed",
        reason_text="x",
    )
    base.update(kw)
    return Prescription(**base)


def _ctx(**kw) -> SchemeContext:
    base = dict(
        history=ExerciseHistory(exercise_id=1),
        state=ProgressionState(),
        last_outcome=None,
        target_sets=3,
        rep_min=8,
        rep_max=12,
        rep_range_source=params.REP_SOURCE_FALLBACK,
        target_rir=2,
    )
    base.update(kw)
    return SchemeContext(**base)


def test_engine_version_is_two():
    assert ENGINE_VERSION == 2


def test_volume_fields_default_to_untouched():
    p = _presc()
    assert p.volume_delta == 0
    assert p.volume_reason_code is None
    assert p.volume_reason_text is None


def test_volume_fields_survive_round_trip():
    p = _presc(
        volume_delta=-2,
        volume_reason_code="soreness_volume",
        volume_reason_text="Крепатура — убрали подходы.",
    )
    assert Prescription.from_dict(p.to_dict()) == p


def test_v1_payload_without_volume_fields_still_loads():
    # Записи, созданные движком версии 1, читаются без миграции.
    raw = {
        "scheme": "double",
        "sets": [{"set_number": 1, "weight_kg": 40.0, "rep_min": 8,
                  "rep_max": 12, "rir": 2, "kind": "normal"}],
        "reason_code": "progressed",
        "reason_text": "x",
        "basis": {},
        "engine_version": 1,
        "provisional": False,
    }
    restored = Prescription.from_dict(raw)
    assert restored.engine_version == 1
    assert restored.volume_delta == 0
    assert restored.volume_reason_code is None


def test_context_readiness_defaults_to_ok():
    ctx = _ctx()
    assert ctx.readiness_level == "ok"
    assert ctx.readiness_source is None
    assert ctx.last_session_skipped is False


def test_context_accepts_readiness():
    ctx = _ctx(readiness_level="limit", readiness_source="pain")
    assert ctx.readiness_level == "limit"
    assert ctx.readiness_source == "pain"


def test_strain_thresholds_are_named():
    assert params.STRAIN_MIN_PRESCRIBED_RIR == 2
    assert params.STRAIN_SET_RATIO == 0.5


def test_soft_layoff_is_below_hard_layoff():
    assert params.LAYOFF_SOFT_DAYS < params.LAYOFF_DAYS


def test_volume_trim_table_covers_both_non_ok_levels():
    assert params.VOLUME_TRIM_BY_LEVEL == {"caution": 1, "limit": 2}


def test_volume_min_sets_leaves_a_meaningful_exercise():
    assert params.VOLUME_MIN_SETS == 2


def test_every_volume_reason_has_text():
    assert set(params.VOLUME_REASON_TEXTS) == {
        "pain_volume", "soreness_volume", "readiness_volume",
    }
    assert all(t.strip() for t in params.VOLUME_REASON_TEXTS.values())
