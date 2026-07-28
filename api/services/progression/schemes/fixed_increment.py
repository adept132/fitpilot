"""Фиксированная прибавка: повторы фиксированы, вес растёт при выполнении.

Схема для новичка на тяжёлой базе и для случая, когда пользователь задал в
плане точное число повторов, а не диапазон (спека P0-06 §6.3).

Относительный потолок обязателен: без него новичок с приседом 20 кг получает
два шага = +5 кг = +25 % за сессию и упирается в стену за месяц. При этом
инкремент никогда не опускается ниже одного шага оборудования — иначе
прибавка станет нулевой и прогрессия встанет.

Инвариант всего движка: схема НИКОГДА не уменьшает вес, даже при полном
провале — снижение целиком живёт в общем слое reduction.py (Задача 11).
"""

from __future__ import annotations

from typing import Optional

from api.services.muscle_keys import to_system_key
from api.services.progression import params
from api.services.progression.rounding import round_down_to_step, step_kg
from api.services.progression.state import working_sets
from api.services.progression.types import (
    Prescription,
    SchemeContext,
    SetPrescription,
)


def _is_lower_body(main_muscle_group: Optional[str]) -> bool:
    """Нижняя часть тела — quads/hamstrings/glutes/calves. Всё остальное,
    включая неизвестное, консервативно считается верхом (k=1)."""
    key = to_system_key(main_muscle_group)
    return key in params.LOWER_BODY_KEYS


def increment_for(ctx: SchemeContext, working_weight: float) -> float:
    """Прибавка в кг: шаг оборудования x k, срезанная относительным потолком.

    k = 2 для низа тела, 1 для верха. Потолок — FIXED_INCREMENT_MAX_RATIO от
    рабочего веса, но не ниже одного шага оборудования (иначе прибавка
    обнулится и прогрессия встанет намертво).
    """
    step = step_kg(list(ctx.equipment), ctx.unit, ctx.weight_steps or None)
    multiplier = (
        params.LOWER_BODY_INCREMENT_STEPS
        if _is_lower_body(ctx.main_muscle_group)
        else params.UPPER_BODY_INCREMENT_STEPS
    )
    raw = step * multiplier
    cap = working_weight * params.FIXED_INCREMENT_MAX_RATIO
    if cap > step:
        return max(step, min(raw, cap))
    return step


def _advance(ctx: SchemeContext, anchor: float) -> float:
    """Вес после успешной сессии: анкор + инкремент, округлённый вниз к шагу.

    Известная ловушка: округление вниз может съесть прибавку целиком, если
    анкор лежит точно на границе шага оборудования — например, для блочного
    тренажёра (шаг всегда в lb) перевод кг-эквивалента шага туда и обратно
    теряет доли грамма, и сумма при floor-делении в lb возвращается в тот же
    грид-пункт, что и анкор. Если округлённый вниз результат не строго выше
    анкора — гарантируем реальный рост минимум на один шаг напрямую, без
    повторного округления, вместо тихой остановки прогрессии навсегда.
    """
    increment = increment_for(ctx, anchor)
    equipment = list(ctx.equipment)
    weight = round_down_to_step(anchor + increment, equipment, ctx.unit, ctx.weight_steps or None)
    if weight <= anchor:
        step = step_kg(equipment, ctx.unit, ctx.weight_steps or None)
        weight = round(anchor + step, 2)
    return weight


def _last_session_completed(ctx: SchemeContext) -> Optional[bool]:
    """Выполнены ли все предписанные подходы прошлой сессии с данными."""
    for session in ctx.history.sessions:
        usable = [s for s in working_sets(session.sets) if not s.is_anomalous]
        if not usable:
            continue
        if session.prescription is None or not session.prescription.sets:
            return None
        required = {sp.set_number: sp.rep_min for sp in session.prescription.sets}
        default = session.prescription.sets[-1].rep_min
        return all(
            int(s.reps) >= required.get(s.set_number, default) for s in usable
        )
    return None


def _anchor(ctx: SchemeContext) -> Optional[float]:
    """Рабочий вес-якорь: максимум по последней сессии с данными, иначе
    последний известный top_weight из состояния."""
    for session in ctx.history.sessions:
        usable = [s for s in working_sets(session.sets) if not s.is_anomalous]
        weights = [float(s.weight_kg) for s in usable if s.weight_kg is not None]
        if weights:
            return max(weights)
    return ctx.state.last_top_weight


def plan(ctx: SchemeContext) -> Prescription:
    anchor = _anchor(ctx)
    if anchor is None:
        return Prescription(
            scheme=params.SCHEME_FIXED_INCREMENT,
            sets=(),
            reason_code="no_basis",
            reason_text=params.REASON_TEXTS["no_basis"],
        )

    completed = _last_session_completed(ctx)
    weight = _advance(ctx, anchor) if completed else anchor

    reps = ctx.rep_min if ctx.rep_min > 0 else ctx.rep_max
    sets = tuple(
        SetPrescription(n, weight, reps, reps, ctx.target_rir, "normal")
        for n in range(1, max(1, ctx.target_sets) + 1)
    )
    return Prescription(
        scheme=params.SCHEME_FIXED_INCREMENT,
        sets=sets,
        reason_code="progressed",
        reason_text=params.REASON_TEXTS["progressed"],
        basis={"anchor_weight": anchor, "completed": bool(completed)},
    )
