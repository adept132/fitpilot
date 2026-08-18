"""Словарь понятий автопилота цели.

Все структуры неизменяемы: ядро — чистые функции, мутабельное состояние
в них только источник ошибок (та же конвенция, что в progression.types).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass(frozen=True)
class FutureSession:
    """Одна будущая тренировка, где стоит целевое упражнение."""

    date: date
    phase_effort_tier: str          # medium | hard | prefailure | failure | deload
    prescription_sets: int


@dataclass(frozen=True)
class Milestone:
    week_start: date
    expected_e1rm: float


@dataclass(frozen=True)
class Simulation:
    nominal_date: Optional[date]
    calibrated_date: Optional[date]
    nominal_slope: float            # кг e1RM в неделю без калибровки
    plan_slope: float               # с калибровкой, если она доступна
    milestones: list[Milestone] = field(default_factory=list)
    horizon: str = "materialized"
    calibration_available: bool = False
    factor: Optional[float] = None
    sessions_used: int = 0
