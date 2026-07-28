"""Double progression: растём повторами внутри диапазона, затем весом.

Основная схема. Причина — свойства модели данных: DUP-матрица отдаёт
диапазоны, а шаг оборудования слишком груб для прогрессии одним весом
(блочный тренажёр — 10 lb, на рабочих 20 кг это +22 %).

Цель на каждый подход считается от ЭТОГО ЖЕ подхода прошлой сессии.
Утомление внутри упражнения нормально: 10/9/7 — физиология, а не провал,
и единая цель по худшему подходу недооценивала бы первый.

Вес не предписан (свой вес без отягощения) — расти нечем, когда повторы
упираются в потолок диапазона: схема прямо просит внешнюю нагрузку
(reason_code="needs_external_load"), а не молча замирает на месте.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from api.services import equipment as equip
from api.services.progression import params
from api.services.progression.rounding import round_to_step, step_kg
from api.services.progression.state import working_sets
from api.services.progression.types import (
    Prescription,
    SchemeContext,
    SetFact,
    SetPrescription,
)


def _usable_facts(facts: Sequence[SetFact]) -> List[SetFact]:
    """Подходы, по которым можно читать повторы прошлой сессии.

    Уже: без разминки/дропов, без аномальных, с положительными повторами.
    Специально НЕ используем state.working_sets() — там отсутствие веса
    (weight_kg is None) считается неполными данными (нужно для evaluate(),
    чтобы отличить забытый лог от факта). Для double это неверно: у
    упражнения со своим весом weight_kg=None — легитимный, полный факт
    подхода, а не пропуск, и повторы по нему такие же ориентиры для роста.
    """
    result = []
    for s in facts:
        if s.is_anomalous:
            continue
        if (s.set_type or "normal").lower() in params.IGNORED_SET_TYPES:
            continue
        if s.reps is None or s.reps <= 0:
            continue
        result.append(s)
    return result


def _previous_reps(ctx: SchemeContext) -> List[int]:
    """Повторы по подходам ближайшей сессии с данными, по порядку set_number."""
    for session in ctx.history.sessions:
        usable = _usable_facts(session.sets)
        if usable:
            return [int(s.reps) for s in sorted(usable, key=lambda s: s.set_number)]
    return []


def _previous_weight(ctx: SchemeContext) -> Optional[float]:
    """Последний рабочий вес. Здесь working_sets() уместна: ищем именно вес,
    и его отсутствие — законный повод пропустить подход при поиске якоря."""
    for session in ctx.history.sessions:
        usable = [s for s in working_sets(session.sets) if not s.is_anomalous]
        if usable:
            weights = [float(s.weight_kg) for s in usable if s.weight_kg is not None]
            if weights:
                return max(weights)
    return None


def _uses_external_weight(ctx: SchemeContext) -> bool:
    """Есть ли чему расти в весе — определяется оборудованием упражнения,
    а не наличием весовых данных (их может не быть и у штанги — первая
    сессия). Свой вес без отягощения (bodyweight) — расти в весе нечем."""
    normalized = equip.normalize_equipment_list(ctx.equipment)
    return equip.BODYWEIGHT not in normalized


def plan(ctx: SchemeContext) -> Prescription:
    sets_count = max(1, ctx.target_sets)
    previous = _previous_reps(ctx)
    external = _uses_external_weight(ctx)

    anchor = _previous_weight(ctx)
    if anchor is None:
        anchor = ctx.state.last_top_weight
    # Для bodyweight-упражнения вес в предписании всегда None: last_top_weight
    # мог остаться от другого контекста, и якорем прошлого веса он тут быть
    # не должен — расти в весе схема для bodyweight не умеет вовсе.
    weight = anchor if external else None

    if not previous:
        # Истории нет: держим якорь (если он есть) и заходим с низа диапазона.
        if external and anchor is None:
            return Prescription(
                scheme=params.SCHEME_DOUBLE,
                sets=(),
                reason_code="no_basis",
                reason_text=params.REASON_TEXTS["no_basis"],
            )
        sets = tuple(
            SetPrescription(n, weight, ctx.rep_min, ctx.rep_max, ctx.target_rir, "normal")
            for n in range(1, sets_count + 1)
        )
        return Prescription(
            scheme=params.SCHEME_DOUBLE,
            sets=sets,
            reason_code="progressed",
            reason_text=params.REASON_TEXTS["progressed"],
            basis={"anchor_weight": anchor},
        )

    ceiling_reached = all(reps >= ctx.rep_max for reps in previous)

    if ceiling_reached and not external:
        # Свой вес без отягощения: расти дальше нечем, говорим об этом прямо.
        sets = tuple(
            SetPrescription(n, None, ctx.rep_max, ctx.rep_max, ctx.target_rir, "normal")
            for n in range(1, sets_count + 1)
        )
        return Prescription(
            scheme=params.SCHEME_DOUBLE,
            sets=sets,
            reason_code="needs_external_load",
            reason_text=params.REASON_TEXTS["needs_external_load"],
        )

    if ceiling_reached:
        # Все подходы взяли верх диапазона -> прибавляем вес и сбрасываем
        # повторы на низ диапазона.
        step = step_kg(list(ctx.equipment), ctx.unit, ctx.weight_steps or None)
        new_weight = round_to_step(
            (anchor or 0.0) + step, list(ctx.equipment), ctx.unit, ctx.weight_steps or None
        )
        sets = tuple(
            SetPrescription(n, new_weight, ctx.rep_min, ctx.rep_max, ctx.target_rir, "normal")
            for n in range(1, sets_count + 1)
        )
        return Prescription(
            scheme=params.SCHEME_DOUBLE,
            sets=sets,
            reason_code="progressed",
            reason_text=params.REASON_TEXTS["progressed"],
            basis={"anchor_weight": anchor, "advanced_by_step": step},
        )

    # Хотя бы один подход ниже потолка: вес держим, каждый подход растёт
    # от своей собственной прошлой цифры (не от худшего подхода).
    sets = []
    for n in range(1, sets_count + 1):
        prior = previous[n - 1] if n - 1 < len(previous) else previous[-1]
        target = min(prior + 1, ctx.rep_max)
        sets.append(
            SetPrescription(n, weight, target, ctx.rep_max, ctx.target_rir, "normal")
        )

    return Prescription(
        scheme=params.SCHEME_DOUBLE,
        sets=tuple(sets),
        reason_code="progressed",
        reason_text=params.REASON_TEXTS["progressed"],
        basis={"anchor_weight": anchor, "previous_reps": previous},
    )
