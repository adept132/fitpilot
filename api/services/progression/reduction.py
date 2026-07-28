"""Общий слой снижения нагрузки поверх любой схемы.

Схемы умеют только повышать. Всё снижение живёт здесь — иначе каждая из
четырёх схем заведёт свою копию правил отката, и они разъедутся.

Инвариант: за одну сессию срабатывает НЕ БОЛЕЕ ОДНОГО снижающего правила.
Иначе плато и повторный недобор дадут -20 % разом.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from api.services.progression import params
from api.services.progression.rounding import round_down_to_step, step_kg
from api.services.progression.state import working_sets
from api.services.progression.types import Prescription, SchemeContext


def _with_weight(
    prescription: Prescription, weight: Optional[float], reason_code: str
) -> Prescription:
    """Тот же набор подходов, другой вес и другая причина."""
    sets = tuple(replace(s, weight_kg=weight) for s in prescription.sets)
    return replace(
        prescription,
        sets=sets,
        reason_code=reason_code,
        reason_text=params.REASON_TEXTS[reason_code],
    )


def _with_reason(prescription: Prescription, reason_code: str) -> Prescription:
    """Меняет только причину, веса подходов не трогает.

    Нужно там, где переякориться не на что (нет ни last_top_weight, ни
    фактического веса), но пользователя всё равно нужно предупредить —
    выдумывать вес нельзя, вес схемы остаётся как есть.
    """
    return replace(
        prescription,
        reason_code=reason_code,
        reason_text=params.REASON_TEXTS[reason_code],
    )


def _reduced(ctx: SchemeContext, anchor: float) -> float:
    """-10 % с округлением ВНИЗ и гарантией фактического снижения."""
    equipment = list(ctx.equipment)
    steps = ctx.weight_steps or None
    target = anchor * params.REDUCTION_RATIO
    result = round_down_to_step(target, equipment, ctx.unit, steps)
    if result >= anchor:
        # Шаг оборудования крупнее 10 % — снижаем ровно на один шаг.
        result = round_down_to_step(
            anchor - step_kg(equipment, ctx.unit, steps), equipment, ctx.unit, steps
        )
    return result


def _actual_last_weight(ctx: SchemeContext) -> Optional[float]:
    """Вес, с которым пользователь реально работал в прошлый раз."""
    for session in ctx.history.sessions:
        usable = [s for s in working_sets(session.sets) if not s.is_anomalous]
        weights = [float(s.weight_kg) for s in usable if s.weight_kg is not None]
        if weights:
            return max(weights)
    return None


def apply_reduction(prescription: Prescription, ctx: SchemeContext) -> Prescription:
    """Решает, расти ли вообще. Порядок правил — из спеки §8.2."""
    if not prescription.sets:
        return prescription

    anchor = ctx.state.last_top_weight
    outcome = ctx.last_outcome

    # 1. Фаза разгрузки: расти не положено.
    if ctx.phase_effort_tier == "deload":
        if anchor is None:
            # Якоря нет (например, первая обработка упражнения с легаси-
            # логами на неделе разгрузки) — вес предписания схемы не
            # трогаем, чтобы не обнулить его, но причину сообщаем.
            return _with_reason(prescription, "deload_phase")
        return _with_weight(prescription, anchor, "deload_phase")

    # 2. Длинный перерыв. Только арифметика по датам — субъективные сигналы
    #    (сон, стресс, боль) подключатся здесь же в P0-07.
    if (
        ctx.days_since_last_session is not None
        and ctx.days_since_last_session > params.LAYOFF_DAYS
        and anchor is not None
    ):
        return _with_weight(prescription, _reduced(ctx, anchor), "layoff")

    # 3. Пользователь работал с другим весом — переякориваемся, но не наказываем.
    if outcome is not None and outcome.status == "deviated":
        actual = _actual_last_weight(ctx)
        if actual is not None or anchor is not None:
            return _with_weight(prescription, actual or anchor, "weight_deviation")
        # Переякорить не на что: ни фактического веса, ни якоря не известно.
        # Правило не срабатывает — идём дальше по списку (4, 5, 6, 7).

    # 4. Повторный или тяжёлый недобор.
    severe = (
        outcome is not None
        and outcome.total_sets > 0
        and outcome.miss_sets / outcome.total_sets >= params.SEVERE_MISS_RATIO
    )
    if anchor is not None and (
        ctx.state.consecutive_misses >= params.MISS_STREAK_FOR_REDUCTION or severe
    ):
        return _with_weight(prescription, _reduced(ctx, anchor), "repeated_miss")

    # 5. Один недобор — вес держим.
    if ctx.state.consecutive_misses == 1 and anchor is not None:
        return _with_weight(prescription, anchor, "hold_after_miss")

    # 6. Плато.
    if ctx.state.stalled and anchor is not None:
        return _with_weight(prescription, _reduced(ctx, anchor), "plateau_reset")

    # 7. Ничего не мешает — предписание схемы как есть.
    return prescription
