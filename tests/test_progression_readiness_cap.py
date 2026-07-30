"""Субъективный потолок веса (спека P0-07 §7.2, §7.4)."""

import pytest

from api.services.progression import params
from api.services.progression.reduction import apply_readiness_cap
from api.services.progression.types import (
    ExerciseHistory,
    Prescription,
    ProgressionState,
    SchemeContext,
    SetPrescription,
)

ANCHOR = 40.0


def presc(weight=45.0, reason="progressed") -> Prescription:
    return Prescription(
        scheme="double",
        sets=(
            SetPrescription(1, weight, 8, 12, 2, "normal"),
            SetPrescription(2, weight, 8, 12, 2, "normal"),
        ),
        reason_code=reason,
        reason_text="x",
    )


def ctx(**kw) -> SchemeContext:
    base = dict(
        history=ExerciseHistory(exercise_id=1),
        state=ProgressionState(last_top_weight=ANCHOR),
        last_outcome=None,
        target_sets=2,
        rep_min=8,
        rep_max=12,
        rep_range_source=params.REP_SOURCE_FALLBACK,
        target_rir=2,
        equipment=("barbell",),
        unit="kg",
    )
    base.update(kw)
    return SchemeContext(**base)


def test_no_verdict_is_identity():
    p = presc()
    assert apply_readiness_cap(p, ctx()) is p


def test_caution_does_not_touch_the_weight():
    # Заморозить прогрессию из-за одной плохой ночи несоразмерно.
    p = presc()
    assert apply_readiness_cap(
        p, ctx(readiness_level="caution", readiness_source="global")
    ) is p


def test_pain_limit_caps_at_the_previous_weight():
    result = apply_readiness_cap(
        presc(), ctx(readiness_level="limit", readiness_source="pain")
    )
    assert result.reason_code == "pain_hold"
    assert result.top_weight == pytest.approx(ANCHOR)


def test_soreness_limit_has_its_own_reason():
    result = apply_readiness_cap(
        presc(), ctx(readiness_level="limit", readiness_source="soreness")
    )
    assert result.reason_code == "soreness_limit"


def test_global_limit_has_its_own_reason():
    result = apply_readiness_cap(
        presc(), ctx(readiness_level="limit", readiness_source="global")
    )
    assert result.reason_code == "readiness_limit"


def test_cap_never_raises_an_already_reduced_weight():
    # ЭТО ГЛАВНЫЙ ТЕСТ ЗАДАЧИ. Объективное правило увело вес до 36; боль
    # обязана оставить его там, а не поднять обратно к якорю 40.
    reduced = presc(weight=36.0, reason="plateau_reset")
    result = apply_readiness_cap(
        reduced, ctx(readiness_level="limit", readiness_source="pain")
    )
    assert result is reduced
    assert result.reason_code == "plateau_reset"


def test_cap_binds_only_when_the_scheme_wanted_more():
    grown = presc(weight=45.0)
    result = apply_readiness_cap(
        grown, ctx(readiness_level="limit", readiness_source="pain")
    )
    assert result.top_weight < grown.top_weight


def test_cap_lands_on_the_equipment_grid():
    result = apply_readiness_cap(
        presc(),
        ctx(
            readiness_level="limit",
            readiness_source="pain",
            state=ProgressionState(last_top_weight=41.3),
        ),
    )
    assert result.top_weight == pytest.approx(42.5)


def test_without_an_anchor_the_weight_is_left_alone():
    # Выдумывать вес нельзя. Объём при этом всё равно урежется (Task 8).
    p = presc()
    assert apply_readiness_cap(
        p,
        ctx(
            readiness_level="limit",
            readiness_source="pain",
            state=ProgressionState(),
        ),
    ) is p


def test_bodyweight_prescription_is_left_alone():
    # weight_kg=None — вес не предписан, ограничивать нечего.
    p = Prescription(
        scheme="double",
        sets=(SetPrescription(1, None, 8, 12, 2, "normal"),),
        reason_code="progressed",
        reason_text="x",
    )
    assert apply_readiness_cap(
        p, ctx(readiness_level="limit", readiness_source="pain")
    ) is p


def test_empty_prescription_is_left_alone():
    p = Prescription(
        scheme="double", sets=(), reason_code="no_basis", reason_text="x"
    )
    assert apply_readiness_cap(
        p, ctx(readiness_level="limit", readiness_source="pain")
    ) is p


@pytest.mark.parametrize("level", ["ok", "caution", "limit"])
@pytest.mark.parametrize("source", [None, "pain", "soreness", "global"])
@pytest.mark.parametrize("scheme_weight", [30.0, 40.0, 45.0, 100.0])
def test_property_cap_never_increases_weight(level, source, scheme_weight):
    """Строгий инвариант §7.4: субъективный слой умеет только убавлять."""
    p = presc(weight=scheme_weight)
    result = apply_readiness_cap(
        p, ctx(readiness_level=level, readiness_source=source)
    )
    assert result.top_weight <= p.top_weight + 1e-9
