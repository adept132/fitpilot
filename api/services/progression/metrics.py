"""Метрика силы: e1RM по Эпли с учётом RIR.

Единая метрика для базовых и изолирующих упражнений. Формула перенесена из
autoprogression.py без изменений — характеризационные тесты фиксируют это.
"""

from __future__ import annotations

from typing import Optional

# effort_level -> RIR. Оба варианта warmup: "warmup" с фронта,
# "warmup_effort" из бэкенд-схемы.
EFFORT_TO_RIR = {
    "warmup": 4,
    "warmup_effort": 4,
    "light": 3,
    "easy": 3,
    "medium": 2,
    "prefailure": 1,
    "failure": 0,
}
DEFAULT_RIR = 2


def effort_to_rir(effort_level: Optional[str]) -> int:
    if not effort_level:
        return DEFAULT_RIR
    return EFFORT_TO_RIR.get(effort_level.strip().lower(), DEFAULT_RIR)


def e1rm(weight_kg: float, reps: int, rir: int) -> float:
    """Эпли с поправкой на запас повторений."""
    return weight_kg * (1 + (reps + rir) / 30)


def weight_for_e1rm(target: float, reps: int, rir: int) -> float:
    """Обратная задача: какой вес даст целевой e1RM на заданных повторах."""
    return target / (1 + (reps + rir) / 30)
