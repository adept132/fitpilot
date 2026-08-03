"""Словарь понятий периодизации. Ни одного объекта SQLAlchemy.

Все структуры неизменяемы в смысле frozen=True (запрещено переприсваивание
атрибутов). Исключение: Proposal.payload — сознательно обычный dict, потому
что уезжает прямо в колонку JSONB и обязан остаться dict для сериализации;
мутировать его на месте нельзя по соглашению, а не по механике (как в readiness/types,
где словари оборачиваются в MappingProxyType).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional


@dataclass(frozen=True)
class PhaseSnapshot:
    """Одна фаза внутри снимка блока.

    phase_number — СТАБИЛЬНЫЙ идентификатор фазы, не порядковый номер:
    на него ссылается WorkoutSession.mesocycle_phase завершённых сессий.
    Порядок фаз задаётся позицией в кортеже BlockState.phases.
    """

    phase_number: int
    name: str
    effort_tier: str
    length_days: int


@dataclass(frozen=True)
class BlockState:
    """Снимок блока, достаточный для всей арифметики координаты."""

    block_index: int
    phases: tuple[PhaseSnapshot, ...]
    start_date: date
    microcycle_length: int


@dataclass(frozen=True)
class BlockPosition:
    """Где мы находимся внутри блока прямо сейчас."""

    phase_number: int
    effort_tier: str
    phase_ordinal: int          # 1-based позиция фазы в списке
    phases_total: int
    day_in_block: int           # 1-based
    days_to_deload: Optional[int]
    is_last_phase: bool
    is_complete: bool


@dataclass(frozen=True)
class FatigueSignal:
    """Выжимка из усталостной модели. band_known=False — холодный старт."""

    fatigued_days: int = 0
    sharp_rise: bool = False
    band_known: bool = False
    chronic_level: Optional[float] = None
    chronic_at_block_start: Optional[float] = None


@dataclass(frozen=True)
class PlateauSignal:
    """Плато уровня блока плюс адреса упражнений для структурных предложений."""

    exercises_with_history: int = 0
    stalled: int = 0
    stalled_after_deload: tuple[int, ...] = ()


@dataclass(frozen=True)
class ReadinessSignal:
    """Уровни последних вердиктов чек-ина, свежие первыми."""

    recent_levels: tuple[str, ...] = ()


@dataclass(frozen=True)
class DecisionInput:
    """Полный вход решателя."""

    position: BlockPosition
    fatigue: FatigueSignal = field(default_factory=FatigueSignal)
    plateau: PlateauSignal = field(default_factory=PlateauSignal)
    readiness: ReadinessSignal = field(default_factory=ReadinessSignal)
    early_deload_used: bool = False
    workouts_to_planned_deload: Optional[int] = None


@dataclass(frozen=True)
class Proposal:
    """Решение движка. Действий не содержит — только что и почему."""

    kind: str
    reason_code: str
    payload: dict[str, Any] = field(default_factory=dict)  # обычный dict для JSONB, не мутировать
