"""Округление веса к шагу доступного оборудования.

Перенос из autoprogression.py плюс округление вниз: при снижении нагрузки
округление к ближайшему может вернуть исходный вес и не снизить ничего
(блочный тренажёр, шаг 10 lb на рабочих 30 кг).
"""

from __future__ import annotations

import math
from typing import Optional

from api.services import equipment as equip
from api.services.progression.params import DEFAULT_WEIGHT_STEPS

# Физические константы перевода единиц — не настраиваемые пороги,
# поэтому в params.py не переносим.
LB_PER_KG = 2.2046226218
KG_PER_LB = 0.45359237


def _resolve_step(category: str, unit: str, steps: Optional[dict]):
    """(шаг, единица шага) для категории оборудования."""
    s = {**DEFAULT_WEIGHT_STEPS, **(steps or {})}
    if category == equip.STEP_PLATE:
        return (s["plate_lb"], "lb") if unit == "lbs" else (s["plate_kg"], "kg")
    if category == equip.STEP_DUMBBELL:
        return (s["dumbbell_lb"], "lb") if unit == "lbs" else (s["dumbbell_kg"], "kg")
    if category == equip.STEP_BLOCK:
        return (s["block_lb"], "lb")
    # Прочее оборудование (bodyweight/band/...): тот же шаг, что и для
    # штанги/тренажёра со свободным весом — тоже должен уважать
    # пользовательское settings.weight_steps, а не жёстко зашитые 5/2.5.
    return (s["plate_lb"], "lb") if unit == "lbs" else (s["plate_kg"], "kg")


def _step_pair(equipment_needed, unit: str, steps: Optional[dict]):
    category = equip.equipment_to_step_category(equipment_needed)
    step, step_unit = _resolve_step(category, unit, steps)
    if not step or step <= 0:
        step, step_unit = (2.5, "kg")
    return step, step_unit


def step_kg(equipment_needed, unit: str = "kg", steps: Optional[dict] = None) -> float:
    """Размер шага в килограммах — для сравнений «в пределах одного шага»."""
    step, step_unit = _step_pair(equipment_needed, unit, steps)
    return round(step * KG_PER_LB, 4) if step_unit == "lb" else float(step)


def _apply(
    weight_kg: float, equipment_needed, unit, steps, round_down: bool
) -> float:
    """round_down — булев флаг, а не строка: опечатка не может тихо
    подменить режим округления к ближайшему."""
    step, step_unit = _step_pair(equipment_needed, unit, steps)
    fn = math.floor if round_down else round

    if step_unit == "lb":
        lbs = weight_kg * LB_PER_KG
        rounded = fn(lbs / step) * step
        return round(max(step, rounded) * KG_PER_LB, 2)

    rounded = fn(weight_kg / step) * step
    return round(max(step, rounded), 2)


def round_to_step(
    weight_kg: float, equipment_needed, unit: str = "kg", steps: Optional[dict] = None
) -> float:
    """К ближайшему шагу. Нижняя граница — один шаг."""
    return _apply(weight_kg, equipment_needed, unit, steps, round_down=False)


def round_down_to_step(
    weight_kg: float, equipment_needed, unit: str = "kg", steps: Optional[dict] = None
) -> float:
    """Вниз до шага. Нижняя граница — один шаг."""
    return _apply(weight_kg, equipment_needed, unit, steps, round_down=True)
