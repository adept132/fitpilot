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
from api.services.progression.rounding import (
    round_down_to_step,
    round_to_step,
    step_kg,
)
from api.services.progression.state import working_sets
from api.services.progression.types import Prescription, SchemeContext


def _with_weight(
    prescription: Prescription, weight: Optional[float], reason_code: str
) -> Prescription:
    """Тот же набор подходов, другой вес и другая причина.

    Вес сюда передаётся уже приведённым к сетке шага оборудования —
    приведение делает вызывающая сторона (Находка 2 ревью Задачи 12,
    P0-06), а не эта функция: разные причины несут разную семантику веса
    (anchor из прошлого предписания vs фактический залогированный вес
    пользователя), и решение "округлять или нет" завязано на эту разницу
    (см. комментарий у правила 3 в apply_reduction).
    """
    sets = tuple(replace(s, weight_kg=weight) for s in prescription.sets)
    return replace(
        prescription,
        sets=sets,
        reason_code=reason_code,
        reason_text=params.REASON_TEXTS[reason_code],
    )


def _on_grid(ctx: SchemeContext, weight: Optional[float]) -> Optional[float]:
    """round_to_step, если вес есть — свёрнуто для читаемости на местах
    вызова _with_weight (Находка 2 ревью Задачи 12, P0-06)."""
    if weight is None:
        return None
    return round_to_step(weight, list(ctx.equipment), ctx.unit, ctx.weight_steps or None)


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
        # anchor — прошлый top_weight из ProgressionState, восстановленный
        # rebuild_state()-ом из Prescription.top_weight прошлых сессий; это
        # НОВОЕ предписание пользователю на предстоящую сессию, поэтому вес
        # обязан лежать на сетке (Находка 2 ревью Задачи 12, P0-06) — легаси-
        # записи/ручной ввод могли сохранить top_weight мимо сетки.
        return _with_weight(prescription, _on_grid(ctx, anchor), "deload_phase")

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
            # НЕ приводим к сетке: actual — это _actual_last_weight(), то
            # есть факт того, что человек реально поднял (SetFact.weight_kg),
            # а не будущее предписание, которое нужно суметь набрать
            # железом. Округление здесь исказило бы "мы увидели, что вы
            # работали с X" в "мы вам советуем Y" — смысл правила 3 (см.
            # docstring выше: "переякориваемся, но не наказываем"), а также
            # сломало бы принятый тест
            # test_deviation_reanchors_to_actual_weight_from_history (42.0
            # кг для barbell не лежит на сетке 2.5 кг, и тест ожидает это
            # значение дословно). Находка 2 ревью Задачи 12 (P0-06) отмечает
            # эту ветку как потенциально несогласованную с инвариантом
            # «предписанный вес всегда на сетке» — оставлено как известный
            # разрыв, чинить в этом заходе запрещено жёстким условием
            # ревью (нельзя ломать принятые тесты).
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
        # См. комментарий у правила 1 (deload_phase): anchor — не факт
        # подхода, а прошлый top_weight предписания, и это НОВОЕ
        # предписание — обязан лежать на сетке (Находка 2).
        return _with_weight(prescription, _on_grid(ctx, anchor), "hold_after_miss")

    # 6. Плато.
    if ctx.state.stalled and anchor is not None:
        return _with_weight(prescription, _reduced(ctx, anchor), "plateau_reset")

    # --- P0-07: новые ОБЪЕКТИВНЫЕ строки. Все три удерживающие и стоят
    # после снижающих 2, 4, 6 — снижение сильнее удержания (спека §7.1).

    # 7. Цель взята ценой отказа: рост останавливаем, но не откатываем.
    #    Сознательный подход до отказа — не провал, наказывать за него
    #    снижением было бы багом.
    if outcome is not None and outcome.status == "strained" and anchor is not None:
        return _with_weight(prescription, _on_grid(ctx, anchor), "strained_hold")

    # 8. Упражнение было в сессии, но подходов не залогировано.
    #    Отдельный флаг, а не outcome.status: _latest_outcome намеренно
    #    пролистывает пропуски в поисках последней результативной сессии,
    #    и менять его семантику ради одного правила нельзя.
    if ctx.last_session_skipped and anchor is not None:
        return _with_weight(prescription, _on_grid(ctx, anchor), "exercise_skipped")

    # 9. Средний перерыв. Строка 2 уже забрала всё, что длиннее LAYOFF_DAYS,
    #    поэтому сюда попадает только (LAYOFF_SOFT_DAYS, LAYOFF_DAYS].
    if (
        ctx.days_since_last_session is not None
        and ctx.days_since_last_session > params.LAYOFF_SOFT_DAYS
        and anchor is not None
    ):
        return _with_weight(prescription, _on_grid(ctx, anchor), "layoff_soft")

    # 10. Ничего не мешает — предписание схемы как есть.
    return prescription
