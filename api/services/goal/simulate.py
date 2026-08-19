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
from api.services.progression.resolve import override_for
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
    """Предписание выполнено по верхней границе диапазона при заданном RIR.

    ПОБОЧНАЯ НАХОДКА (P0-12, фикс C1): у AMRAP-подхода (rep_max=None,
    открытый верх — так задаёт percent_1rm.plan) верхней границы не
    существует. Старое `sp.rep_max or sp.rep_min` падало здесь на
    буквальный rep_min — «выполнено ровно по минимуму» — и делало
    percent_1rm ЕДИНСТВЕННОЙ схемой, для которой «удавшаяся сессия»
    (спека §5.3: «симуляция предполагает, что каждая будущая сессия
    удалась») не двигает working_e1rm вовсе: training_max пересчитывается
    от AMRAP-результата, а ровно-минимум воспроизводит тот же вес на
    следующей сессии, не давая расти. Этот путь ни разу не исполнялся до
    фикса C1 (override схемы никуда не передавался — см. правку в run()
    ниже), поэтому и не был замечен раньше. +2 — тот же запас, что и у
    рычага "диапазон повторов" (decide._detail), а не новое число с нуля.
    """
    return tuple(
        SetFact(
            set_number=sp.set_number,
            weight_kg=sp.weight_kg,
            reps=sp.rep_max if sp.rep_max is not None else sp.rep_min + 2,
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

    # ФИКС C1 (финальное ревью P0-12, побочная находка): plan_exercise()
    # принимает ручной override схемы ТРЕТЬИМ параметром, а не читает его
    # из ctx.settings сам — это делает вызывающий код через
    # progression.resolve.override_for (см. её докстринг). Раньше run()
    # звала plan_exercise(step_ctx) без override вовсе, и LEVER_SCHEME
    # (apply_lever пишет override именно в ctx.settings) был бы НЕВИДИМ
    # симуляции целиком: resolve_scheme(ctx, override=None) никогда не
    # увидел бы override и всегда шёл бы дальше по фазе/эвристике — рычаг
    # "схема" не смог бы дать ни одного отличного от нуля эффекта ни при
    # каком плане. exercise_id и settings не меняются между итерациями
    # цикла (settings — часть ctx, exercise_id — ctx.history.exercise_id,
    # неизменный между шагами), поэтому override считается один раз.
    override = override_for(ctx.settings, ctx.history.exercise_id)

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
        prescription = plan_exercise(step_ctx, override=override)
        used_count += 1

        top = prescription.top_weight
        if top is None:
            continue

        # ИСПРАВЛЕНО (финальное ревью, Deferred Minor 3): раньше max() шёл
        # без default по генератору, который теоретически мог оказаться
        # пустым, — от ValueError его спасало только совпадение: top ==
        # prescription.top_weight УЖЕ есть max() по тем же sets (см. её
        # докстринг в progression/types.py), так что хотя бы один set с
        # weight_kg == top гарантированно существовал. Это совпадение —
        # случайность реализации top_weight, а не контракт этой функции;
        # default=None делает падение невозможным явно, а не по случайности.
        top_set = max(
            (s for s in prescription.sets if s.weight_kg == top),
            key=lambda s: s.rep_max or s.rep_min,
            default=None,
        )
        if top_set is None:
            continue
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


def calibration_factor(
    adherence_ratios: list[float], lift_sessions: int, success_rate: float
) -> Optional[float]:
    """Множитель темпа из собственной статистики пользователя.

    None означает «данных мало» — вторая дата не показывается вовсе.
    Выдуманного множителя не бывает: это ровно то число, которое
    пользователь не смог бы проверить.
    """
    if len(adherence_ratios) < params.CALIBRATION_MIN_WINDOWS:
        return None
    if lift_sessions < params.CALIBRATION_MIN_SESSIONS:
        return None
    adherence = sum(adherence_ratios) / len(adherence_ratios)
    raw = adherence * success_rate
    return max(params.CALIBRATION_MIN_FACTOR, min(params.CALIBRATION_MAX_FACTOR, raw))


# --- P0-12, фикс C1: рычаг -> изменённый план для повторного прогона run() ---
#
# decide.py просит "какой темп даст этот рычаг" через simulate_with, а
# simulate_with (service.py) отвечает, прогоняя run() ЕЩЁ РАЗ над планом,
# который меняет apply_lever. Здесь и только здесь живёт знание о том, ЧТО
# именно каждый рычаг меняет в сессиях/контексте — то же самое знание, что
# использует apply_goal_decision (service.py) для настоящей записи в БД,
# но здесь оно только перестраивает dataclasses в памяти, БД не касается.


def _typical_sets(sessions: list[FutureSession], fallback: int) -> int:
    """Подходов на сессию по умолчанию для СИНТЕЗИРУЕМЫХ сессий — среднее по
    уже существующим, а не выдуманное число; fallback — когда сессий ещё нет
    (ensure_present: лифта в плане пока нет вовсе)."""
    if not sessions:
        return max(1, fallback)
    return max(1, round(sum(s.prescription_sets for s in sessions) / len(sessions)))


def _spread_sessions(
    base: list[FutureSession],
    extra_per_cycle: int,
    microcycle_length: int,
    start: date,
    until: date,
    prescription_sets: int,
) -> list[FutureSession]:
    """Добавить `extra_per_cycle` синтетических сессий на КАЖДЫЙ микроцикл
    длиной `microcycle_length` в окне [start, until], равномерно расставленных
    внутри цикла, и слить с уже существующими.

    Прокси для "лифт появляется в плане чаще/впервые" (LEVER_ENSURE_PRESENT —
    base пуст, LEVER_LIFT_FREQUENCY — extra_per_cycle=1, LEVER_STRUCTURAL —
    extra_per_cycle=2) БЕЗ обращения к настоящему генератору расписания:
    полный прогон SchedulingEngine на каждый кандидат-рычаг в decide()
    означал бы запись в БД на КАЖДУЮ пробную симуляцию — недопустимая цена
    за число, которое может быть тут же отброшено (эффект <= 0). Это
    осознанное приближение, а не точный повтор перегенерации; оно
    задокументировано в отчёте задачи как известное упрощение.
    """
    if extra_per_cycle <= 0 or microcycle_length <= 0 or start > until:
        return list(base)

    offsets = [
        round(microcycle_length * (i + 1) / (extra_per_cycle + 1))
        for i in range(extra_per_cycle)
    ]
    extra: list[FutureSession] = []
    cycle_start = start
    while cycle_start <= until:
        for offset in offsets:
            d = cycle_start + timedelta(days=offset)
            if start <= d <= until:
                extra.append(FutureSession(
                    date=d, phase_effort_tier="medium", prescription_sets=prescription_sets,
                ))
        cycle_start += timedelta(days=microcycle_length)

    return sorted(list(base) + extra, key=lambda s: s.date)


def apply_lever(
    kind: str,
    detail: dict,
    sessions: list[FutureSession],
    ctx: SchemeContext,
    *,
    exercise_id: int,
    microcycle_length: int,
    start: date,
    until: date,
) -> tuple[list[FutureSession], SchemeContext]:
    """План (сессии + контекст), КАКИМ ОН БУДЕТ, если применить рычаг `kind`.

    Та же правка, которую реально запишет apply_goal_decision в БД (см. её
    носители в спеке §5.2), но здесь только в памяти — для повторного
    прогона run() внутри simulate_with (decide.py). Ни разу не обращается к
    БД: exercise_id/microcycle_length/start/until — уже известные вызывающей
    стороне числа, а не запросы.
    """
    if kind == params.LEVER_ENSURE_PRESENT:
        # Лифта в sessions нет вовсе (см. decide._applicable) — синтезируем
        # его присутствие с нуля, по одной сессии на микроцикл.
        return _spread_sessions([], 1, microcycle_length, start, until, ctx.target_sets), ctx

    if kind == params.LEVER_SETS:
        delta = int(detail.get("delta_sets") or 0)
        if delta <= 0 or not sessions:
            return list(sessions), ctx
        return (
            [replace(s, prescription_sets=s.prescription_sets + delta) for s in sessions],
            ctx,
        )

    if kind == params.LEVER_REP_RANGE:
        rep_min, rep_max = detail.get("rep_min"), detail.get("rep_max")
        if rep_min is None or rep_max is None:
            return list(sessions), ctx
        return list(sessions), replace(ctx, rep_min=int(rep_min), rep_max=int(rep_max))

    if kind == params.LEVER_SCHEME:
        to_scheme = detail.get("to_scheme")
        if not to_scheme:
            return list(sessions), ctx
        # Тот же путь резолва, что и настоящее применение (_apply_scheme
        # пишет туда же, profile.settings.progression.overrides) — override
        # приоритетнее фазы мезоцикла и эвристики (resolve_scheme), поэтому
        # это действительно меняет схему, которую выберет движок.
        settings = dict(ctx.settings or {})
        progression = dict(settings.get("progression") or {})
        overrides = dict(progression.get("overrides") or {})
        overrides[str(exercise_id)] = to_scheme
        progression["overrides"] = overrides
        settings["progression"] = progression
        return list(sessions), replace(ctx, settings=settings)

    if kind in params.STRUCTURAL_LEVERS:
        # LEVER_LIFT_FREQUENCY — +1 сессия на микроцикл (та же дельта, что
        # decide._detail даёт настоящему рычагу). LEVER_STRUCTURAL — более
        # дорогая для пользователя ступень (другой сплит/частота/длина
        # микроцикла через генератор); полный прогон генератора здесь
        # недоступен (см. докстринг _spread_sessions), поэтому моделируем
        # её как более сильную версию того же механизма — +2 сессии на
        # микроцикл вместо одной.
        extra = 2 if kind == params.LEVER_STRUCTURAL else 1
        typical = _typical_sets(sessions, ctx.target_sets)
        return _spread_sessions(sessions, extra, microcycle_length, start, until, typical), ctx

    return list(sessions), ctx
