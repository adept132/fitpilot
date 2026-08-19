"""Прокрутка движка прогрессии вперёд: обе даты, потолок (P0-12, Задача 1)."""
from datetime import date, datetime, timedelta

from api.services.goal import params, simulate
from api.services.goal.types import FutureSession
from api.services.progression.types import (
    ExerciseHistory,
    Prescription,
    ProgressionState,
    SchemeContext,
    SessionFact,
    SetFact,
    SetPrescription,
)


def _ctx(working_e1rm: float = 100.0, scheme_hint: str = "double") -> SchemeContext:
    """Контекст «штанга, средний уровень, 5–8 повторов при RIR 2».

    ВАЖНАЯ НАХОДКА: с пустой history() движок в бутстрапе выбирает схему
    e1rm_factor (resolve_scheme: `_has_prescription_history` пуста ->
    e1rm_factor), а её plan() требует ctx.state.working_e1rm — а
    plan_exercise() ВСЕГДА пересчитывает state через
    rebuild_state(ctx.history, ...), отбрасывая state, переданный сюда
    вызывающей стороной. rebuild_state() пустой истории даёт working_e1rm =
    None -> схема возвращает no_basis (sets=()) НАВСЕГДА, вес не сдвигается
    ни на одной из будущих сессий. Поэтому история засеяна ОДНОЙ
    синтетической сессией: предписание на working_e1rm кг, все подходы на
    потолке диапазона (8 повторов). Это даёт resolve_scheme настоящую
    историю предписаний (схема "double", а не вечный бутстрап) и якорь
    веса, от которого прокрутка растёт по-настоящему.
    """
    seed_prescription = Prescription(
        scheme=scheme_hint,
        sets=tuple(
            SetPrescription(n, working_e1rm, 5, 8, 2, "normal") for n in range(1, 4)
        ),
        reason_code="progressed",
        reason_text="",
    )
    seed_facts = tuple(
        SetFact(set_number=n, weight_kg=working_e1rm, reps=8, rir=2) for n in range(1, 4)
    )
    seed_history = ExerciseHistory(
        exercise_id=51,
        sessions=(
            SessionFact(
                session_id=-1,
                finished_at=datetime(2026, 2, 27),
                prescription=seed_prescription,
                sets=seed_facts,
            ),
        ),
    )
    return SchemeContext(
        history=seed_history,
        state=ProgressionState(working_e1rm=working_e1rm, last_scheme=scheme_hint),
        last_outcome=None,
        target_sets=3,
        rep_min=5,
        rep_max=8,
        rep_range_source="fallback",
        target_rir=2,
        equipment=("barbell",),
        experience_level="intermediate",
        fatigue_tier=1,
        main_muscle_group="chest",
    )


def _sessions(n: int, start: date = date(2026, 3, 2), every_days: int = 3) -> list[FutureSession]:
    return [
        FutureSession(
            date=start + timedelta(days=i * every_days),
            phase_effort_tier="medium",
            prescription_sets=3,
        )
        for i in range(n)
    ]


def test_accumulates_until_target_and_returns_date():
    result = simulate.run(
        _ctx(), target_e1rm=110.0, sessions=_sessions(40), cap_pct=0.01, factor=None
    )
    assert result.nominal_date is not None
    assert result.nominal_date > date(2026, 3, 2)
    assert result.nominal_slope > 0
    assert 0 < result.sessions_used <= 40


def test_unreachable_target_returns_no_date():
    result = simulate.run(
        _ctx(), target_e1rm=400.0, sessions=_sessions(10), cap_pct=0.01, factor=None
    )
    assert result.nominal_date is None


def test_growth_is_capped_by_biology():
    """Схема прибавляет вес каждую сессию; потолок — 0.3 %/нед от 100 кг."""
    fast = simulate.run(
        _ctx(), target_e1rm=130.0, sessions=_sessions(60), cap_pct=0.003, factor=None
    )
    assert fast.nominal_slope <= 100.0 * 0.003 + 1e-9


def test_calibration_slows_the_pace_not_the_date_directly():
    base = simulate.run(
        _ctx(), target_e1rm=110.0, sessions=_sessions(60), cap_pct=0.01, factor=None
    )
    slow = simulate.run(
        _ctx(), target_e1rm=110.0, sessions=_sessions(60), cap_pct=0.01, factor=0.5
    )
    assert slow.calibration_available is True
    assert slow.plan_slope < base.nominal_slope
    assert slow.calibrated_date > base.nominal_date


def test_without_factor_second_date_is_absent():
    result = simulate.run(
        _ctx(), target_e1rm=110.0, sessions=_sessions(40), cap_pct=0.01, factor=None
    )
    assert result.calibration_available is False
    assert result.calibrated_date is None
    assert result.factor is None


def test_milestones_are_weekly_and_monotonic():
    result = simulate.run(
        _ctx(), target_e1rm=115.0, sessions=_sessions(40), cap_pct=0.01, factor=None
    )
    values = [m.expected_e1rm for m in result.milestones]
    assert len(values) >= 2
    assert values == sorted(values)
    assert all(
        result.milestones[i + 1].week_start - result.milestones[i].week_start == timedelta(days=7)
        for i in range(len(result.milestones) - 1)
    )


def test_no_future_sessions_gives_zero_slope():
    result = simulate.run(_ctx(), target_e1rm=110.0, sessions=[], cap_pct=0.01, factor=None)
    assert result.nominal_date is None
    assert result.nominal_slope == 0.0
    assert result.sessions_used == 0


def test_target_already_reached_returns_first_day():
    result = simulate.run(
        _ctx(working_e1rm=130.0), target_e1rm=110.0,
        sessions=_sessions(10), cap_pct=0.01, factor=None,
    )
    assert result.nominal_date == date(2026, 3, 2)


def test_session_cap_guards_runaway_horizon():
    result = simulate.run(
        _ctx(), target_e1rm=300.0,
        sessions=_sessions(params.MAX_SIMULATED_SESSIONS + 50),
        cap_pct=0.01, factor=None,
    )
    assert result.sessions_used <= params.MAX_SIMULATED_SESSIONS


def test_history_keep_matches_progression_history_limit():
    """Deferred Minor 1 (финальное ревью): simulate._HISTORY_KEEP = 12
    дублирует progression.repository.HISTORY_LIMIT литералом, потому что
    импорт сломал бы правило "чистое ядро без БД" (см. докстринг simulate.py
    про _HISTORY_KEEP). Это ЕДИНСТВЕННОЕ место в проекте, которому разрешено
    сравнить их напрямую — тест, а не модуль ядра, может импортировать
    БД-слой. Расхождение станет красным тестом, а не тихим дрейфом двух
    чисел, которые обязаны совпадать (rebuild_state держит ровно столько же
    сессий, сколько simulate.run проносит между итерациями прокрутки)."""
    from api.services.progression.repository import HISTORY_LIMIT

    assert simulate._HISTORY_KEEP == HISTORY_LIMIT
