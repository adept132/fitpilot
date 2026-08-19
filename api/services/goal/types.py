"""Словарь понятий автопилота цели.

Все структуры неизменяемы: ядро — чистые функции, мутабельное состояние
в них только источник ошибок (та же конвенция, что в progression.types).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional, Sequence


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


@dataclass(frozen=True)
class Rates:
    """Три темпа, между которыми решает решатель рычагов (P0-12, §5.4)."""

    required: float                 # кг e1RM в неделю, чтобы успеть к сроку
    plan: float                     # что даёт план (с калибровкой)
    ceiling: float                  # биологический потолок


@dataclass(frozen=True)
class Lever:
    """Одна ступень лестницы рычагов, предложенная пользователю."""

    index: int
    kind: str
    reason_code: str
    effect_slope: float             # прибавка к темпу, кг/нед
    effect_days: int                # насколько раньше наступит ETA
    detail: dict = field(default_factory=dict)


# Контракт пересимуляции (P0-12, фикс C1 финального ревью): decide.py не
# знает, КАК рычаг меняет план (сессии, диапазон повторов, схему) — это
# знание живёт в simulate.apply_lever и в вызывающей стороне (service.py,
# у которой есть SchemeContext и будущие сессии). decide.py лишь просит
# число: "какой темп и какой ETA даст этот рычаг, если применить его поверх
# уже принятых рычагов applied" — и получает его РЕАЛЬНЫМ прогоном движка
# (simulate.run), а не оценкой. applied передаётся явно (а не через
# скрытое состояние замыкания), поэтому одинаковый вызов всегда даёт
# одинаковый ответ — decide.py остаётся чистой функцией своих аргументов.
SimulateWith = Callable[[Sequence[Lever], str, dict], tuple[float, Optional[date]]]


@dataclass(frozen=True)
class DecisionInput:
    """Вход решателя: три темпа плюс контекст плана и тренда."""

    rates: Rates
    deadline: date
    eta: Optional[date]
    lift_in_plan: bool
    trend_slope: float              # фактический тренд, кг/нед; < 0 — регресс
    microcycles_left: int
    headroom_sets: int              # сколько подходов ещё влезает до MRV
    scheme: str                     # текущая схема прогрессии
    is_heavy_compound: bool         # fatigue_tier == 1 и штанга/смит
    rep_max: int                    # верх текущего диапазона повторов
    target_reps: int                # повторы, на которые поставлена цель
