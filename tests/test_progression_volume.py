"""Подрезка объёма по вердикту готовности (спека P0-07 §7.3)."""

import pytest

from api.services.progression import params
from api.services.progression.types import (
    ExerciseHistory,
    Prescription,
    ProgressionState,
    SchemeContext,
    SetPrescription,
)
from api.services.progression.volume import apply_volume_trim


def presc(n=4, amrap_last=False) -> Prescription:
    sets = []
    for i in range(1, n + 1):
        kind = "amrap" if (amrap_last and i == n) else "normal"
        rep_max = None if kind == "amrap" else 12
        sets.append(SetPrescription(i, 40.0, 8, rep_max, 2, kind))
    return Prescription(
        scheme="double",
        sets=tuple(sets),
        reason_code="progressed",
        reason_text="x",
    )


def ctx(**kw) -> SchemeContext:
    base = dict(
        history=ExerciseHistory(exercise_id=1),
        state=ProgressionState(),
        last_outcome=None,
        target_sets=4,
        rep_min=8,
        rep_max=12,
        rep_range_source=params.REP_SOURCE_FALLBACK,
        target_rir=2,
    )
    base.update(kw)
    return SchemeContext(**base)


def test_no_verdict_is_identity():
    p = presc()
    assert apply_volume_trim(p, ctx()) is p


def test_caution_removes_one_set():
    result = apply_volume_trim(
        presc(), ctx(readiness_level="caution", readiness_source="soreness")
    )
    assert len(result.sets) == 3
    assert result.volume_delta == -1


def test_limit_removes_two_sets():
    result = apply_volume_trim(
        presc(), ctx(readiness_level="limit", readiness_source="pain")
    )
    assert len(result.sets) == 2
    assert result.volume_delta == -2


def test_floor_stops_the_trim():
    # Три подхода, limit хочет снять два — пол оставляет два.
    result = apply_volume_trim(
        presc(n=3), ctx(readiness_level="limit", readiness_source="pain")
    )
    assert len(result.sets) == params.VOLUME_MIN_SETS
    assert result.volume_delta == -1


def test_at_the_floor_nothing_is_trimmed():
    p = presc(n=2)
    result = apply_volume_trim(
        p, ctx(readiness_level="limit", readiness_source="pain")
    )
    assert result is p


def test_amrap_is_never_removed():
    # У percent_1rm AMRAP — единственный вход, обновляющий working_e1rm.
    # Срезать его значит обезглавить схему.
    result = apply_volume_trim(
        presc(n=4, amrap_last=True),
        ctx(readiness_level="limit", readiness_source="pain"),
    )
    assert len(result.sets) == 2
    assert result.sets[-1].kind == "amrap"


def test_trim_takes_sets_from_the_end():
    result = apply_volume_trim(
        presc(), ctx(readiness_level="caution", readiness_source="soreness")
    )
    assert [s.weight_kg for s in result.sets] == [40.0, 40.0, 40.0]
    assert result.sets[-1].kind == "normal"


def test_set_numbers_are_renumbered_from_one():
    # evaluate сопоставляет факт с предписанием по set_number — дыра
    # в нумерации сломала бы оценку следующей сессии.
    result = apply_volume_trim(
        presc(n=4, amrap_last=True),
        ctx(readiness_level="limit", readiness_source="pain"),
    )
    assert [s.set_number for s in result.sets] == [1, 2]


def test_reason_reflects_the_source():
    for source, code in (
        ("pain", "pain_volume"),
        ("soreness", "soreness_volume"),
        ("global", "readiness_volume"),
    ):
        result = apply_volume_trim(
            presc(), ctx(readiness_level="limit", readiness_source=source)
        )
        assert result.volume_reason_code == code
        assert result.volume_reason_text == params.VOLUME_REASON_TEXTS[code]


def test_weight_reason_is_not_overwritten():
    p = presc()
    p = Prescription(
        scheme=p.scheme,
        sets=p.sets,
        reason_code="plateau_reset",
        reason_text="плато",
    )
    result = apply_volume_trim(
        p, ctx(readiness_level="limit", readiness_source="pain")
    )
    assert result.reason_code == "plateau_reset"
    assert result.volume_reason_code == "pain_volume"


def test_all_amrap_prescription_is_untouched():
    p = Prescription(
        scheme="percent_1rm",
        sets=(
            SetPrescription(1, 90.0, 1, None, 0, "amrap"),
            SetPrescription(2, 90.0, 1, None, 0, "amrap"),
            SetPrescription(3, 90.0, 1, None, 0, "amrap"),
        ),
        reason_code="progressed",
        reason_text="x",
    )
    result = apply_volume_trim(
        p, ctx(readiness_level="limit", readiness_source="pain")
    )
    assert result is p


def test_empty_prescription_is_untouched():
    p = Prescription(
        scheme="double", sets=(), reason_code="no_basis", reason_text="x"
    )
    assert apply_volume_trim(
        p, ctx(readiness_level="limit", readiness_source="pain")
    ) is p


@pytest.mark.parametrize("level", ["ok", "caution", "limit"])
@pytest.mark.parametrize("source", [None, "pain", "soreness", "global"])
@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 8])
@pytest.mark.parametrize("amrap_last", [False, True])
def test_property_trim_only_subtracts(level, source, n, amrap_last):
    """Трим никогда не увеличивает число подходов и не трогает AMRAP."""
    p = presc(n=n, amrap_last=amrap_last)
    result = apply_volume_trim(
        p, ctx(readiness_level=level, readiness_source=source)
    )
    assert len(result.sets) <= len(p.sets)
    assert result.volume_delta <= 0
    before = sum(1 for s in p.sets if s.kind == "amrap")
    after = sum(1 for s in result.sets if s.kind == "amrap")
    assert after == before


@pytest.mark.parametrize("level", ["caution", "limit"])
@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 8])
def test_property_floor_is_respected(level, n):
    """После трима подходов либо >= VOLUME_MIN_SETS, либо трима не было."""
    p = presc(n=n)
    result = apply_volume_trim(
        p, ctx(readiness_level=level, readiness_source="pain")
    )
    assert len(result.sets) >= params.VOLUME_MIN_SETS or result is p
