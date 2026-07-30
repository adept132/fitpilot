"""Оркестратор с субъективным слоем (спека P0-07 §5.2)."""

from datetime import datetime

import pytest

from api.services.progression import params
from api.services.progression.engine import plan_exercise
from api.services.progression.types import (
    ExerciseHistory,
    Prescription,
    ProgressionState,
    SchemeContext,
    SessionFact,
    SetFact,
    SetPrescription,
)

BASE = datetime(2026, 1, 1)


def prev(weight=40.0, reps=12, rir=2, sets_count=1, logged=True) -> SessionFact:
    presc = Prescription(
        scheme="double",
        sets=tuple(
            SetPrescription(i, weight, 8, 12, 2, "normal")
            for i in range(1, sets_count + 1)
        ),
        reason_code="progressed",
        reason_text="x",
    )
    facts = (
        tuple(
            SetFact(i, weight, reps, rir, "normal", False)
            for i in range(1, sets_count + 1)
        )
        if logged
        else ()
    )
    return SessionFact(
        session_id=1, finished_at=BASE, prescription=presc, sets=facts
    )


def ctx(history_sessions=(), **kw) -> SchemeContext:
    base = dict(
        history=ExerciseHistory(exercise_id=1, sessions=tuple(history_sessions)),
        state=ProgressionState(),
        last_outcome=None,
        target_sets=4,
        rep_min=8,
        rep_max=12,
        rep_range_source=params.REP_SOURCE_FALLBACK,
        target_rir=2,
        equipment=("barbell",),
        unit="kg",
        days_since_last_session=3,
    )
    base.update(kw)
    return SchemeContext(**base)


def test_without_verdict_engine_behaves_as_before():
    plain = plan_exercise(ctx(history_sessions=[prev(sets_count=4)]))
    assert plain.volume_delta == 0
    assert plain.volume_reason_code is None
    assert len(plain.sets) == 4


def test_verdict_limit_holds_weight_and_trims_volume():
    result = plan_exercise(
        ctx(
            history_sessions=[prev(sets_count=4)],
            readiness_level="limit",
            readiness_source="pain",
        )
    )
    assert result.reason_code == "pain_hold"
    assert result.volume_reason_code == "pain_volume"
    assert len(result.sets) == 2


def test_verdict_caution_trims_volume_but_not_weight():
    plain = plan_exercise(ctx(history_sessions=[prev(sets_count=4)]))
    cautious = plan_exercise(
        ctx(
            history_sessions=[prev(sets_count=4)],
            readiness_level="caution",
            readiness_source="soreness",
        )
    )
    assert cautious.top_weight == pytest.approx(plain.top_weight)
    assert len(cautious.sets) == len(plain.sets) - 1
    assert cautious.volume_reason_code == "soreness_volume"


def test_engine_never_prescribes_more_with_a_verdict_than_without():
    """Следствие инварианта §7.4 на уровне всего движка."""
    history = [prev(sets_count=4)]
    plain = plan_exercise(ctx(history_sessions=history))
    for level, source in [
        ("caution", "global"),
        ("caution", "soreness"),
        ("limit", "pain"),
        ("limit", "soreness"),
        ("limit", "global"),
    ]:
        loaded = plan_exercise(
            ctx(
                history_sessions=history,
                readiness_level=level,
                readiness_source=source,
            )
        )
        assert loaded.top_weight <= plain.top_weight + 1e-9, (level, source)
        assert len(loaded.sets) <= len(plain.sets), (level, source)


def test_skipped_last_session_is_detected():
    # Упражнение было в сессии, но подходов не залогировано.
    result = plan_exercise(
        ctx(history_sessions=[prev(logged=False), prev()])
    )
    assert result.reason_code == "exercise_skipped"


def test_empty_history_does_not_look_like_a_skip():
    result = plan_exercise(ctx(history_sessions=[]))
    assert result.reason_code != "exercise_skipped"


def test_provisional_flag_still_works():
    result = plan_exercise(ctx(history_sessions=[prev()]), provisional=True)
    assert result.provisional is True
