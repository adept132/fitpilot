"""Чистая математика усталостной модели. Ни одного обращения к БД.

Инвариант: две валюты не складываются. Внутренняя нагрузка (AU, «как тяжело
далось») и внешняя механика (кг*повт, «какие силы на ткани») идут раздельно
до отсеков включительно и отвечают на разные вопросы.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from api.services.fatigue.params import FatigueParams


@dataclass(frozen=True)
class SetImpulse:
    """Один рабочий подход как источник нагрузки."""
    at: datetime
    weight_kg: float
    reps: int
    rir: int
    main_muscle: str
    secondary_muscles: tuple[str, ...]
    fatigue_tier: int


@dataclass(frozen=True)
class CompartmentLoads:
    """Разложение одного импульса по отсекам.

    systemic и muscular — в AU (трек A); mechanical — в кг*повт (трек B).
    Складывать их между собой нельзя.
    """
    systemic: float
    muscular: dict[str, float]
    mechanical: float


def e1rm_epley(weight_kg: float, reps: int) -> float:
    """«Сырой» e1RM по Эпли, без поправки на RIR.

    ВНУТРЕННЯЯ функция. Наружу не отдаётся: единственный видимый пользователю
    e1RM — autoprogression.set_target_value, который складывает RIR внутрь
    формулы. Здесь RIR учитывается отдельным множителем EF, и применять его
    дважды было бы ошибкой.
    """
    return weight_kg * (1 + reps / 30)


def effort_factor(rir: int, p: FatigueParams) -> float:
    """Множитель близости к отказу: последние повторения непропорционально дороги.

    Потолок обязателен — без него отказные подходы разносит.
    """
    return min(math.exp(p.beta * (p.rir_ref - rir)), p.ef_cap)


def internal_load_set(weight_kg: float, reps: int, rir: int, p: FatigueParams) -> float:
    """Трек A: вклад подхода во внутреннюю нагрузку, в AU."""
    if reps <= 0 or weight_kg <= 0:
        return 0.0
    e1rm = e1rm_epley(weight_kg, reps)
    if e1rm <= 0:
        return 0.0
    return reps * (weight_kg / e1rm) * effort_factor(rir, p)


def mechanical_load_set(weight_kg: float, reps: int, p: FatigueParams) -> float:
    """Трек B: взвешенный по интенсивности тоннаж, в кг*повт.

    Нелинейность (W/e1RM)^q живёт именно здесь: высокая доля от максимума
    непропорционально грузит связки. Из ощущения это не выводится.
    """
    if reps <= 0 or weight_kg <= 0:
        return 0.0
    e1rm = e1rm_epley(weight_kg, reps)
    if e1rm <= 0:
        return 0.0
    return weight_kg * reps * (weight_kg / e1rm) ** p.q


def split_to_compartments(impulse: SetImpulse, p: FatigueParams) -> CompartmentLoads:
    """Раздаёт нагрузку подхода по отсекам.

    Внутренняя нагрузка делится между systemic и muscular по профилю
    fatigue_tier; внутри muscular — пропорционально вовлечённости мышц.
    Механика идёт в свой отсек нетронутой.
    """
    internal = internal_load_set(impulse.weight_kg, impulse.reps, impulse.rir, p)
    mechanical = mechanical_load_set(impulse.weight_kg, impulse.reps, p)

    a_sys, a_mus = p.a_split_by_tier.get(
        impulse.fatigue_tier, p.a_split_by_tier[2]
    )

    weights: dict[str, float] = {impulse.main_muscle: p.w_direct}
    for muscle in impulse.secondary_muscles:
        if muscle and muscle != impulse.main_muscle:
            weights[muscle] = weights.get(muscle, 0.0) + p.w_indirect

    total_weight = sum(weights.values())
    muscular = (
        {m: internal * a_mus * (w / total_weight) for m, w in weights.items()}
        if total_weight > 0
        else {}
    )

    return CompartmentLoads(
        systemic=internal * a_sys,
        muscular=muscular,
        mechanical=mechanical,
    )
