"""Прокрутка движка прогрессии вперёд по будущим сессиям лифта (P0-12 §5.3).

Чистый модуль: ни БД, ни ORM. Прокручивается САМ движок P0-06 — схема
отвечает на вопрос «какой вес будет следующим, если эту сессию выполнить
как назначено», и этот ответ применяется многократно, до пересечения цели.
Формула «шаг × частота» здесь была бы ровно тем выдуманным коэффициентом,
который спека отвергает (решение 6).

Синтетический факт — выполнение по ВЕРХНЕЙ границе диапазона: при double
progression выполнение по нижней не двигает вес никогда, и симуляция
отвечала бы на вопрос «а если не расти».

НАХОДКА (расхождение с заготовкой брифа, см. отчёт Задачи 1): plan_exercise()
ВСЕГДА пересчитывает SchemeContext.state через
progression.state.rebuild_state(ctx.history, ...) и last_outcome через
_latest_outcome(ctx, ...) — то, что мы бы вручную положили в ctx.state и
ctx.last_outcome перед вызовом, движок просто отбрасывает и пересчитывает
заново из ctx.history. Поэтому единственное, что имеет смысл протаскивать
между итерациями цикла — это history: она и есть источник истины, из
которого движок сам восстанавливает state и last_outcome.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from typing import Optional

from api.services.goal import params
from api.services.goal.types import FutureSession, Milestone, Simulation
from api.services.progression.engine import plan_exercise
from api.services.progression.types import (
    ExerciseHistory,
    SchemeContext,
    SessionFact,
    SetFact,
)

_WEEK = 7.0
_HISTORY_KEEP = 12          # столько же сессий, сколько держит rebuild_state


def _e1rm(weight: float, reps: int) -> float:
    """Эпли без RIR — та же формула, что в goal_service и в графике."""
    return weight * (1.0 + reps / 30.0) if reps > 0 else weight


def _synthetic_facts(prescription) -> tuple[SetFact, ...]:
    """Предписание выполнено по верхней границе диапазона при заданном RIR."""
    return tuple(
        SetFact(
            set_number=sp.set_number,
            weight_kg=sp.weight_kg,
            reps=sp.rep_max or sp.rep_min,
            rir=sp.rir,
        )
        for sp in prescription.sets
    )


def run(
    ctx: SchemeContext,
    target_e1rm: float,
    sessions: list[FutureSession],
    cap_pct: float,
    factor: Optional[float],
) -> Simulation:
    """Дата пересечения цели, темп и вехи.

    cap_pct — доля текущего e1RM в неделю, биологический потолок роста
    (WEEKLY_GROWTH_CAP_PCT из forecast_service): арифметика прибавок не
    физиология, и без потолка движок обещал бы +260 кг в год.
    factor=None — статистики исполнения не хватило, вторая дата не считается.
    """
    used = sessions[: params.MAX_SIMULATED_SESSIONS]
    start = ctx.state.working_e1rm or 0.0
    first_day = used[0].date if used else None

    if not used or start <= 0:
        return Simulation(
            nominal_date=None, calibrated_date=None,
            nominal_slope=0.0, plan_slope=0.0,
            horizon=params.HORIZON_MATERIALIZED,
            calibration_available=factor is not None,
            factor=factor, sessions_used=0,
        )

    if target_e1rm <= start:
        return Simulation(
            nominal_date=first_day, calibrated_date=first_day,
            nominal_slope=0.0, plan_slope=0.0,
            horizon=params.HORIZON_MATERIALIZED,
            calibration_available=factor is not None,
            factor=factor, sessions_used=0,
        )

    reached_on: Optional[date] = None
    current = start
    history = ctx.history
    used_count = 0

    for future in used:
        # state/last_outcome сюда сознательно не пробрасываются — см.
        # докстринг модуля: plan_exercise() их всё равно отбросит и
        # пересчитает из history.
        step_ctx = replace(
            ctx,
            history=history,
            phase_effort_tier=future.phase_effort_tier,
            target_sets=future.prescription_sets or ctx.target_sets,
        )
        prescription = plan_exercise(step_ctx)
        used_count += 1

        top = prescription.top_weight
        if top is None:
            continue

        top_set = max(
            (s for s in prescription.sets if s.weight_kg == top),
            key=lambda s: s.rep_max or s.rep_min,
        )
        current = _e1rm(top, top_set.rep_max or top_set.rep_min)

        facts = _synthetic_facts(prescription)
        history = ExerciseHistory(
            exercise_id=history.exercise_id,
            sessions=(
                SessionFact(
                    session_id=-used_count,
                    finished_at=datetime.combine(future.date, time.min),
                    prescription=prescription,
                    sets=facts,
                ),
            ) + history.sessions[: _HISTORY_KEEP - 1],
        )

        if reached_on is None and current >= target_e1rm:
            reached_on = future.date
            break

    weeks = max((used[used_count - 1].date - first_day).days / _WEEK, 1e-9)
    raw_slope = max((current - start) / weeks, 0.0)
    nominal_slope = min(raw_slope, start * cap_pct)

    calibration_available = factor is not None
    plan_slope = nominal_slope * factor if calibration_available else nominal_slope

    if reached_on is None:
        # Цель не была подтверждена настоящей прокруткой движка в пределах
        # предоставленного горизонта сессий: экстраполировать дату «в
        # бесконечность» по среднему темпу здесь означало бы придумать
        # именно тот коэффициент, который решение 6 спеки отвергает — мы
        # обещаем дату только когда сами её увидели в прокрутке.
        nominal_date = None
        calibrated_date = None
    else:
        # Дату берём по ОБРЕЗАННОМУ темпу, а не по сырой прокрутке: иначе
        # потолок висел бы в отчёте, ни на что не влияя. Но только когда
        # потолок темп реально урезал — если сырой темп и так не выше
        # потолка, дата пересечения и есть дата, которую увидела прокрутка.
        nominal_date = _crossing_date(first_day, start, target_e1rm, nominal_slope)
        if nominal_slope >= raw_slope:
            nominal_date = reached_on
        calibrated_date = (
            _crossing_date(first_day, start, target_e1rm, plan_slope)
            if calibration_available
            else None
        )

    return Simulation(
        nominal_date=nominal_date,
        calibrated_date=calibrated_date,
        nominal_slope=nominal_slope,
        plan_slope=plan_slope,
        milestones=(
            _milestones(first_day, start, target_e1rm, plan_slope)
            if reached_on is not None
            else []
        ),
        horizon=params.HORIZON_MATERIALIZED,
        calibration_available=calibration_available,
        factor=factor,
        sessions_used=used_count,
    )


def _crossing_date(
    first_day: Optional[date], start: float, target: float, slope: float
) -> Optional[date]:
    """Дата пересечения цели при постоянном темпе. None — темпа нет."""
    if first_day is None or slope <= 0:
        return None
    if target <= start:
        return first_day
    weeks = (target - start) / slope
    days = int(round(weeks * _WEEK))
    return first_day + timedelta(days=days)


def _milestones(
    first_day: Optional[date], start: float, target: float, slope: float
) -> list[Milestone]:
    """Значения e1RM по неделям до пересечения цели (не более года)."""
    if first_day is None or slope <= 0 or target <= start:
        return []
    weeks = min(int((target - start) / slope) + 1, 52)
    return [
        Milestone(
            week_start=first_day + timedelta(days=7 * i),
            expected_e1rm=round(start + slope * i, 1),
        )
        for i in range(weeks + 1)
    ]
