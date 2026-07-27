"""Округление веса к шагу доступного оборудования.

Перенос из autoprogression.py плюс округление вниз: при снижении нагрузки
округление к ближайшему может вернуть исходный вес и не снизить ничего
(блочный тренажёр, шаг 10 lb на рабочих 30 кг).
"""

from __future__ import annotations

from typing import Optional

from api.services import equipment as equip

LB_PER_KG = 2.2046226218
KG_PER_LB = 0.45359237

DEFAULT_WEIGHT_STEPS = {
    "plate_kg": 2.5,
    "plate_lb": 5,
    "dumbbell_kg": 2.5,
    "dumbbell_lb": 5,
    "block_lb": 10,  # блочный тренажёр — всегда в фунтах
}


def _resolve_step(category: str, unit: str, steps: Optional[dict]):
    """(шаг, единица шага) для категории оборудования."""
    s = {**DEFAULT_WEIGHT_STEPS, **(steps or {})}
    if category == equip.STEP_PLATE:
        return (s["plate_lb"], "lb") if unit == "lbs" else (s["plate_kg"], "kg")
    if category == equip.STEP_DUMBBELL:
        return (s["dumbbell_lb"], "lb") if unit == "lbs" else (s["dumbbell_kg"], "kg")
    if category == equip.STEP_BLOCK:
        return (s["block_lb"], "lb")
    return (5, "lb") if unit == "lbs" else (2.5, "kg")


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


def _apply(weight_kg: float, equipment_needed, unit, steps, mode: str) -> float:
    import math

    step, step_unit = _step_pair(equipment_needed, unit, steps)
    fn = math.floor if mode == "down" else round

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
    return _apply(weight_kg, equipment_needed, unit, steps, "nearest")


def round_down_to_step(
    weight_kg: float, equipment_needed, unit: str = "kg", steps: Optional[dict] = None
) -> float:
    """Вниз до шага. Нижняя граница — один шаг."""
    return _apply(weight_kg, equipment_needed, unit, steps, "down")
